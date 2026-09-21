#!/usr/bin/env node
// Rescores every row in leads.businesses against the current ICP definition in
// lib/score.mjs, reading each record's stored raw_scrape rather than re-scraping.
//
// Exists because scores written before the scorer was fixed measured the search
// keyword instead of the business, inflating Tier 1 to 55% of the database. This
// recomputes icp_score, icp_tier, qc_status and service_category in place. sources[]
// and every scraped field are left untouched -- Prompt 8 forbids erasing discovery
// history.
//
// Usage:
//   node scripts/rescore.mjs --dry-run      # print the before/after tier histogram
//   node scripts/rescore.mjs                # apply
//   node scripts/rescore.mjs --limit 100    # apply to the first N rows (smoke test)

import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { tmpdir } from "node:os";
import { loadEnv } from "./lib/env.mjs";
import { scoreIcp, aboutOptionNames } from "./lib/score.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

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

function psql(env, sql, extraArgs = []) {
  return execFileSync("/usr/bin/psql", ["-X", "-v", "ON_ERROR_STOP=1", env.LEADS_DB_URL, ...extraArgs, "-c", sql],
    { encoding: "utf8", maxBuffer: 1024 * 1024 * 512 });
}

function histogram(rows, key) {
  const h = new Map();
  for (const r of rows) h.set(r[key], (h.get(r[key]) || 0) + 1);
  return [...h.entries()].sort((a, b) => String(a[0]).localeCompare(String(b[0])));
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);
  const limit = args.limit ? parseInt(args.limit, 10) : null;
  const env = loadEnv(ROOT);

  // Pull one JSON array of the scoring inputs. raw_scrape holds the full original
  // place record, so the fields the scorer wants are all there even when the
  // corresponding column was never populated (description, for one).
  const rows = JSON.parse(psql(env, `
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', id,
      'name', name,
      'icp_score', icp_score,
      'icp_tier', icp_tier,
      'qc_status', qc_status,
      'service_category', service_category,
      'primary_category', COALESCE(primary_category, raw_scrape->>'category'),
      'categories', COALESCE(to_jsonb(additional_categories), raw_scrape->'categories'),
      'description', COALESCE(description, raw_scrape->>'description'),
      'about', raw_scrape->'about',
      'rating', rating,
      'review_count', review_count,
      'website', website,
      'price_range', price_range,
      'booking_present', booking_present,
      'growth_score', growth_score,
      'ads_confirmed_active', ads_confirmed_active,
      'ads_creative_count', ads_creative_count
    )), '[]'::jsonb)
    FROM (SELECT * FROM leads.businesses ORDER BY id ${limit ? `LIMIT ${limit}` : ""}) b
  `, ["-t", "-A"]));

  console.error(`[rescore] loaded ${rows.length} businesses`);

  const before = rows.map((r) => ({ tier: r.icp_tier, qc: r.qc_status, cat: r.service_category }));
  const updates = [];
  const after = [];

  for (const r of rows) {
    const categories = Array.isArray(r.categories) ? r.categories : [];
    const res = scoreIcp({
      name: r.name,
      keyword: null, // rescoring never sees a query; that is the point
      primaryCategory: r.primary_category,
      categories,
      description: r.description,
      about: aboutOptionNames(r.about),
      rating: r.rating,
      reviewCount: r.review_count,
      website: r.website,
      priceRange: r.price_range,
      bookingPresent: r.booking_present,
      growthScore: r.growth_score,
      // Null for a business the Ads Transparency pass has not reached; the scorer
      // treats that as "unknown", not "does not advertise", so an unchecked row is
      // never penalised for missing evidence.
      adsConfirmedActive: r.ads_confirmed_active,
      adsCreativeCount: r.ads_creative_count,
    });

    after.push({ tier: res.tier, qc: res.qcStatus, cat: res.serviceCategory || r.service_category });

    const changed =
      res.score !== r.icp_score ||
      res.tier !== r.icp_tier ||
      res.qcStatus !== r.qc_status ||
      (res.serviceCategory && res.serviceCategory !== r.service_category);

    if (changed) {
      updates.push({
        id: r.id,
        icp_score: res.score,
        icp_tier: res.tier,
        qc_status: res.qcStatus,
        // A rejected row keeps its existing category: the column is NOT NULL and the
        // original value records which sweep surfaced it.
        service_category: res.serviceCategory || r.service_category,
      });
    }
  }

  const fmt = (label, list, key) =>
    `${label}: ` + histogram(list, key).map(([k, n]) =>
      `${k}=${n} (${(100 * n / list.length).toFixed(1)}%)`).join("  ");

  console.error("");
  console.error(fmt("tier  before", before, "tier"));
  console.error(fmt("tier  after ", after, "tier"));
  console.error("");
  console.error(fmt("qc    before", before, "qc"));
  console.error(fmt("qc    after ", after, "qc"));
  console.error("");
  console.error(fmt("cat   before", before, "cat"));
  console.error(fmt("cat   after ", after, "cat"));
  console.error("");
  console.error(`[rescore] ${updates.length} rows would change`);

  if (dryRun) {
    console.error("[rescore] --dry-run: nothing written");
    return;
  }
  if (updates.length === 0) return;

  // Apply as one UPDATE ... FROM a JSON payload: a single statement, one transaction,
  // no 3,686-statement script.
  const payloadPath = path.join(tmpdir(), `rescore-${process.pid}.json`);
  writeFileSync(payloadPath, JSON.stringify(updates));

  const sql = `
    BEGIN;
    CREATE TEMP TABLE rescore_payload (data jsonb);
    \\copy rescore_payload (data) FROM '${payloadPath}'
    UPDATE leads.businesses b
    SET icp_score        = (u->>'icp_score')::int,
        icp_tier         = u->>'icp_tier',
        qc_status        = (u->>'qc_status')::leads.qc_status,
        service_category = u->>'service_category',
        date_updated     = now()
    FROM rescore_payload p, LATERAL jsonb_array_elements(p.data) u
    WHERE b.id = (u->>'id')::uuid;
    COMMIT;
  `;
  execFileSync("/usr/bin/psql", ["-X", "-v", "ON_ERROR_STOP=1", env.LEADS_DB_URL],
    { input: sql, encoding: "utf8", stdio: ["pipe", "inherit", "inherit"] });

  console.error(`[rescore] applied ${updates.length} updates`);
}

main();
