#!/usr/bin/env node
// Extracts contact emails from page text already stored in leads.website_crawl.
//
// No network calls: every address here was fetched during an earlier crawl and has been
// sitting in text_excerpt unextracted. That makes this pass free and re-runnable, and
// it should be re-run after every crawl, since new pages mean new addresses.
//
// Results land in leads.business_email keyed on (business_id, email), plus three
// rolled-up columns on businesses (contact_email, contact_email_count,
// contact_email_is_role) that exports read without joining.
//
// Precision over recall throughout: a junk address in a send list costs sender
// reputation, which is slow and expensive to repair, while a missed address costs one
// prospect that the next crawl may pick up anyway. Rejections are counted and reported
// by reason rather than dropped silently -- silent filtering is how a bad rule survives.
//
// Usage:
//   node scripts/extract-emails.mjs --dry-run       # report, write nothing
//   node scripts/extract-emails.mjs                 # extract and store
//   node scripts/extract-emails.mjs --limit 200     # smoke test

import { writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { extractFromText, confidenceFor, bestOf, rejectReason } from "./lib/emails.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SCRATCH = process.env.CLAUDE_SCRATCH || "/tmp";

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) continue;
    const key = argv[i].slice(2);
    const val = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
    out[key] = val;
  }
  return out;
}

/** Serializes for a dollar-quoted jsonb literal; same NUL hazard as the other passes. */
function jsonLiteral(value) {
  return JSON.stringify(value)
    .replace(/\\u000[0-8bcef]|\\u001[0-9a-f]/gi, " ")
    .replace(/\$e\$/g, "");
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);
  const limit = args.limit ? Number(args.limit) : null;
  const env = loadEnv(ROOT);

  // Pull the crawled text. Joining to businesses here so the site's own domain is
  // available for the same-domain check without a second query.
  // signal_emails is the mailto: channel. detectPage() in site-signals.mjs runs its
  // extractor over raw HTML, so it sees <a href="mailto:...">; toText() historically
  // stripped those tags before text_excerpt was written, so an address linked but never
  // printed existed in signals and nowhere else. Older crawl rows still look like that,
  // and reading both sources here recovers them without re-fetching anything.
  const rows = JSON.parse(q(`
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'business_id', w.business_id,
      'url', COALESCE(w.final_url, w.url),
      'page_kind', w.page_kind,
      'website', b.website,
      'text', w.text_excerpt,
      'signal_emails', COALESCE(w.signals->'person_emails', '[]'::jsonb)
                       || COALESCE(w.signals->'role_emails', '[]'::jsonb)
    )), '[]'::jsonb)
    FROM leads.website_crawl w
    JOIN leads.businesses b ON b.id = w.business_id
    WHERE w.http_status = 200
      AND (w.text_excerpt IS NOT NULL
           OR w.signals ? 'person_emails' OR w.signals ? 'role_emails')
    ${limit ? `LIMIT ${limit}` : ""}
  `, { args: ["-tA"], env }));

  console.error(`[email] scanning ${rows.length} crawled pages`);

  // business_id -> email -> record. Merged across pages so an address appearing on
  // several pages keeps its best page's confidence and a count of where it was seen.
  const byBusiness = new Map();
  const rejected = new Map();
  let rawMatches = 0;
  let fromSignals = 0;

  for (const row of rows) {
    // The stored signal addresses go through the identical filter and classifier as
    // page text -- a mailto: is a different *channel*, not a lower standard of
    // evidence, and filler@godaddy.com is just as worthless behind an href.
    const text = [
      (row.signal_emails || []).join(" "),
      row.text || "",
    ].join(" ");

    const found = extractFromText(text, row.website);
    rawMatches += found.length;
    if (row.signal_emails?.length) fromSignals++;

    // Count rejections separately so the filter rules stay observable. Re-scanning the
    // raw matches is cheap next to the crawl that produced them.
    for (const raw of text.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g) || []) {
      const reason = rejectReason(raw.toLowerCase().replace(/[.,;:)\]}>'"]+$/, ""));
      if (reason) rejected.set(reason, (rejected.get(reason) || 0) + 1);
    }

    if (!found.length) continue;
    if (!byBusiness.has(row.business_id)) byBusiness.set(row.business_id, new Map());
    const bucket = byBusiness.get(row.business_id);

    for (const rec of found) {
      const confidence = confidenceFor(rec, row.page_kind);
      const existing = bucket.get(rec.email);
      if (existing) {
        existing.page_count++;
        // Keep the strongest sighting: the same address on the contact page is better
        // evidence than on a pricing page.
        if (confidence > existing.confidence) {
          existing.confidence = confidence;
          existing.source_url = row.url;
          existing.page_kind = row.page_kind;
        }
      } else {
        bucket.set(rec.email, {
          ...rec, confidence, source_url: row.url, page_kind: row.page_kind, page_count: 1,
        });
      }
    }
  }

  const payload = [];
  const rollups = [];
  for (const [businessId, bucket] of byBusiness) {
    const records = [...bucket.values()];
    for (const r of records) payload.push({ business_id: businessId, ...r });
    const best = bestOf(records);
    rollups.push({
      business_id: businessId,
      contact_email: best.email,
      contact_email_count: records.length,
      contact_email_is_role: best.is_role,
    });
  }

  const roleCount = payload.filter((r) => r.is_role).length;
  const freeCount = payload.filter((r) => r.is_free_mail).length;
  const ownDomain = payload.filter((r) => r.matches_site_domain).length;

  console.error(
    `\n[email] ${payload.length} addresses across ${byBusiness.size} businesses ` +
    `(${rawMatches} raw matches kept)`
  );
  console.error(`[email] ${fromSignals} pages contributed a mailto: address from signals`);
  console.error(
    `[email] personal=${payload.length - roleCount}  role=${roleCount}  ` +
    `own-domain=${ownDomain}  free-mail=${freeCount}`
  );
  if (rejected.size) {
    console.error("[email] rejected:");
    for (const [reason, n] of [...rejected].sort((a, b) => b[1] - a[1])) {
      console.error(`         ${String(n).padStart(5)}  ${reason}`);
    }
  }

  if (dryRun) {
    console.error("\n[email] --dry-run: nothing written");
    const sample = payload.filter((r) => !r.is_role).slice(0, 10);
    for (const r of sample) {
      console.error(`   ${String(r.confidence).padStart(3)}  ${r.email.padEnd(44)} ${r.page_kind}`);
    }
    return;
  }
  if (!payload.length) return;

  // One statement via a JSON payload rather than thousands of INSERTs. ON CONFLICT
  // keeps the pass idempotent: re-running after a new crawl updates in place.
  const payloadPath = path.join(SCRATCH, `emails-${process.pid}.json`);
  writeFileSync(payloadPath, JSON.stringify({ emails: payload, rollups }));

  const sql = `
    BEGIN;
    CREATE TEMP TABLE email_payload (data jsonb);
    \\copy email_payload (data) FROM '${payloadPath}'

    INSERT INTO leads.business_email
      (business_id, email, local_part, domain, source, source_url, page_kind,
       is_role, is_free_mail, matches_site_domain, confidence, extracted_at)
    SELECT (e->>'business_id')::uuid, e->>'email', e->>'local_part', e->>'domain',
           'crawl', e->>'source_url', e->>'page_kind',
           (e->>'is_role')::boolean, (e->>'is_free_mail')::boolean,
           (e->>'matches_site_domain')::boolean, (e->>'confidence')::int, now()
    FROM email_payload p, LATERAL jsonb_array_elements(p.data->'emails') e
    ON CONFLICT (business_id, email) DO UPDATE SET
      source_url = EXCLUDED.source_url,
      page_kind = EXCLUDED.page_kind,
      confidence = EXCLUDED.confidence,
      is_role = EXCLUDED.is_role,
      is_free_mail = EXCLUDED.is_free_mail,
      matches_site_domain = EXCLUDED.matches_site_domain,
      extracted_at = now();

    UPDATE leads.businesses b
    SET contact_email = r->>'contact_email',
        contact_email_count = (r->>'contact_email_count')::int,
        contact_email_is_role = (r->>'contact_email_is_role')::boolean,
        date_updated = now()
    FROM email_payload p, LATERAL jsonb_array_elements(p.data->'rollups') r
    WHERE b.id = (r->>'business_id')::uuid;
    COMMIT;
  `;
  execFileSync("/usr/bin/psql", ["-X", "-v", "ON_ERROR_STOP=1", env.LEADS_DB_URL],
    { input: sql, encoding: "utf8", stdio: ["pipe", "inherit", "inherit"] });

  console.error(`[email] stored ${payload.length} addresses for ${rollups.length} businesses`);
}

main();
