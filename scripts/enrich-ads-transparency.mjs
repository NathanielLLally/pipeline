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
//
// Transport: scrape.do (residential) is used automatically when SCRAPEDO_TOKEN is set,
// because Google blocks the Webshare datacenter ASN on this endpoint. It BILLS 10
// credits per request, so spend is capped:
//   node scripts/enrich-ads-transparency.mjs --credit-budget 950 --tier "Tier 1"
//   node scripts/enrich-ads-transparency.mjs --no-scrapedo   # back to the free pool
//
// DATAIMPULSE_PROXY, when set, wins over scrape.do and has NO credit cap: it bills per
// gigabyte and one response is ~3KB, so the whole database costs a few megabytes.
//   node scripts/enrich-ads-transparency.mjs --no-residential   # skip it for a test

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
const SCRAPEDO_TIMEOUT_SEC = 90;   // residential exits are much slower than datacenter

// scrape.do budget. `super=true` is residential and bills 10 credits per request --
// measured, not assumed, from the Scrape.do-Request-Cost header. The account in use
// holds 1,000 credits total, i.e. ~100 requests, against ~2,099 unchecked domains, so
// the budget is the binding constraint and the guard below is the point of this
// transport rather than a nicety. The default is deliberately far below the balance:
// spending the whole account in one unattended run is not something to do by accident.
const DEFAULT_CREDIT_BUDGET = 300;

// What one super=true request bills, used to decide whether the NEXT call fits inside
// the budget. The actual charge always comes from the Scrape.do-Request-Cost header --
// this is only the look-ahead, and it is deliberately the observed price rather than an
// optimistic one so the cap cannot be overshot by a request that turns out dearer.
const PER_REQUEST_ESTIMATE = 10;
const MAX_BYTES = 8 << 20;   // a heavy advertiser's page of 40 creatives runs ~25KB
// Deliberately small. On the free pool a lost batch costs only time, but a scrape.do
// request costs real credits, so results are checkpointed often enough that a crash
// cannot throw away work that was paid for.
const FLUSH_EVERY = 25;

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

// Set from SCRAPEDO_TOKEN in main(); when present, every fetch goes through scrape.do's
// residential network instead of the Webshare pool. Module-scoped so fetchOnce keeps the
// same signature for both transports.
let scrapedoToken = null;

// Set from DATAIMPULSE_PROXY in main(). A residential SOCKS5 gateway billed per GIGABYTE
// rather than per request, which is the right shape for this pass: a measured response is
// ~2.9KB gzipped, so the whole remaining database is single-digit megabytes. Preferred
// over scrape.do whenever it is configured, since scrape.do bills 10 credits for the same
// ~3KB. Parsed once into {host, port, user, pass} so curl gets the credentials via
// --proxy-user and they never enter a URL or argv-visible proxy string.
let residentialProxy = null;

/** Splits a socks5://user:pass@host:port URL into curl's pieces, or null if unset. */
function parseResidential(raw) {
  if (!raw) return null;
  const u = new URL(String(raw).trim());
  return {
    host: u.hostname,
    port: u.port || "1080",
    // The username carries DataImpulse's targeting tag (e.g. `__cr.us` = US exits) and
    // is percent-encoded in the URL form, so it must be decoded before curl sees it.
    user: decodeURIComponent(u.username),
    pass: decodeURIComponent(u.password),
  };
}

/** curl argv for the DataImpulse residential gateway. */
function residentialArgs(domain, p) {
  return [
    "-sS", "-X", "POST", "--compressed",
    // Residential exits are slower than datacenter ones; the same 90s allowance the
    // scrape.do path needed.
    "--max-time", String(SCRAPEDO_TIMEOUT_SEC),
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
}

/** curl argv for the Webshare SOCKS5 pool (the original, free transport). */
function poolArgs(domain, p) {
  return [
    "-sS", "-X", "POST", "--compressed",
    "--max-time", String(TIMEOUT_SEC),
    "--max-filesize", String(MAX_BYTES),
    "--socks5-hostname", `${p.host}:${p.port}`,
    // Credentials go in --proxy-user, never in the proxy URL: curl 8.20 rejects the
    // embedded form and echoes the whole URL back on failure, which has put live proxy
    // credentials into a transcript once already.
    "--proxy-user", `${p.user}:${p.pass}`,
    "-H", "content-type: application/x-www-form-urlencoded",
    "-H", "x-same-domain: 1",
    "-H", `user-agent: ${UA}`,
    "-H", "referer: https://adstransparency.google.com/",
    "--data-raw", requestBody(domain),
    "-w", "\n@@%{http_code}",
    ENDPOINT,
  ];
}

/**
 * curl argv for scrape.do's residential network.
 *
 * Used because Google blocks the Webshare datacenter ASN on this endpoint outright --
 * measured at 12 of 12 proxies redirected to /sorry, persisting over two hours, while a
 * direct request from the workstation returned 200. The block is by ASN, not by volume.
 *
 * `super=true` selects residential/mobile exits, which is the whole point (plain
 * datacenter mode is the thing Google already refuses) and costs **10 credits per
 * request** rather than 1. With `extraHeaders=true`, headers meant for the TARGET are
 * prefixed `Sd-`; unprefixed ones would configure scrape.do itself and never reach
 * Google.
 *
 * The token is a URL parameter, so it is a credential sitting in argv. It is read from
 * SCRAPEDO_TOKEN and never logged: the error path below strips it before any message is
 * stored or printed.
 */
function scrapedoArgs(domain) {
  const target = encodeURIComponent(ENDPOINT);
  const url = `https://api.scrape.do/?url=${target}&token=${scrapedoToken}` +
              `&super=true&extraHeaders=true`;
  return [
    "-sS", "-X", "POST",
    // Residential exits are slower than datacenter ones and this endpoint is doing real
    // work behind the proxy; 30s was tight enough to lose otherwise-good requests.
    "--max-time", String(SCRAPEDO_TIMEOUT_SEC),
    "--max-filesize", String(MAX_BYTES),
    "-H", "content-type: application/x-www-form-urlencoded",
    "-H", "Sd-content-type: application/x-www-form-urlencoded",
    "-H", "Sd-x-same-domain: 1",
    "-H", `Sd-user-agent: ${UA}`,
    "-H", "Sd-referer: https://adstransparency.google.com/",
    "--data-raw", requestBody(domain),
    // The cost header is the authoritative per-request price and is what the budget
    // guard counts; -D - puts the headers in the same stream, ahead of the body.
    "-D", "-",
    "-w", "\n@@%{http_code}",
    url,
  ];
}

/** Human-readable name of the transport actually in use, for the run's log lines. */
function transportName(useScrapedo) {
  if (residentialProxy) return "dataimpulse (residential, per-GB)";
  return useScrapedo ? "scrape.do (residential)" : "webshare pool";
}

/** Removes the scrape.do token from anything that might be printed or stored. */
function scrub(text) {
  if (!text) return text;
  let out = String(text);
  if (scrapedoToken) out = out.split(scrapedoToken).join("<token>");
  // curl can echo the proxy host on failure. The password is kept out of argv by
  // --proxy-user, but scrub it anyway: this string is written to fetch_error in the
  // database, and a credential that reaches a stored column is hard to recall.
  if (residentialProxy?.pass) out = out.split(residentialProxy.pass).join("<proxy-pass>");
  return out;
}

/**
 * One POST, through whichever transport is configured.
 */
async function fetchOnce(domain, nextProxy) {
  // Preference order: residential-per-GB, then scrape.do-per-credit, then the free
  // datacenter pool. The pool is last because Google blocks its ASN outright on this
  // endpoint; it stays wired up only so --no-scrapedo remains usable if that changes.
  const args = residentialProxy
    ? residentialArgs(domain, residentialProxy)
    : scrapedoToken
      ? scrapedoArgs(domain)
      : poolArgs(domain, nextProxy());
  try {
    const { stdout } = await execFileAsync("/usr/bin/curl", args, {
      encoding: "utf8", maxBuffer: MAX_BYTES + (1 << 20),
    });
    const i = stdout.lastIndexOf("\n@@");
    if (i < 0) return { status: 0, body: "", error: "no status marker" };
    const status = Number(stdout.slice(i + 3)) || 0;
    let body = stdout.slice(0, i);
    let cost = 0;
    let remaining = null;

    if (scrapedoToken) {
      // -D - prepended the response headers. Split them off, and read the authoritative
      // cost and balance before handing the body on.
      const sep = body.search(/\r?\n\r?\n/);
      if (sep >= 0) {
        const head = body.slice(0, sep);
        body = body.replace(/^[\s\S]*?\r?\n\r?\n/, "");
        const c = head.match(/scrape\.do-request-cost:\s*(\d+)/i);
        const r = head.match(/scrape\.do-remaining-credits:\s*(\d+)/i);
        if (c) cost = Number(c[1]);
        if (r) remaining = Number(r[1]);
        // scrape.do reports the TARGET's status separately; its own 200 only means the
        // proxy call succeeded. Google's /sorry redirect arrives here.
        const init = head.match(/scrape\.do-initial-status-code:\s*(\d+)/i);
        if (init && Number(init[1]) === 302) {
          return { status: 302, body: "", error: "rate-limited (/sorry)", throttled: true, cost, remaining };
        }
      }
    }

    // A 302 here is Google's rate limiter: it redirects to /sorry/index rather than
    // returning 429. Flagged distinctly because it says nothing about the business --
    // it means the TRANSPORT is throttled, and storing it as an ordinary error would
    // record a permanent "no result" for a domain that was never actually asked about.
    if (status === 302) return { status, body: "", error: "rate-limited (/sorry)", throttled: true, cost, remaining };
    return { status, body, error: null, cost, remaining };
  } catch (err) {
    // curl's stderr can name the proxy host; the Webshare password is not in it thanks
    // to --proxy-user, but the scrape.do token IS in the URL, so scrub before storing.
    const raw = (err.stderr || "").toString();
    const m = raw.match(/curl:\s*\(\d+\)\s*(.{0,90})/);
    return { status: 0, body: "", error: scrub(m ? m[1].trim() : "fetch failed"), cost: 0, remaining: null };
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
  let spent = 0;
  for (let i = 0; i < attempts; i++) {
    last = await fetchOnce(domain, nextProxy);
    spent += last.cost || 0;
    // Retries are NOT free on scrape.do -- each attempt is billed, so a 3x retry on a
    // paid transport silently triples the budget. One attempt only when paying.
    // Per-GB and pool transports may retry freely; only scrape.do bills per attempt.
    const maxAttempts = !residentialProxy && scrapedoToken ? 1 : attempts;
    const retryable = last.status === 0 || last.status === 302 || last.status === 403 ||
                      last.status === 429 || last.status >= 500;
    if (!retryable || i >= maxAttempts - 1) break;
    await new Promise((r) => setTimeout(r, 500 * (i + 1)));
  }
  return { ...last, cost: spent };
}

/** Fetches one domain and decodes it, keeping transport and parse failures distinct. */
async function checkDomain(domain, nextProxy) {
  const res = await fetchWithRetry(domain, nextProxy);
  if (res.status !== 200) {
    return {
      ok: false, http_status: res.status,
      fetch_error: res.error || `http ${res.status}`,
      throttled: Boolean(res.throttled),
      cost: res.cost || 0, remaining: res.remaining ?? null,
    };
  }
  let json;
  try {
    json = JSON.parse(res.body);
  } catch {
    // A 200 that is not JSON means the shape of the endpoint changed, which is worth
    // recording loudly rather than counting as "no ads".
    return {
      ok: false, http_status: 200, fetch_error: "unparseable response",
      cost: res.cost || 0, remaining: res.remaining ?? null,
    };
  }
  return {
    ok: true, http_status: 200, fetch_error: null,
    ...parseCreatives(json),
    cost: res.cost || 0, remaining: res.remaining ?? null,
  };
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

  // Transport, in preference order. Google blocks the Webshare datacenter ASN on this
  // endpoint, so a residential exit is required; the only question is which one.
  // DATAIMPULSE_PROXY bills per gigabyte and a response is ~3KB, so it is far cheaper
  // than scrape.do's flat 10 credits for the same bytes and is preferred whenever set.
  // --no-residential and --no-scrapedo step down the chain for testing.
  residentialProxy = args["no-residential"] ? null : parseResidential(env.DATAIMPULSE_PROXY);
  const useScrapedo = !residentialProxy && Boolean(env.SCRAPEDO_TOKEN) && !args["no-scrapedo"];
  scrapedoToken = useScrapedo ? env.SCRAPEDO_TOKEN : null;

  // The credit budget is a HARD cap on spend, enforced against the cost header rather
  // than an assumed price per request, so a server-side price change cannot overrun it.
  const creditBudget = args["credit-budget"] !== undefined
    ? Number(args["credit-budget"])
    : DEFAULT_CREDIT_BUDGET;
  let creditsSpent = 0;
  let creditsRemaining = null;

  // The pool is only loaded when it will be used: loadProxies makes a network call, and
  // on the paid transport it is pure waste.
  let nextProxy = () => { throw new Error("proxy pool not loaded"); };
  let transport = useScrapedo
    ? `scrape.do residential, budget ${creditBudget} credits`
    : "dataimpulse residential (per-GB)";
  if (!useScrapedo && !residentialProxy) {
    const proxies = loadProxies(env, args["proxy-mode"] === "rotating" ? "rotating" : "direct");
    nextProxy = rotator(proxies);
    transport = `${proxies.length} proxies`;
  }

  // Endpoint check, always. `{}` from a small business is ambiguous; `{}` from a known
  // heavy advertiser means the request shape has broken and every result this run would
  // produce is a false negative. Cheap insurance against silently writing 1,200 wrong
  // answers.
  const probe = await checkDomain(PROBE_DOMAIN, nextProxy);
  creditsSpent += probe.cost || 0;
  if (probe.remaining !== null && probe.remaining !== undefined) creditsRemaining = probe.remaining;
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
  console.error(
    `[ads] endpoint check ok via ${transportName(useScrapedo)}: ` +
    `${PROBE_DOMAIN} -> ${probe.creative_count} creatives` +
    (useScrapedo ? `  [cost ${probe.cost}, ${creditsRemaining ?? "?"} credits left]` : "")
  );
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
    `, ${transport}, concurrency ${concurrency}` +
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
  // Set once when the credit cap is hit, so the message prints a single time rather
  // than once per worker.
  let budgetStopped = false;

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
      // Budget check before the request, not after: the cap must never be exceeded, and
      // the cost of the next call is known (10 credits on super). Checked inside the
      // loop so every worker sees the shared running total.
      if (useScrapedo && creditsSpent + PER_REQUEST_ESTIMATE > creditBudget) {
        if (!budgetStopped) {
          budgetStopped = true;
          console.error(
            `\n[ads] BUDGET REACHED: ${creditsSpent} of ${creditBudget} credits spent ` +
            `(~${Math.round(creditsSpent / PER_REQUEST_ESTIMATE)} requests).\n` +
            `[ads] Stopping cleanly; results so far are saved. Raise it with ` +
            `--credit-budget N to continue.`
          );
        }
        break;
      }

      const domain = queue.shift();
      const bizList = byDomain.get(domain);
      const res = await checkDomain(domain, nextProxy);
      creditsSpent += res.cost || 0;
      if (res.remaining !== null && res.remaining !== undefined) creditsRemaining = res.remaining;
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
          `none=${stats.none} err=${stats.errors}  stored=${stats.written}  ${rate.toFixed(1)}/s` +
          (useScrapedo ? `  credits=${creditsSpent}/${creditBudget}` : "")
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
  if (useScrapedo) {
    console.error(
      `[ads] credits: ${creditsSpent} spent this run, ` +
      `${creditsRemaining ?? "?"} remaining on the account` +
      (budgetStopped ? `  (stopped at the --credit-budget cap of ${creditBudget})` : "")
    );
  }
  // Exit non-zero on a throttle abort so a scheduled or piped run cannot report success
  // while having checked only part of the database.
  if (aborted) process.exit(2);
}

main().catch((err) => {
  console.error(`[ads] ${err.message}`);
  process.exit(1);
});
