#!/usr/bin/env node
// QC pass over leads.businesses (Prompt 8). Three jobs, all reversible:
//
//   1. Normalize state to two-letter codes.
//   2. Fill the 520 empty states by inference, recording that they were inferred.
//   3. Flag duplicate candidates into leads.qc_review for a human to adjudicate.
//
// What this deliberately does NOT do:
//
//   * It never deletes or merges a row. The earlier design merged rows sharing a
//     domain or phone; on this dataset that would have destroyed ~230 legitimate
//     businesses, because franchise branches share both (stores.petco.com covers 12
//     stores in 11 cities). Duplicates are flagged, not resolved.
//   * It sets no qc_status = 'CLOSED'. The scraper's `status` field is not an
//     open/closed flag despite the name -- it holds Google's editorial blurb
//     ("Casual, kid-friendly American brewpub", "$227"). One row in 3,686 contains
//     the word "closed", so there is no closure signal here to act on.
//
// Usage:
//   node scripts/qc-pass.mjs --dry-run    # report what would change
//   node scripts/qc-pass.mjs              # apply

import { q } from "./lib/q.mjs";

// The 16 states this campaign's metros actually span. An explicit map rather than
// left(state, 2), which silently turns "New York" into "NE".
const STATE_CODES = new Map([
  ["arizona", "AZ"], ["california", "CA"], ["colorado", "CO"], ["connecticut", "CT"],
  ["district of columbia", "DC"], ["florida", "FL"], ["georgia", "GA"], ["illinois", "IL"],
  ["maryland", "MD"], ["massachusetts", "MA"], ["new jersey", "NJ"], ["new york", "NY"],
  ["pennsylvania", "PA"], ["texas", "TX"], ["virginia", "VA"], ["washington", "WA"],
]);

const VALID_CODES = new Set([...STATE_CODES.values()]);

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      const key = argv[i].slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
      out[key] = val;
    }
  }
  return out;
}

/** "Park Slope, Brooklyn, NY" -> "NY"; returns null when the geo has no state suffix. */
function stateFromGeoTarget(geo) {
  const m = /,\s*([A-Z]{2})\s*$/.exec(geo || "");
  return m && VALID_CODES.has(m[1]) ? m[1] : null;
}

function normalizeState(state) {
  if (!state) return null;
  const s = state.trim();
  if (!s) return null;
  if (s.length === 2 && VALID_CODES.has(s.toUpperCase())) return s.toUpperCase();
  return STATE_CODES.get(s.toLowerCase()) || null;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);

  const rows = JSON.parse(q(`
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', id, 'name', name, 'city', city, 'state', state,
      'geo_target', sources->0->>'geo_target',
      'domain', domain, 'phone_normalized', phone_normalized,
      'name_city_state_key', name_city_state_key,
      'place_id', place_id, 'review_count', review_count
    )), '[]'::jsonb)
    FROM leads.businesses
  `, { args: ["-t", "-A"] }));

  console.error(`[qc-pass] loaded ${rows.length} businesses`);

  // --- 1 & 2: state normalization and inference -----------------------------
  const stateUpdates = [];
  const tally = { normalized: 0, inferred: 0, already_ok: 0, unresolved: 0 };

  for (const r of rows) {
    const current = (r.state || "").trim();
    if (current) {
      const code = normalizeState(current);
      if (!code) { tally.unresolved++; continue; } // unknown spelling: leave it alone
      if (code === current) { tally.already_ok++; continue; }
      tally.normalized++;
      stateUpdates.push({ id: r.id, state: code, state_source: "scraped" });
    } else {
      // Service-area businesses (in-home trainers, dog walkers) have no street
      // address at all, so Google returns no state. The geo that found them is the
      // best available evidence: it agrees with the scraped state on 97.3% of the
      // 3,166 rows where both exist. Recorded as inferred so it is never mistaken
      // for observed data.
      const code = stateFromGeoTarget(r.geo_target);
      if (!code) { tally.unresolved++; continue; }
      tally.inferred++;
      stateUpdates.push({ id: r.id, state: code, state_source: "inferred_from_geo_target" });
    }
  }

  console.error(
    `[qc-pass] state: ${tally.normalized} normalized, ${tally.inferred} inferred from geo_target, ` +
    `${tally.already_ok} already 2-letter, ${tally.unresolved} unresolved`
  );

  // --- 3: duplicate candidates ----------------------------------------------
  // Only name-key matches are considered. A shared domain or phone alone is not
  // evidence of duplication in this dataset -- it is the normal shape of a
  // multi-location business.
  const byNameKey = new Map();
  for (const r of rows) {
    if (!r.name_city_state_key) continue;
    if (!byNameKey.has(r.name_city_state_key)) byNameKey.set(r.name_city_state_key, []);
    byNameKey.get(r.name_city_state_key).push(r);
  }

  const flags = [];
  for (const [key, group] of byNameKey) {
    if (group.length < 2) continue;
    const domains = new Set(group.map((g) => g.domain).filter(Boolean));
    const phones = new Set(group.map((g) => g.phone_normalized).filter(Boolean));
    const corroborated =
      (domains.size === 1 && group.filter((g) => g.domain).length > 1) ||
      (phones.size === 1 && group.filter((g) => g.phone_normalized).length > 1);

    // Same name and city, plus a shared domain or phone. Still only a candidate:
    // "Rapawzel Dog Grooming & Day Care" legitimately has two New York locations.
    const reason = corroborated
      ? `same name+city, shared ${domains.size === 1 ? "domain" : "phone"}`
      : "same name+city, no shared domain or phone";

    flags.push({
      kind: "dup_candidate",
      reason,
      // Sorted, because qc_review_open_uidx is a unique index on this array and array
      // equality in Postgres is order-sensitive. Unsorted, the same pair of rows
      // arriving in a different order on a re-run slips past ON CONFLICT and inserts
      // a second flag for the identical finding.
      business_ids: group.map((g) => g.id).sort(),
      details: {
        name_city_state_key: key,
        corroborated,
        members: group.map((g) => ({
          id: g.id, name: g.name, city: g.city, state: g.state,
          domain: g.domain, phone: g.phone_normalized,
          place_id: g.place_id, review_count: g.review_count,
        })),
      },
    });
  }

  const corroboratedCount = flags.filter((f) => f.details.corroborated).length;
  console.error(
    `[qc-pass] duplicates: ${flags.length} candidate groups flagged ` +
    `(${corroboratedCount} corroborated by a shared domain/phone), 0 merged`
  );

  if (dryRun) {
    console.error("\n[qc-pass] --dry-run: nothing written. Sample flags:");
    for (const f of flags.slice(0, 8)) {
      const m = f.details.members;
      console.error(`  ${f.reason}: ` + m.map((x) => `${x.name} [${x.city || "?"} rc=${x.review_count ?? "-"}]`).join("  |  "));
    }
    return;
  }

  // --- apply -----------------------------------------------------------------
  // Both writes go in one transaction: a partial QC pass is harder to reason about
  // than none at all.
  const sql = `
    BEGIN;

    CREATE TEMP TABLE qc_state (data jsonb);
    CREATE TEMP TABLE qc_flags (data jsonb);

    INSERT INTO qc_state VALUES ($qc$${JSON.stringify(stateUpdates)}$qc$::jsonb);
    INSERT INTO qc_flags VALUES ($qc$${JSON.stringify(flags)}$qc$::jsonb);

    UPDATE leads.businesses b
    SET state        = u->>'state',
        state_source = u->>'state_source',
        date_updated = now()
    FROM qc_state p, LATERAL jsonb_array_elements(p.data) u
    WHERE b.id = (u->>'id')::uuid;

    -- Rows whose state was already a correct 2-letter code still need their
    -- provenance recorded, or 'scraped' would look like a property only of rows this
    -- pass happened to rewrite.
    UPDATE leads.businesses
    SET state_source = 'scraped'
    WHERE state_source IS NULL AND coalesce(state, '') <> '';

    INSERT INTO leads.qc_review (kind, reason, business_ids, details)
    SELECT u->>'kind', u->>'reason',
           ARRAY(SELECT jsonb_array_elements_text(u->'business_ids'))::uuid[],
           u->'details'
    FROM qc_flags p, LATERAL jsonb_array_elements(p.data) u
    ON CONFLICT DO NOTHING;

    COMMIT;
  `;

  q(sql);
  console.error(`[qc-pass] applied ${stateUpdates.length} state updates, ${flags.length} review flags`);
}

main();
