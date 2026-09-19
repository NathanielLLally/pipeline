#!/usr/bin/env node
// Phase D (Prompt 7, second source): confirms CURRENT ad spend via Google's Ads
// Transparency Center.
//
// Why this exists alongside the website crawler: `marketing_active` means only that a
// paid-media pixel is installed, and a pixel outlives the campaign that installed it.
// This pass asks Google what it is actually serving today, per domain, with the date
// each creative was last shown. Measured on 25 top-tier prospects: 19 advertising, and
// 9 of those 19 had marketing_active false or null -- so this is not a refinement of
// the pixel flag, it is a different and stronger question.
//
// One request per business, form-POSTed through the Webshare SOCKS5 pool via curl.
// Verified 5 proxies out of 5: unlike Meta, Google does not refuse this ASN.
//
// Evidence lands in leads.ads_transparency keyed on (business_id, domain), so a re-run
// skips what it already checked and only new or failed rows cost anything.
//
// READ THIS BEFORE CHANGING THE REQUEST: an empty response `{}` means "runs no ads",
// not "the request was malformed". The two are indistinguishable from one call, and
// mistaking them cost an earlier session its conclusion about this endpoint entirely.
// --probe checks a known advertiser first for exactly this reason, and the full run
// aborts if that check comes back empty.
//
// Usage:
//   node scripts/enrich-ads-transparency.mjs --probe            # verify the endpoint only
//   node scripts/enrich-ads-transparency.mjs --dry-run --limit 25
//   node scripts/enrich-ads-transparency.mjs --tier "Tier 1,Tier 2"
//   node scripts/enrich-ads-transparency.mjs --recheck          # ignore prior results
//   node scripts/enrich-ads-transparency.mjs --stale-days 30    # refresh old rows only

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { loadProxies, rotator } from "./lib/proxies.mjs";
import {
  ENDPOINT, UA, requestBody, toDomain, parseCreatives, isRecent, RECENT_DAYS,
} from "./lib/ads-transparency.mjs";

const execFileAsync = promisify(execFile);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const TIMEOUT_SEC = 30;
const MAX_BYTES = 8 << 20;   // a heavy advertiser's page of 40 creatives runs ~25KB
const FLUSH_EVERY = 100;

// A domain that certainly advertises, used to tell "no ads" apart from "broken request".
const PROBE_DOMAIN = "nike.com";

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

/**
 * Serializes a payload for a dollar-quoted jsonb literal.
 *
 * Same two hazards as the website crawler: Postgres text cannot store \u0000 and jsonb
 * aborts the whole transaction rather than dropping it, and a payload containing the
 * literal delimiter would end the quoted string early. Advertiser names come from
 * Google and are mostly clean, but "mostly" has already cost this project one completed
 * crawl.
 */
function jsonLiteral(value) {
  return JSON.stringify(value)
    .replace(/\\u000[0-8bcef]|\\u001[0-9a-f]/gi, " ")
    .replace(/\$e\$/g, "");
}

/**
 * One POST through the next proxy in rotation.
 *
 * Credentials go in --proxy-user, never in the proxy URL: curl 8.20 rejects the
 * embedded form and echoes the whole URL back on failure, which has put live proxy
 * credentials into a transcript once already.
 */
async function fetchOnce(domain, nextProxy) {
  const p = nextProxy();
  const args = [
    "-sS", "-X", "POST", "--compressed",
    "--max-time", String(TIMEOUT_SEC),
    "--max-filesize", String(MAX_BYTES),
    "--socks5-hostname", `${p.host}:${p.port}`,
    "--proxy-user", `${p.user}:${p.pass}`,
    "-H", "content-type: application/x-www-form-urlencoded",
    "-H", "x-same-domain: 1",
    "-H", `user-agent: ${UA}`,
    "-H", "referer: https://adstransparency.google.com/",
    "--data-raw", requestBody(domain),
    "-w", "\n@@%{http_code}",
    ENDPOINT,
  ];
  try {
    const { stdout } = await execFileAsync("/usr/bin/curl", args, {
      encoding: "utf8", maxBuffer: MAX_BYTES + (1 << 20),
    });
    const i = stdout.lastIndexOf("\n@@");
    if (i < 0) return { status: 0, body: "", error: "no status marker" };
    const status = Number(stdout.slice(i + 3)) || 0;
    // A 302 here is Google's rate limiter: it redirects to /sorry/index rather than
    // returning 429. Flagged distinctly because it says nothing about the business --
    // it means the POOL is throttled, and storing it as an ordinary error would record
    // a permanent "no result" for a domain that was never actually asked about.
    if (status === 302) return { status, body: "", error: "rate-limited (/sorry)", throttled: true };
    return { status, body: stdout.slice(0, i), error: null };
  } catch (err) {
    // curl's stderr can name the proxy host; the password is not in it thanks to
    // --proxy-user, but keep the note short rather than storing curl's whole complaint.
    const raw = (err.stderr || "").toString();
    const m = raw.match(/curl:\s*\(\d+\)\s*(.{0,90})/);
    return { status: 0, body: "", error: m ? m[1].trim() : "fetch failed" };
  }
}

/**
 * Fetch with retries through *different* proxies.
 *
 * Retries only on transport failure and on the statuses that indicate the proxy or a
 * throttle rather than a real answer. A 200 carrying `{}` is a real answer and is never
 * retried -- retrying it would turn "runs no ads" into wasted budget on every
 * non-advertiser, which is a third of the database.
 */
async function fetchWithRetry(domain, nextProxy, attempts = 3) {
  let last;
  for (let i = 0; i < attempts; i++) {
    last = await fetchOnce(domain, nextProxy);
    const retryable = last.status === 0 || last.status === 302 || last.status === 403 ||
                      last.status === 429 || last.status >= 500;
    if (!retryable) return last;
    if (i < attempts - 1) await new Promise((r) => setTimeout(r, 500 * (i + 1)));
  }
  return last;
}

/** Fetches one domain and decodes it, keeping transport and parse failures distinct. */
async function checkDomain(domain, nextProxy) {
  const res = await fetchWithRetry(domain, nextProxy);
  if (res.status !== 200) {
    return {
      ok: false, http_status: res.status,
      fetch_error: res.error || `http ${res.status}`,
      throttled: Boolean(res.throttled),
    };
  }
  let json;
  try {
    json = JSON.parse(res.body);
  } catch {
    // A 200 that is not JSON means the shape of the endpoint changed, which is worth
    // recording loudly rather than counting as "no ads".
    return { ok: false, http_status: 200, fetch_error: "unparseable response" };
  }
  return { ok: true, http_status: 200, fetch_error: null, ...parseCreatives(json) };
}

/** Writes one batch: evidence rows, then the rolled-up columns on businesses. */
function persist(batch, env) {
  if (!batch.length) return;
  const evidence = batch.map((r) => ({
    business_id: r.business_id,
    domain: r.domain,
    advertiser_id: r.advertiser_id ?? null,
    advertiser_name: r.advertiser_name ?? null,
    creative_count: r.creative_count ?? 0,
    first_shown: r.first_shown ?? null,
    last_shown: r.last_shown ?? null,
    formats: r.formats ?? [],
    creatives: r.creatives ?? [],
    http_status: r.http_status ?? 0,
    fetch_error: r.fetch_error ?? null,
  }));

  q(`
    BEGIN;
    CREATE TEMP TABLE ads_payload (data jsonb) ON COMMIT DROP;
    INSERT INTO ads_payload VALUES ($e$${jsonLiteral(evidence)}$e$::jsonb);

    INSERT INTO leads.ads_transparency
      (business_id, domain, advertiser_id, advertiser_name, creative_count,
       first_shown, last_shown, formats, creatives, http_status, fetch_error)
    SELECT (a->>'business_id')::uuid, a->>'domain', a->>'advertiser_id', a->>'advertiser_name',
           (a->>'creative_count')::int,
           (a->>'first_shown')::timestamptz, (a->>'last_shown')::timestamptz,
           ARRAY(SELECT jsonb_array_elements_text(a->'formats')),
           coalesce(a->'creatives', '[]'::jsonb),
           (a->>'http_status')::int, a->>'fetch_error'
    FROM ads_payload p, LATERAL jsonb_array_elements(p.data) a
    ON CONFLICT (business_id, domain) DO UPDATE SET
      advertiser_id = EXCLUDED.advertiser_id, advertiser_name = EXCLUDED.advertiser_name,
      creative_count = EXCLUDED.creative_count,
      first_shown = EXCLUDED.first_shown, last_shown = EXCLUDED.last_shown,
      formats = EXCLUDED.formats, creatives = EXCLUDED.creatives,
      http_status = EXCLUDED.http_status, fetch_error = EXCLUDED.fetch_error,
      checked_at = now();

    -- The rolled-up columns are derived here rather than in JS so they can never drift
    -- from the evidence table: this recomputes them from what was just written. Rows
    -- whose fetch failed are left alone -- a transport error is not evidence that a
    -- business stopped advertising, and overwriting a good prior result with a null
    -- would silently destroy it.
    UPDATE leads.businesses b
    SET ads_confirmed_active = (t.last_shown IS NOT NULL
                                AND t.last_shown >= now() - interval '${RECENT_DAYS} days'),
        ads_last_shown       = t.last_shown,
        ads_creative_count   = t.creative_count,
        date_updated         = now()
    FROM leads.ads_transparency t, ads_payload p, LATERAL jsonb_array_elements(p.data) a
    WHERE b.id = t.business_id
      AND t.business_id = (a->>'business_id')::uuid
      AND t.domain = a->>'domain'
      AND t.http_status = 200;
    COMMIT;
  `, { env });
}

function buildWhere(args) {
  const where = [
    "website IS NOT NULL",
    "website <> ''",
    // A social profile in the website column is not a domain this advertiser owns:
    // querying facebook.com would return Meta's own ads and attribute them to a dog
    // trainer, which is worse than having no answer.
    "website !~* '(facebook\\.com|instagram\\.com|nextdoor\\.com|linktr\\.ee|yelp\\.com|google\\.com)'",
    "qc_status <> 'REJECTED'",
  ];
  if (args["service-category"]) {
    const cats = String(args["service-category"]).split(",").map((c) => `'${c.trim().replace(/'/g, "''")}'`);
    where.push(`service_category IN (${cats.join(",")})`);
  }
  if (args.tier) {
    const tiers = String(args.tier).split(",").map((t) => `'${t.trim().replace(/'/g, "''")}'`);
    where.push(`icp_tier IN (${tiers.join(",")})`);
  }
  return where.join("\n      AND ");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);
  const limit = args.limit ? Number(args.limit) : null;
  // Defaults chosen after the first full run tripped Google's rate limiter: 4 workers
  // each pausing 250ms is roughly 3-4 domains/s (was ~7 unthrottled).
  const concurrency = args.concurrency ? Number(args.concurrency) : 4;
  const delayMs = args.delay !== undefined ? Number(args.delay) : 250;
  const recheck = Boolean(args.recheck);
  const staleDays = args["stale-days"] ? Number(args["stale-days"]) : null;

  const env = loadEnv(ROOT);
  const proxies = loadProxies(env, args["proxy-mode"] === "rotating" ? "rotating" : "direct");
  const nextProxy = rotator(proxies);

  // Endpoint check, always. `{}` from a small business is ambiguous; `{}` from a known
  // heavy advertiser means the request shape has broken and every result this run would
  // produce is a false negative. Cheap insurance against silently writing 1,200 wrong
  // answers.
  const probe = await checkDomain(PROBE_DOMAIN, nextProxy);
  // Being throttled is a different diagnosis from the request shape having broken, and
  // sending someone to re-capture the payload with Playwright when the real answer is
  // "wait an hour" would waste exactly the kind of time this check exists to save.
  if (probe.throttled) {
    console.error(
      `[ads] RATE-LIMITED before starting: Google redirected the probe to /sorry.\n` +
      `[ads] The limit applies across the whole pool, so rotating proxies will not help. ` +
      `Wait and re-run -- already-checked domains are skipped.`
    );
    process.exit(3);
  }
  if (!probe.ok || !probe.found) {
    console.error(
      `[ads] ENDPOINT CHECK FAILED: ${PROBE_DOMAIN} returned no creatives ` +
      `(status=${probe.http_status}${probe.fetch_error ? `, ${probe.fetch_error}` : ""}).\n` +
      `[ads] The request shape has likely changed. Re-capture a real request with ` +
      `Playwright before trusting any result -- see docs/ARCHITECTURE.md.`
    );
    process.exit(1);
  }
  console.error(`[ads] endpoint check ok: ${PROBE_DOMAIN} -> ${probe.creative_count} creatives`);
  if (args.probe) return;

  // Resumability. A prior row is skipped unless --recheck, or unless --stale-days says
  // it is old enough to be worth asking again: ad activity changes, so a result from
  // three months ago is not evidence about today.
  let skipClause = "";
  if (!recheck) {
    skipClause = staleDays
      ? `AND NOT EXISTS (SELECT 1 FROM leads.ads_transparency t
                         WHERE t.business_id = b.id AND t.http_status = 200
                           AND t.checked_at > now() - interval '${staleDays} days')`
      : `AND NOT EXISTS (SELECT 1 FROM leads.ads_transparency t
                         WHERE t.business_id = b.id AND t.http_status = 200)`;
  }

  const rows = JSON.parse(q(`
    SELECT coalesce(json_agg(t), '[]')
    FROM (
      SELECT b.id, b.name, b.website, b.icp_tier, b.marketing_active
      FROM leads.businesses b
      WHERE ${buildWhere(args)}
      ${skipClause}
      ORDER BY b.icp_score DESC, b.review_count DESC NULLS LAST
      ${limit ? `LIMIT ${limit}` : ""}
    ) t;
  `, { env, args: ["-tA"] }).trim());

  // One query per business, but several rows can share a domain (chains, and duplicate
  // records that survived QC). Deduplicating by domain means the shared answer is
  // fetched once and written to every business holding it.
  const byDomain = new Map();
  let unparseable = 0;
  for (const r of rows) {
    const d = toDomain(r.website);
    if (!d) { unparseable++; continue; }
    if (!byDomain.has(d)) byDomain.set(d, []);
    byDomain.get(d).push(r);
  }

  const domains = [...byDomain.keys()];
  if (domains.length === 0) {
    console.error("[ads] nothing to check (all matching rows already have a result)");
    return;
  }

  console.error(
    `[ads] ${rows.length} businesses -> ${domains.length} distinct domains` +
    (unparseable ? ` (${unparseable} unparseable website values skipped)` : "") +
    `, ${proxies.length} proxies, concurrency ${concurrency}` +
    (dryRun ? "  (DRY RUN -- nothing written)" : "")
  );

  const queue = [...domains];
  let pending = [];
  const samples = [];
  const stats = { done: 0, advertising: 0, recent: 0, none: 0, errors: 0, throttled: 0,
                  written: 0, failedBatches: 0, newVsPixel: 0, pixelNoAds: 0 };
  const started = Date.now();

  // Circuit breaker. Google rate-limits the whole pool at once rather than per IP -- a
  // first full run tripped it and then spent hundreds of further requests collecting
  // /sorry/ pages, because nothing was watching. Once THROTTLE_LIMIT consecutive
  // throttles are seen, the run stops: every subsequent answer would be meaningless,
  // and continuing deepens the block. Rotating proxies does not help, since the limit
  // is applied across the pool.
  const THROTTLE_LIMIT = 12;
  let consecutiveThrottles = 0;
  let aborted = false;

  async function flush() {
    const batch = pending;
    pending = [];
    if (!batch.length) return;
    try {
      persist(batch, env);
      stats.written += batch.length;
    } catch (err) {
      // Never abort the run for one bad batch: the rest is still worth checking, and
      // (business_id, domain) keying means a re-run picks up what did not land.
      stats.failedBatches++;
      console.error(`[ads] batch of ${batch.length} rows failed: ${err.message}`);
    }
  }

  async function worker() {
    while (queue.length && !aborted) {
      const domain = queue.shift();
      const bizList = byDomain.get(domain);
      const res = await checkDomain(domain, nextProxy);
      stats.done++;

      // A throttled request asked Google nothing, so it is not evidence about this
      // business and must not be stored -- a stored error row would otherwise make the
      // domain look permanently checked and a resumed run would skip it forever.
      if (res.throttled) {
        stats.throttled++;
        if (++consecutiveThrottles >= THROTTLE_LIMIT) {
          aborted = true;
          console.error(
            `\n[ads] ABORTING: ${THROTTLE_LIMIT} consecutive rate-limit redirects -- the ` +
            `proxy pool is throttled by Google.\n` +
            `[ads] Nothing further would be meaningful. Results so far are saved; re-run ` +
            `later to resume (already-checked domains are skipped).`
          );
        }
        continue;
      }
      consecutiveThrottles = 0;

      if (!res.ok) stats.errors++;
      else if (res.found) {
        stats.advertising++;
        if (isRecent(res.last_shown)) stats.recent++;
        // The headline comparison: how often this contradicts the pixel flag.
        if (!bizList[0].marketing_active) stats.newVsPixel++;
        if (samples.length < 15) {
          const days = res.last_shown
            ? Math.round((Date.now() - Date.parse(res.last_shown)) / 86400000) : null;
          samples.push(
            `  ${String(res.creative_count).padStart(3)} creatives  last shown ` +
            `${days === null ? "unknown" : `${days}d ago`}`.padEnd(28) +
            `${bizList[0].marketing_active ? "pixel " : "NO-PIX"}  ${bizList[0].name} (${domain})`
          );
        }
      } else {
        stats.none++;
        if (bizList[0].marketing_active) stats.pixelNoAds++;
      }

      // One answer, written to every business sharing the domain.
      for (const biz of bizList) pending.push({ ...res, business_id: biz.id, domain });

      if (stats.done % 50 === 0) {
        const rate = stats.done / ((Date.now() - started) / 1000);
        console.error(
          `[ads] ${stats.done}/${domains.length}  advertising=${stats.advertising} ` +
          `none=${stats.none} err=${stats.errors}  stored=${stats.written}  ${rate.toFixed(1)}/s`
        );
      }
      if (!dryRun && pending.length >= FLUSH_EVERY) await flush();
      // Pacing. The first full run went flat out at ~7 domains/s and tripped Google's
      // limiter partway through; the throughput was never the constraint, since even
      // 3/s finishes the whole database in under ten minutes. Slower and complete beats
      // faster and blocked.
      if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    }
  }

  await Promise.all(Array.from({ length: concurrency }, worker));
  if (!dryRun) await flush();

  const secs = ((Date.now() - started) / 1000).toFixed(0);
  const pct = (n) => `${((100 * n) / Math.max(stats.done - stats.errors, 1)).toFixed(0)}%`;
  console.error(
    `\n[ads] checked ${stats.done} domains in ${secs}s  ` +
    `(${stats.errors} errors, not written as "no ads")` +
    (stats.throttled ? `  [${stats.throttled} rate-limited, not stored]` : "")
  );
  console.error(
    `[ads] advertising=${stats.advertising} (${pct(stats.advertising)})  ` +
    `of those, shown within ${RECENT_DAYS}d=${stats.recent}  ` +
    `no ads=${stats.none}`
  );
  // The point of the whole pass, stated in the numbers that justify it.
  console.error(
    `[ads] vs the pixel flag: ${stats.newVsPixel} confirmed advertisers had NO ` +
    `marketing_active pixel; ${stats.pixelNoAds} had a pixel but run no ads`
  );
  if (samples.length) {
    console.error(`\n[ads] samples:`);
    for (const s of samples) console.error(s);
  }
  if (dryRun) {
    console.error("\n[ads] --dry-run: nothing written.");
    return;
  }
  console.error(
    `\n[ads] stored ${stats.written} evidence rows` +
    (stats.failedBatches ? `  (${stats.failedBatches} batch(es) failed -- see above)` : "")
  );
  // Exit non-zero on a throttle abort so a scheduled or piped run cannot report success
  // while having checked only part of the database.
  if (aborted) process.exit(2);
}

main().catch((err) => {
  console.error(`[ads] ${err.message}`);
  process.exit(1);
});
