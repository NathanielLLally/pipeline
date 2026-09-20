#!/usr/bin/env node
// Phase B enrichment (Prompt 7): crawls prospect websites for marketing, booking and
// growth signals, plus the about/team pages that Phase C reads for decision makers.
//
// Per business: the homepage, then up to MAX_SUB internal links whose URL looks like
// about/team/contact/pricing. Everything fetched lands in leads.website_crawl keyed on
// (business_id, url), so a second run skips what it already has and only the failures
// and the newly-added businesses cost anything.
//
// Fetches go through the Webshare SOCKS5 pool via curl. Node's own fetch() has no
// SOCKS support without a dependency, and this repo has no package.json by design.
//
// What marketing_active means here: PAID ACQUISITION INFRASTRUCTURE IS PRESENT. The
// column is set only on a confirmed ad pixel (AW- conversion id, fbevents.js), never
// on analytics -- but a pixel proves the business *configured* paid acquisition, not
// that it is *spending today*. A pixel left over from a six-week campaign two years ago
// is byte-identical to one backing $8k/month. Do not phrase this column as "running
// ads" in any downstream copy, export or campaign segment. Confirming current spend
// needs a second source; see the Ads Transparency note in docs/ARCHITECTURE.md.
//
// Scope note, from the measurements in docs/ARCHITECTURE.md: about/team pages yield a
// decision maker on ~33% of top-tier trainer sites but only ~7% across a random sample,
// because grooming sites largely have no about page. --tier and --service-category
// exist so the expensive pass can be pointed at the rows where it pays.
//
// Usage:
//   node scripts/enrich-websites.mjs --dry-run --limit 20
//   node scripts/enrich-websites.mjs --service-category dog_training --tier "Tier 1,Tier 2"
//   node scripts/enrich-websites.mjs --limit 500 --concurrency 12
//   node scripts/enrich-websites.mjs --refetch      # ignore what is already crawled
//   node scripts/enrich-websites.mjs --proxy-mode rotating   # after a throttled run

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { loadProxies, rotator } from "./lib/proxies.mjs";
import { detectPage, aggregate } from "./lib/site-signals.mjs";

const execFileAsync = promisify(execFile);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const TIMEOUT_SEC = 25;
const MAX_SUB = 3;              // sub-pages per business, beyond the homepage
const MAX_BYTES = 4 << 20;      // a homepage past 4MB is a bundle, not content
const EXCERPT_CHARS = 20000;    // enough for an about page; Phase C truncates further

// Sub-pages worth the extra fetch, most valuable first. about/team come first because
// they are the only pages that name people.
const SUBPAGE_PATTERNS = [
  [/\/(?:about|our-story|who-we-are|meet|team|staff|trainers?|bios?)\b/i, "about"],
  [/\/(?:pricing|rates|packages|programs?|services|tuition)\b/i, "pricing"],
  [/\/(?:contact|book|schedule|consultation|get-started)\b/i, "contact"],
];

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
 * Two hazards, both learned the hard way. Postgres text cannot store \u0000, and
 * jsonb rejects the escape rather than dropping it, aborting the transaction -- so
 * control characters are stripped from the encoded JSON. And a page containing the
 * literal delimiter would end the quoted string early, so the delimiter is removed too.
 */
function jsonLiteral(value) {
  return JSON.stringify(value)
    .replace(/\\u000[0-8bcef]|\\u001[0-9a-f]/gi, " ")
    .replace(/\$e\$/g, "");
}

/**
 * Strips tags and script/style bodies. Feeds text_excerpt, which Phase C reads.
 *
 * mailto: addresses are lifted out and prefixed onto the text first. An address that
 * appears ONLY as <a href="mailto:erin@x.com">Email us</a> -- the normal shape of a
 * contact page -- is otherwise destroyed by the tag strip below, and invisible to
 * extract-emails.mjs, which reads this text and not the HTML. That cost 496 businesses
 * their contact address on the first full crawl.
 *
 * Prefixed rather than appended because the caller truncates at EXCERPT_CHARS, and a
 * suffix on a long page would be cut off.
 */
export function toText(html) {
  const mailtos = new Set();
  for (const m of html.matchAll(/mailto:\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24})/gi)) {
    mailtos.add(m[1].toLowerCase());
  }
  // Capped: a staff-directory page can carry dozens, and the excerpt budget belongs to
  // the prose that Phase C reads for names.
  const prefix = mailtos.size ? `${[...mailtos].slice(0, 10).join(" ")} ` : "";

  return prefix + html
    // NUL and the other C0 controls first. Postgres text cannot store \u0000 at all --
    // it rejects the value with "unsupported Unicode escape sequence" and takes the
    // whole transaction with it, which silently discarded a completed 1,542-site crawl.
    // Real pages do contain them, usually from mis-decoded bytes in review text.
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&(?:#39|rsquo|apos);/g, "'")
    .replace(/&(?:quot|ldquo|rdquo);/g, '"')
    .replace(/\s+/g, " ")
    .trim();
}

// Markers of a block, challenge or error page. Matched against the *extracted text*,
// where such a page says what it is, rather than against markup.
//
// "Page not found" is included even though a 404 body can be long: a themed 404 carries
// the site's whole navigation chrome, which is bulk, not content. Such a page may still
// hold a header mailto: address, but it is not evidence the URL is live, and recording
// it as a successful crawl would stop the real page ever being fetched.
const BLOCK_MARKERS =
  /(?:\b40[0-9]\b[\s-]*(?:forbidden|unauthorized|bad request|not found|page)|\bpage not found\b|\bnot found\b|\b404 page\b|dns resolution error|please enable cookies|error 10\d\d\b|ray id:|forbidden|unauthorized|access (?:to this page )?is (?:forbidden|denied)|access denied|attention required|checking your browser|just a moment|ddos protection|request blocked|are you a robot|captcha|security check|verify you are human|not acceptable|service unavailable|site can'?t be reached|account suspended|website firewall|not configured|domain (?:for sale|is parked)|under construction)/i;

// A real business homepage has prose. An error page, however elaborately styled, does
// not -- the observed nginx/WordPress 403 stubs render to about 87 characters once tags
// are stripped, while a genuine salvaged page runs to thousands.
const MIN_SALVAGE_TEXT = 400;

/**
 * Whether a response carries usable page content.
 *
 * Status alone is the wrong test: some sites -- WordPress behind a caching proxy --
 * answer GET with 403 while returning their complete homepage, title, contact details
 * and all. But the opposite is far more common, and the two are indistinguishable by
 * status or by body size, because a styled error page carries plenty of markup.
 *
 * So the test for a non-2xx/3xx response is made on the *extracted text*: substantial
 * prose, and no error/challenge phrasing in its opening. Checking raw body length
 * instead let 230 nginx 403 stubs through in one run -- each over 500 bytes of markup
 * and 87 characters of text -- and recorded them as successfully crawled pages.
 *
 * The status is still stored verbatim, so a later pass can distinguish a clean 200
 * from a salvaged 403.
 */
export function usableBody(res, text = null) {
  if (!res.body || res.body.length <= 500) return false;
  if (res.status >= 200 && res.status < 400) return true;

  const t = text ?? toText(res.body);
  if (t.length < MIN_SALVAGE_TEXT) return false;
  return !BLOCK_MARKERS.test(t.slice(0, 600));
}

/**
 * Fetch with retries through *different* proxies.
 *
 * One attempt through one IP is not evidence about a site. Two back-to-back runs over
 * the same 1,542 businesses returned 1,289 reachable and then 624, because the pool had
 * just served 4,000 requests and was being rate-limited -- the per-chunk failure rate
 * spiked to ~90% mid-run and recovered by the end, which is the shape of throttling,
 * not of sites going down. Retrying through a fresh proxy is what makes "unreachable"
 * mean something about the business rather than about the pool's recent history.
 *
 * Retries only on transport failure and on the status codes that indicate the proxy
 * rather than the page: 403/429 (blocked or throttled) and 5xx. A 404 is a real answer.
 */
async function fetchWithRetry(url, nextProxy, attempts = 3) {
  let last;
  for (let i = 0; i < attempts; i++) {
    last = await fetchOnce(url, nextProxy);
    const retryable = last.status === 0 || last.status === 403 ||
                      last.status === 429 || last.status >= 500;
    if (!retryable) return last;
    // Brief, growing pause so a throttled pool is not hammered on the way out.
    if (i < attempts - 1) await new Promise((r) => setTimeout(r, 400 * (i + 1)));
  }
  return last;
}

/**
 * One request through the next proxy in rotation.
 *
 * Credentials go in --proxy-user, never in the proxy URL: curl 8.20 rejects the
 * embedded form, and it also prints the whole URL back on failure, which has put live
 * proxy credentials into a transcript once already.
 */
async function fetchOnce(url, nextProxy) {
  const p = nextProxy();
  const args = [
    "-sS", "--compressed", "-L", "--max-redirs", "5",
    "--max-time", String(TIMEOUT_SEC),
    "--max-filesize", String(MAX_BYTES),
    "--socks5-hostname", `${p.host}:${p.port}`,
    "--proxy-user", `${p.user}:${p.pass}`,
    "-A", UA,
    "-w", "\n@@%{http_code}\t%{url_effective}",
    url,
  ];
  try {
    const { stdout } = await execFileAsync("/usr/bin/curl", args, {
      encoding: "utf8", maxBuffer: MAX_BYTES + (1 << 20),
    });
    const i = stdout.lastIndexOf("\n@@");
    if (i < 0) return { status: 0, error: "no status marker", body: "", finalUrl: url };
    const [code, effective] = stdout.slice(i + 3).split("\t");
    return {
      status: Number(code) || 0,
      body: stdout.slice(0, i),
      finalUrl: (effective || url).trim(),
      error: null,
    };
  } catch (err) {
    // curl's stderr can name the proxy host; the password is not in it because of
    // --proxy-user, but keep the note short rather than storing curl's whole complaint.
    const raw = (err.stderr || "").toString();
    const m = raw.match(/curl:\s*\(\d+\)\s*(.{0,90})/);
    return { status: 0, body: "", finalUrl: url, error: m ? m[1].trim() : "fetch failed" };
  }
}

/** Internal links worth following, deduplicated and capped. */
function pickSubpages(html, baseUrl) {
  let origin;
  try { origin = new URL(baseUrl).origin; } catch { return []; }

  const seen = new Map();
  for (const m of html.matchAll(/href\s*=\s*["']([^"']+)["']/gi)) {
    const href = m[1];
    if (/^(?:mailto:|tel:|javascript:|#)/i.test(href)) continue;
    if (/\.(?:jpe?g|png|gif|webp|svg|pdf|css|js|zip|mp4|ico)(?:$|\?)/i.test(href)) continue;

    let abs;
    try { abs = new URL(href, baseUrl); } catch { continue; }
    if (abs.origin !== origin) continue;

    abs.hash = "";
    const key = abs.href.replace(/\/$/, "");
    if (seen.has(key)) continue;

    for (const [re, kind] of SUBPAGE_PATTERNS) {
      if (re.test(abs.pathname)) { seen.set(key, { url: abs.href, kind }); break; }
    }
  }
  // Ordered by SUBPAGE_PATTERNS priority so a site with ten candidate pages still
  // gets its about page fetched rather than three pricing pages.
  const order = ["about", "pricing", "contact"];
  return [...seen.values()]
    .sort((a, b) => order.indexOf(a.kind) - order.indexOf(b.kind))
    .slice(0, MAX_SUB);
}

/**
 * Writes one batch: crawl evidence, then the derived columns on businesses.
 *
 * Kept as a single transaction per batch so the two halves never disagree -- a
 * marketing_score on a business always has the crawl rows behind it on record.
 */
function persist(crawlBatch, updateBatch, env) {
  if (crawlBatch.length) {
    // ON CONFLICT rather than plain INSERT so a --refetch updates in place; that is
    // what the (business_id, url) unique index is for. Note the payload must not
    // contain the same (business_id, url) twice -- Postgres refuses to update a row
    // twice in one statement -- which crawlBusiness() guarantees per business.
    q(`
      BEGIN;
      CREATE TEMP TABLE crawl_payload (data jsonb) ON COMMIT DROP;
      INSERT INTO crawl_payload VALUES ($e$${jsonLiteral(crawlBatch)}$e$::jsonb);
      INSERT INTO leads.website_crawl
        (business_id, url, final_url, page_kind, http_status, fetch_error, content_bytes, signals, text_excerpt)
      SELECT (c->>'business_id')::uuid, c->>'url', c->>'final_url', c->>'page_kind',
             (c->>'http_status')::int, c->>'fetch_error', (c->>'content_bytes')::int,
             coalesce(c->'signals', '{}'::jsonb), c->>'text_excerpt'
      FROM crawl_payload p, LATERAL jsonb_array_elements(p.data) c
      ON CONFLICT (business_id, url) DO UPDATE SET
        final_url = EXCLUDED.final_url, page_kind = EXCLUDED.page_kind,
        http_status = EXCLUDED.http_status, fetch_error = EXCLUDED.fetch_error,
        content_bytes = EXCLUDED.content_bytes, signals = EXCLUDED.signals,
        text_excerpt = EXCLUDED.text_excerpt, fetched_at = now();
      COMMIT;
    `, { env });
  }

  if (updateBatch.length) {
    // The crawl observed the site directly, so its booleans are authoritative and
    // overwrite. booking_present is the exception: it is COALESCEd because Phase A
    // may have found a booking link in raw_scrape that the homepage does not show,
    // and a null from this pass means "not seen", not "absent".
    q(`
      BEGIN;
      CREATE TEMP TABLE mk_payload (data jsonb) ON COMMIT DROP;
      INSERT INTO mk_payload VALUES ($e$${jsonLiteral(updateBatch)}$e$::jsonb);
      UPDATE leads.businesses b
      SET marketing_active   = (u->>'marketing_active')::boolean,
          google_ads_signal  = (u->>'google_ads_signal')::boolean,
          meta_ads_signal    = (u->>'meta_ads_signal')::boolean,
          lead_form_present  = (u->>'lead_form_present')::boolean,
          booking_present    = COALESCE((u->>'booking_present')::boolean, b.booking_present),
          marketing_score    = (u->>'marketing_score')::int,
          marketing_evidence = COALESCE(u->>'marketing_evidence', b.marketing_evidence),
          date_updated       = now()
      FROM mk_payload p, LATERAL jsonb_array_elements(p.data) u
      WHERE b.id = (u->>'id')::uuid;
      COMMIT;
    `, { env });
  }
}

function normalizeUrl(website) {
  const raw = String(website).trim();
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  try { return new URL(withScheme).href; } catch { return null; }
}

async function crawlBusiness(biz, nextProxy, alreadyCrawled) {
  const home = normalizeUrl(biz.website);
  if (!home) return { biz, crawls: [], skipped: "unparseable url" };

  const crawls = [];
  const pages = [];

  if (alreadyCrawled.has(home)) return { biz, crawls: [], skipped: "already crawled" };

  const res = await fetchWithRetry(home, nextProxy);
  // Extract once and hand it to usableBody, so the salvage decision is made on exactly
  // the text that will be stored.
  const homeText = res.body ? toText(res.body).slice(0, EXCERPT_CHARS) : null;
  const ok = usableBody(res, homeText);
  crawls.push({
    business_id: biz.id,
    url: home,
    final_url: res.finalUrl,
    page_kind: "home",
    http_status: res.status,
    fetch_error: res.error,
    content_bytes: Buffer.byteLength(res.body),
    signals: ok ? detectPage(res.body, res.finalUrl) : {},
    text_excerpt: ok ? homeText : null,
  });
  if (!ok) return { biz, crawls, pages: [] };
  pages.push(crawls[0].signals);

  // The GBP "website" field sometimes points straight at /about, in which case that
  // page is both the homepage and a discovered subpage. Inserting it twice in one
  // statement trips "ON CONFLICT DO UPDATE cannot affect row a second time", which
  // aborts the batch -- so the requested URLs are deduplicated per business here.
  const requested = new Set([home]);
  for (const sub of pickSubpages(res.body, res.finalUrl)) {
    if (alreadyCrawled.has(sub.url) || requested.has(sub.url)) continue;
    requested.add(sub.url);
    const r = await fetchWithRetry(sub.url, nextProxy);
    const subText = r.body ? toText(r.body).slice(0, EXCERPT_CHARS) : null;
    const sok = usableBody(r, subText);
    crawls.push({
      business_id: biz.id,
      url: sub.url,
      final_url: r.finalUrl,
      page_kind: sub.kind,
      http_status: r.status,
      fetch_error: r.error,
      content_bytes: Buffer.byteLength(r.body),
      signals: sok ? detectPage(r.body, r.finalUrl) : {},
      text_excerpt: sok ? subText : null,
    });
    if (sok) pages.push(crawls[crawls.length - 1].signals);
  }

  return { biz, crawls, pages };
}

function buildWhere(args) {
  const where = [
    "website IS NOT NULL",
    "website <> ''",
    // A GBP "website" pointing at a Facebook page is not a site to crawl -- Meta
    // returns 400 to this proxy pool's ASN. 26 of 3,883 rows are like this.
    "website !~* '(facebook\\.com|instagram\\.com|nextdoor\\.com|linktr\\.ee)'",
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
  const concurrency = args.concurrency ? Number(args.concurrency) : 10;
  const refetch = Boolean(args.refetch);

  const env = loadEnv(ROOT);

  const rows = JSON.parse(q(`
    SELECT coalesce(json_agg(t), '[]')
    FROM (
      SELECT id, name, website, icp_tier, icp_score, service_category
      FROM leads.businesses
      WHERE ${buildWhere(args)}
      ORDER BY icp_score DESC, review_count DESC NULLS LAST
      ${limit ? `LIMIT ${limit}` : ""}
    ) t;
  `, { env, args: ["-tA"] }).trim());

  if (rows.length === 0) {
    console.error("[enrich-websites] no businesses match the filter");
    return;
  }

  // Resumability: one query for every URL whose CONTENT is already on record, so a
  // re-run costs nothing for the pages it has. Keyed on text_excerpt rather than on
  // status: a salvaged 403 has content and should be skipped, while a 403 stored back
  // when the status gate discarded its body has none and must be fetched again.
  // --refetch skips this and re-fetches everything.
  const crawled = new Map();
  if (!refetch) {
    const prior = JSON.parse(q(`
      SELECT coalesce(json_agg(t), '[]')
      FROM (SELECT business_id, url FROM leads.website_crawl
            WHERE text_excerpt IS NOT NULL) t;
    `, { env, args: ["-tA"] }).trim());
    for (const p of prior) {
      if (!crawled.has(p.business_id)) crawled.set(p.business_id, new Set());
      crawled.get(p.business_id).add(p.url);
    }
  }

  // "direct" is the fixed 100-IP list; "rotating" is the gateway that assigns a fresh
  // exit per connection. Refetching the direct list does not change its contents, so
  // rotating is the only refresh that actually moves to different addresses.
  const proxyMode = args["proxy-mode"] === "rotating" ? "rotating" : "direct";
  const proxies = loadProxies(env, proxyMode);
  const nextProxy = rotator(proxies);
  console.error(
    `[enrich-websites] ${rows.length} businesses, ${proxies.length} ${proxyMode} proxies, ` +
    `concurrency ${concurrency}${dryRun ? " (DRY RUN -- nothing written)" : ""}`
  );

  const queue = [...rows];
  let allCrawls = [];
  let updates = [];
  const stats = { done: 0, reachable: 0, unreachable: 0, skipped: 0, pages: 0,
                  ads: 0, booking: 0, form: 0, about: 0, person_email: 0,
                  written: 0, failedBatches: 0 };
  const started = Date.now();

  // Flush every FLUSH_EVERY businesses rather than once at the end. A single closing
  // transaction means any rejected row discards the whole run: a NUL byte in one page
  // of one site, and nine minutes of crawling nine hundred sites is gone with nothing
  // to resume from. Both failures that happened here had exactly that shape. With
  // incremental flushes a bad batch costs one chunk, and because website_crawl is keyed
  // on (business_id, url), a re-run picks up from what already landed.
  const FLUSH_EVERY = 100;

  async function flush(final = false) {
    const crawlBatch = allCrawls;
    const updateBatch = updates;
    allCrawls = [];
    updates = [];
    if (crawlBatch.length === 0 && updateBatch.length === 0) return;
    try {
      persist(crawlBatch, updateBatch, env);
      stats.written += crawlBatch.length;
    } catch (err) {
      // Never abort the run for a bad batch: the remaining businesses are still worth
      // crawling, and the failure is reported rather than swallowed.
      stats.failedBatches++;
      console.error(`[enrich-websites] batch of ${crawlBatch.length} crawl rows failed: ${err.message}`);
    }
    if (final) console.error(`[enrich-websites] final flush complete`);
  }

  async function worker() {
    while (queue.length) {
      const biz = queue.shift();
      const seen = crawled.get(biz.id) || new Set();
      const { crawls, pages, skipped } = await crawlBusiness(biz, nextProxy, seen);
      stats.done++;
      if (skipped) { stats.skipped++; continue; }

      allCrawls.push(...crawls);
      stats.pages += crawls.length;

      if (!pages || pages.length === 0) { stats.unreachable++; continue; }
      stats.reachable++;
      if (crawls.some((c) => c.page_kind === "about")) stats.about++;

      const agg = aggregate(pages);
      if (agg.marketing_active) stats.ads++;
      if (agg.booking_present) stats.booking++;
      if (agg.lead_form_present) stats.form++;
      if (agg.person_emails.length) stats.person_email++;

      updates.push({
        id: biz.id,
        marketing_active: agg.marketing_active,
        google_ads_signal: agg.google_ads_signal,
        meta_ads_signal: agg.meta_ads_signal,
        lead_form_present: agg.lead_form_present,
        booking_present: agg.booking_present,
        marketing_score: agg.marketing_score,
        marketing_evidence: agg.marketing_evidence,
      });

      if (stats.done % 25 === 0) {
        const rate = stats.done / ((Date.now() - started) / 1000);
        console.error(
          `[enrich-websites] ${stats.done}/${rows.length}  ` +
          `reachable=${stats.reachable} unreachable=${stats.unreachable} ` +
          `ads=${stats.ads} booking=${stats.booking}  stored=${stats.written}  ${rate.toFixed(1)}/s`
        );
      }
      if (!dryRun && allCrawls.length >= FLUSH_EVERY) await flush();
    }
  }

  await Promise.all(Array.from({ length: concurrency }, worker));

  const pct = (n) => `${((100 * n) / Math.max(stats.reachable, 1)).toFixed(0)}%`;
  console.error(
    `\n[enrich-websites] crawled ${stats.pages} pages across ${stats.done} businesses ` +
    `in ${((Date.now() - started) / 1000).toFixed(0)}s`
  );
  console.error(
    `[enrich-websites] reachable=${stats.reachable} unreachable=${stats.unreachable} ` +
    `skipped(already crawled)=${stats.skipped}`
  );
  console.error(
    `[enrich-websites] of reachable: paid-ad pixel=${stats.ads} (${pct(stats.ads)})  ` +
    `booking=${stats.booking} (${pct(stats.booking)})  lead form=${stats.form} (${pct(stats.form)})  ` +
    `about page=${stats.about} (${pct(stats.about)})  person-shaped email=${stats.person_email} (${pct(stats.person_email)})`
  );

  if (dryRun) {
    console.error("\n[enrich-websites] --dry-run: nothing written. Samples:");
    for (const u of updates.filter((x) => x.marketing_evidence).slice(0, 8)) {
      const name = rows.find((r) => r.id === u.id)?.name;
      console.error(`  ${String(u.marketing_score).padStart(3)}  ${name} -- ${u.marketing_evidence.slice(0, 110)}`);
    }
    return;
  }

  await flush(true);
  console.error(
    `[enrich-websites] stored ${stats.written} crawl rows` +
    (stats.failedBatches ? `  (${stats.failedBatches} batch(es) failed -- see above)` : "")
  );
}

// Only crawl when run as a script. toText is exported for its regression test, and
// importing this module must not launch a crawl as a side effect.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(`[enrich-websites] ${err.message}`);
    process.exit(1);
  });
}
