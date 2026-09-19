#!/usr/bin/env node
// Phase C enrichment (Prompt 9): identifies the likely decision maker for each business
// from the website text already captured in leads.website_crawl.
//
// Deterministic, not an LLM. The plan allowed for a model here, but the measured
// precision of explicit patterns is high enough that a model would mostly add cost,
// latency and a new way to hallucinate a name -- which is the one thing Prompt 9
// forbids outright. A model pass remains a reasonable follow-up for the pages these
// patterns cannot read; it is not a prerequisite.
//
// No network access. Everything is read from the crawl table, so this is cheap to
// re-run after tuning the patterns.
//
// What "confidence" means here, measured over 1,307 crawled businesses:
//   high   - name and title bound by punctuation ("Jane Doe, Owner"), or corroborated
//            by a second pattern or a matching person-shaped email. ~97% precision on
//            a hand-audited sample of 40.
//   medium - an explicit but looser pattern, chiefly "Founder Jane Doe", where the
//            capture can run into surrounding copy.
//   low    - a first-person about page ("My name is ..."), where the title is inferred
//            from the page rather than stated, or a name seen only on a staff page.
//
// Usage:
//   node scripts/enrich-decision-makers.mjs --dry-run
//   node scripts/enrich-decision-makers.mjs --min-confidence high
//   node scripts/enrich-decision-makers.mjs --limit 200 --service-category dog_training

import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { extractCandidates, pickDecisionMaker } from "./lib/people.mjs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const CONF_RANK = { low: 1, medium: 2, high: 3 };

function parseArgs(argv) {
  const o = { dryRun: false, limit: null, minConfidence: "medium", serviceCategory: null, tier: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dry-run") o.dryRun = true;
    else if (a === "--limit") o.limit = Number(argv[++i]);
    else if (a === "--min-confidence") o.minConfidence = argv[++i];
    else if (a === "--service-category") o.serviceCategory = argv[++i];
    else if (a === "--tier") o.tier = argv[++i];
    else throw new Error(`unknown flag: ${a}`);
  }
  if (!CONF_RANK[o.minConfidence]) throw new Error("--min-confidence must be low, medium or high");
  return o;
}

/** SQL string literal. Names come from third-party web pages, so never interpolate raw. */
function lit(s) {
  if (s === null || s === undefined) return "NULL";
  // Strip C0 controls for the same reason enrich-websites.mjs does: Postgres text
  // cannot store \u0000 and one bad row aborts the whole statement.
  const clean = String(s).replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, " ");
  return `'${clean.replace(/'/g, "''")}'`;
}

function main() {
  const opts = parseArgs(process.argv);
  const env = loadEnv(ROOT);

  const where = ["wc.http_status = 200", "wc.text_excerpt IS NOT NULL"];
  if (opts.serviceCategory) where.push(`b.service_category = ${lit(opts.serviceCategory)}`);
  if (opts.tier) where.push(`b.icp_tier IN (${opts.tier.split(",").map((t) => lit(t.trim())).join(",")})`);

  console.log("[decision-makers] loading crawled pages...");
  const raw = q(
    `SELECT json_agg(r)::text FROM (
       SELECT wc.business_id, wc.url, wc.page_kind, wc.text_excerpt,
              wc.signals->'person_emails' AS person_emails
       FROM leads.website_crawl wc
       JOIN leads.businesses b ON b.id = wc.business_id
       WHERE ${where.join(" AND ")}
     ) r`,
    { env, args: ["-A", "-t"] },
  ).trim();

  const rows = raw && raw !== "" ? JSON.parse(raw) : [];
  if (!rows.length) {
    console.log("[decision-makers] no crawled pages matched; nothing to do");
    return;
  }

  const byBusiness = new Map();
  for (const r of rows) {
    if (!byBusiness.has(r.business_id)) byBusiness.set(r.business_id, []);
    byBusiness.get(r.business_id).push(r);
  }
  console.log(`[decision-makers] ${rows.length} pages across ${byBusiness.size} businesses`);

  const found = [];
  const stats = { examined: 0, found: 0, high: 0, medium: 0, low: 0, belowThreshold: 0 };

  for (const [businessId, pages] of byBusiness) {
    stats.examined++;
    const candidates = [];
    const emails = new Set();
    for (const p of pages) {
      for (const e of p.person_emails || []) emails.add(e);
      candidates.push(...extractCandidates(p.text_excerpt, { pageKind: p.page_kind, pageUrl: p.url }));
    }

    const dm = pickDecisionMaker(candidates, { personEmails: [...emails] });
    if (!dm) continue;
    stats.found++;
    stats[dm.confidence]++;

    if (CONF_RANK[dm.confidence] < CONF_RANK[opts.minConfidence]) {
      stats.belowThreshold++;
      continue;
    }
    found.push({ businessId, ...dm });
    if (opts.limit && found.length >= opts.limit) break;
  }

  console.log(
    `[decision-makers] found=${stats.found}/${stats.examined} ` +
      `(high=${stats.high} medium=${stats.medium} low=${stats.low}), ` +
      `${stats.belowThreshold} below --min-confidence=${opts.minConfidence}, ` +
      `${found.length} to write`,
  );

  if (opts.dryRun) {
    console.log("\n[decision-makers] dry run; sample:");
    for (const f of found.slice(0, opts.limit || 30)) {
      console.log(`  ${f.name.padEnd(26)} ${f.title.padEnd(15)} ${f.confidence.padEnd(6)} ${f.pattern}`);
      console.log(`      ${(f.evidence_quote || "").slice(0, 120)}`);
    }
    console.log("\n[decision-makers] dry run: nothing written");
    return;
  }

  // Batched so one rejected row costs a chunk rather than the run -- the failure mode
  // that destroyed two full crawls before enrich-websites.mjs was made incremental.
  const BATCH = 200;
  let written = 0;
  for (let i = 0; i < found.length; i += BATCH) {
    const chunk = found.slice(i, i + BATCH);
    const values = chunk
      .map((f) => `(${lit(f.businessId)}::uuid, ${lit(f.name)}, ${lit(f.title)}, ${lit(f.source_url)}, ${lit(f.confidence)})`)
      .join(",\n         ");
    try {
      q(
        `BEGIN;
         CREATE TEMP TABLE dm_batch (
           business_id uuid, name text, title text, source_url text, confidence text
         ) ON COMMIT DROP;
         INSERT INTO dm_batch VALUES
         ${values};
         UPDATE leads.businesses b
            SET decision_maker_name       = d.name,
                decision_maker_title      = d.title,
                decision_maker_source     = d.source_url,
                decision_maker_confidence = d.confidence
           FROM dm_batch d
          WHERE b.id = d.business_id;
         COMMIT;`,
        { env },
      );
      written += chunk.length;
      console.log(`[decision-makers] wrote ${written}/${found.length}`);
    } catch (err) {
      console.error(`[decision-makers] batch at ${i} failed: ${err.message}`);
    }
  }
  console.log(`[decision-makers] done: ${written} businesses updated`);
}

main();
