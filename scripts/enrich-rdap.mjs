#!/usr/bin/env node
// Recovers contact addresses from domain registration records (RDAP).
//
// A different acquisition channel from the website crawl: this asks the registry who
// registered the domain, so it can reach a business whose own site publishes no
// address anywhere. That makes it worth running -- but only once, and with clear eyes
// about the yield.
//
// Measured on samples of the uncovered corpus before building the full pass, because
// the shape of the answer decides the shape of the code:
//
//   * Registries carry nothing. Over 142 domains, the registry's own RDAP answer
//     contained a contact entity exactly zero times -- .com and .net are thin
//     registries by design. ALL yield comes from a second hop to the registrar's own
//     RDAP server, which means a failed hop is an unanswered domain, not a miss.
//   * Yield is 3-4%, and it is not evenly spread. Registrars that bundle privacy
//     registration produce nothing at all: GoDaddy 0 found / 8 privacy, Namecheap
//     0 / 7, Squarespace 0 / 8 redacted, Wix refused every request. Every address
//     recovered came from a smaller registrar -- web.com, Network Solutions, Amazon,
//     NameSilo -- that still publishes a registrant.
//   * The registrars, not the registries, are the rate limit. GoDaddy answers 429 with
//     a ~15s sliding window under any parallel load.
//
// Those three facts together are why this pass hops to the registrar, paces per
// registrar host rather than globally, and skips the no-yield registrars outright.
// Expect on the order of 40 addresses across the ~1,100 businesses that still lack
// one. Small, but free, non-recurring, and it reaches businesses nothing else does.
//
// Addresses land in leads.business_email with source='rdap', NOT 'crawl'. The rule in
// 005_business_email.sql is explicit that an address obtained by a different method
// must stay distinguishable: an RDAP registrant address is weaker evidence than one
// the business printed on its own contact page. It may be the web developer, or an
// address that has not been read since the domain was bought. Downstream sends can
// filter on source; they cannot un-blend the two after the fact.
//
// Every outcome -- including the misses -- is recorded in leads.domain_rdap, so a
// re-run probes only what has never been answered. Transport failures and 429s are
// deliberately not recorded, since a throttled request asked the registry nothing.
//
// Usage:
//   node scripts/enrich-rdap.mjs --limit 100 --dry-run   # measure, write nothing
//   node scripts/enrich-rdap.mjs --limit 100             # small live pass
//   node scripts/enrich-rdap.mjs                         # everything outstanding

import { writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { classify, rejectReason } from "./lib/emails.mjs";
import {
  loadBootstrap, toDomain, contactFrom, registrarLink, PLATFORM_HOST, NO_YIELD_REGISTRAR,
  VENDOR_DOMAIN, LEGACY_ISP_DOMAIN, localMatchesRegistrant,
} from "./lib/rdap.mjs";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SCRATCH = process.env.CLAUDE_SCRATCH || "/tmp";

// Concurrency here is across *registrar hosts*, not domains: see HostGate. The
// registries tolerate parallel load fine, but a single registrar will not, and the
// corpus is concentrated -- GoDaddy alone holds about 40% of it. Twelve workers keeps
// the long tail of small registrars busy while HostGate paces each host individually.
const CONCURRENCY = 12;
// Minimum gap between two requests to the same registrar. GoDaddy's retry-after is 33s
// once tripped, so staying under the limit is far cheaper than recovering from it.
const HOST_GAP_MS = 2500;
const TIMEOUT_MS = 20000;

// RDAP returns a contact address per domain, not per page, so there is no page to
// credit it to and no contact-page bonus to earn. This is the ceiling for a
// non-observed address: below any crawl-sourced address, above nothing.
const RDAP_CONFIDENCE_BASE = 35;

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --ask-all-registrars overrides the measured no-yield skip list. Here so the
// measurement can be repeated later without editing code: registrar privacy policy is
// not a constant, and the day one of them starts publishing registrants again, this
// pass should be able to find out cheaply.
const ASK_ALL_REGISTRARS = process.argv.includes("--ask-all-registrars");

async function rdapFetch(url) {
  const res = await fetch(url, {
    headers: { accept: "application/rdap+json", "user-agent": "leads-rdap/1.0" },
    signal: AbortSignal.timeout(TIMEOUT_MS),
    redirect: "follow",
  });
  const status = res.status;
  const retryAfter = res.headers.get("retry-after");
  if (status !== 200) return { status, doc: null, retryAfter };
  try {
    return { status, doc: await res.json(), retryAfter };
  } catch {
    return { status, doc: null, retryAfter };
  }
}

/**
 * Per-registrar-host pacing.
 *
 * The registries are not the constraint: Verisign answered 12 consecutive requests
 * without complaint. The registrars are. GoDaddy -- which holds ~40% of this corpus --
 * answers 429 with `retry-after: 33` almost immediately under any parallel load, and
 * every other registrar has its own limit.
 *
 * So each registrar host gets its own serialized queue with a minimum gap, and a 429
 * pushes that one host's gate out by its retry-after without stalling the others. This
 * is why the pass fans out across registrars rather than across domains.
 */
class HostGate {
  constructor(minGapMs) {
    this.minGapMs = minGapMs;
    this.nextFreeAt = new Map();
  }

  async acquire(host) {
    const now = Date.now();
    const free = this.nextFreeAt.get(host) ?? 0;
    const wait = Math.max(0, free - now);
    this.nextFreeAt.set(host, Math.max(now, free) + this.minGapMs);
    if (wait > 0) await sleep(wait);
  }

  /** A 429 from this host: hold everything queued behind it for retry-after. */
  penalize(host, retryAfterSeconds) {
    const secs = Number(retryAfterSeconds) || 35;
    this.nextFreeAt.set(host, Date.now() + secs * 1000 + 500);
  }
}

/**
 * Probes one domain. Returns a result row, or null when nothing was learned.
 *
 * Null means "ask again next run": a timeout, a 429, or a transport error. Recording
 * those as a miss would permanently poison the domain, since the pass skips anything
 * already in leads.domain_rdap.
 *
 * The registrar hop is not an optional enrichment -- it is the entire source of yield.
 * Measured over 142 domains, the registry's own answer carried a contact entity
 * exactly zero times: .com and .net are thin registries by design, and .org redacts.
 * So a failed registrar hop means this domain is simply unanswered, and returning the
 * registry's contact-free document would record a fabricated `redacted` that the
 * resume logic would then never re-ask.
 */
async function probe(domain, bootstrap, gate, stats) {
  const tld = domain.split(".").pop();
  const base = bootstrap.get(tld);
  if (!base) { stats.no_endpoint++; return null; }

  const registryUrl = `${base}domain/${domain}`;
  let res;
  try {
    res = await rdapFetch(registryUrl);
  } catch {
    stats.transport_error++;
    return null;
  }

  if (res.status === 404) {
    stats.notfound++;
    return { domain, outcome: "notfound", email: null, registrant: null, rdap_url: registryUrl, http_status: 404 };
  }
  if (res.status === 429) { stats.throttled++; return null; }
  if (res.status !== 200 || !res.doc) { stats.bad_status++; return null; }

  // If the registry answered with contacts itself, take it -- a few ccTLDs and
  // thick registries do. Otherwise the registrar hop is mandatory.
  const direct = contactFrom(res.doc, domain);
  if (direct.outcome !== "redacted") {
    stats[direct.outcome]++;
    return { domain, ...direct, rdap_url: registryUrl, http_status: 200 };
  }

  const link = registrarLink(res.doc, base);
  if (!link) {
    // Genuinely nothing further to ask: the registry redacted and named no registrar
    // server. That is a real, storable answer.
    stats.redacted++;
    return { domain, outcome: "redacted", email: null, registrant: null, rdap_url: registryUrl, http_status: 200 };
  }

  const host = new URL(link).hostname;
  // Registrars that bundle privacy registration answer 100% proxy or redacted, and are
  // also the ones that rate-limit hardest. Asking them is pure cost. Not recorded as a
  // miss -- if the policy ever changes, these domains must still be askable.
  if (NO_YIELD_REGISTRAR.test(host) && !ASK_ALL_REGISTRARS) {
    stats.skipped_registrar++;
    return null;
  }
  await gate.acquire(host);
  let second;
  try {
    second = await rdapFetch(link);
  } catch {
    stats.transport_error++;
    return null;
  }
  if (second.status === 429) {
    gate.penalize(host, second.retryAfter);
    stats.throttled++;
    return null;
  }
  if (second.status === 404) {
    stats.notfound++;
    return { domain, outcome: "notfound", email: null, registrant: null, rdap_url: link, http_status: 404 };
  }
  if (second.status !== 200 || !second.doc) { stats.bad_status++; return null; }

  const { outcome, email, registrant } = contactFrom(second.doc, domain);
  stats[outcome]++;
  return { domain, outcome, email, registrant, rdap_url: link, http_status: 200 };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);
  const limit = args.limit ? Number(args.limit) : null;
  const env = loadEnv(ROOT);

  // One business per domain -- several businesses occasionally share one site, and the
  // registry answer is identical for all of them, so probing once and fanning the
  // result out afterwards is both faster and politer.
  //
  // NOT LEFT JOIN-ing business_email here: the gate is businesses.contact_email being
  // null, which is the rolled-up "we have no way to reach this prospect" state that
  // this pass exists to fix.
  const targets = JSON.parse(q(`
    SELECT COALESCE(jsonb_agg(jsonb_build_object('website', website, 'ids', ids)), '[]'::jsonb)
    FROM (
      SELECT min(website) AS website, jsonb_agg(id) AS ids
      FROM leads.businesses
      WHERE qc_status <> 'REJECTED'
        AND contact_email IS NULL
        AND website IS NOT NULL
      GROUP BY lower(regexp_replace(regexp_replace(regexp_replace(
        website, '^https?://', ''), '/.*$', ''), '^www\\.', ''))
    ) g
  `, { args: ["-tA"], env }));

  // Map domain -> business ids, dropping what RDAP cannot or should not answer for.
  const byDomain = new Map();
  let skippedPlatform = 0, skippedUnparseable = 0;
  for (const t of targets) {
    const domain = toDomain(t.website);
    if (!domain) { skippedUnparseable++; continue; }
    // A listing whose "website" is an Instagram profile would return Meta's domain
    // desk, which is not this prospect's address by any reading.
    if (PLATFORM_HOST.test(domain)) { skippedPlatform++; continue; }
    if (!byDomain.has(domain)) byDomain.set(domain, { domain, website: t.website, ids: [] });
    byDomain.get(domain).ids.push(...t.ids);
  }

  // Skip anything already answered. Misses count as answered -- that is the whole
  // point of recording them.
  const done = new Set(
    q(`SELECT domain FROM leads.domain_rdap`, { args: ["-tA"], env })
      .split("\n").map((s) => s.trim()).filter(Boolean)
  );
  let queue = [...byDomain.values()].filter((d) => !done.has(d.domain));
  const outstanding = queue.length;
  if (limit) queue = queue.slice(0, limit);

  console.error(
    `[rdap] ${byDomain.size} distinct domains lack a contact email  ` +
    `(${done.size} already probed, ${outstanding} outstanding)`
  );
  if (skippedPlatform || skippedUnparseable) {
    console.error(`[rdap] skipped ${skippedPlatform} social/platform URLs, ${skippedUnparseable} unparseable`);
  }
  if (!queue.length) { console.error("[rdap] nothing to do"); return; }
  console.error(`[rdap] probing ${queue.length} domains at concurrency ${CONCURRENCY}`);

  const bootstrap = await loadBootstrap();
  console.error(`[rdap] IANA bootstrap: ${bootstrap.size} TLDs`);

  const stats = {
    found: 0, privacy: 0, redacted: 0, junk: 0, notfound: 0,
    no_endpoint: 0, transport_error: 0, throttled: 0, bad_status: 0, skipped_registrar: 0,
  };
  const results = [];
  const started = Date.now();
  const gate = new HostGate(HOST_GAP_MS);
  let index = 0;

  await Promise.all(Array.from({ length: CONCURRENCY }, async () => {
    while (index < queue.length) {
      const item = queue[index++];
      const row = await probe(item.domain, bootstrap, gate, stats);
      if (row) results.push({ ...row, website: item.website, ids: item.ids });
      if ((index % 100) === 0) {
        const rate = (index / ((Date.now() - started) / 1000)).toFixed(1);
        console.error(
          `[rdap] ${index}/${queue.length}  found=${stats.found} privacy=${stats.privacy} ` +
          `redacted=${stats.redacted} throttled=${stats.throttled}  ${rate}/s`
        );
      }
    }
  }));

  // Run every recovered address through the same filter the crawl channel uses. A
  // different channel is not a lower standard of evidence: a placeholder is worthless
  // whether it came off a contact page or out of a registry.
  const emails = [];
  const rollups = [];
  const rejected = new Map();
  for (const r of results) {
    if (r.outcome !== "found" || !r.email) continue;
    const reason = rejectReason(r.email);
    if (reason) {
      rejected.set(reason, (rejected.get(reason) || 0) + 1);
      r.outcome = "junk";
      r.email = null;
      stats.found--;
      stats.junk++;
      continue;
    }
    const rec = classify(r.email, r.website);

    // A third-party domain is the RDAP-specific hazard: web developers and IT shops
    // register domains for clients and leave their own address in the registrant
    // field. The crawl channel never produces these, so this check has no equivalent
    // there. Rejected when the address is on neither the business's own domain nor a
    // consumer mailbox provider -- `brian@fitdog.com` and `sarah@gmail.com` are both
    // plausibly the owner, `purchasing@milesit.com` is an IT vendor.
    const onOwnDomain = rec.matches_site_domain;
    const consumerMailbox = rec.is_free_mail || LEGACY_ISP_DOMAIN.test(rec.domain);
    // An address naming the registrant is theirs wherever it is hosted -- an owner who
    // runs two businesses registers one under the other's domain. A known vendor domain
    // is still rejected: the agency's staff name proves nothing about the prospect.
    const isRegistrantsOwn = localMatchesRegistrant(rec.email, r.registrant)
      && !VENDOR_DOMAIN.test(rec.domain);
    if (!onOwnDomain && !isRegistrantsOwn
        && (VENDOR_DOMAIN.test(rec.domain) || !consumerMailbox)) {
      rejected.set("third-party vendor domain", (rejected.get("third-party vendor domain") || 0) + 1);
      r.outcome = "junk";
      r.email = null;
      stats.found--;
      stats.junk++;
      continue;
    }

    // Matching the site's own domain is nearly automatic here -- the address came from
    // that domain's registration -- so it earns far less than it does from a crawl.
    const confidence = Math.min(
      60,
      RDAP_CONFIDENCE_BASE + (rec.is_role ? 0 : 10) + (rec.is_free_mail ? -5 : 5)
    );
    for (const id of r.ids) {
      emails.push({ business_id: id, ...rec, confidence, source_url: r.rdap_url });
      rollups.push({
        business_id: id,
        contact_email: rec.email,
        contact_email_count: 1,
        contact_email_is_role: rec.is_role,
      });
    }
  }

  const elapsed = Math.round((Date.now() - started) / 1000);
  console.error(`\n[rdap] probed ${queue.length} domains in ${elapsed}s`);
  console.error(
    `[rdap] found=${stats.found}  privacy-proxied=${stats.privacy}  redacted=${stats.redacted}  ` +
    `registrar-only=${stats.junk}  does-not-exist=${stats.notfound}`
  );
  console.error(
    `[rdap] skipped ${stats.skipped_registrar} domains at registrars that never publish a ` +
    `registrant (GoDaddy, Namecheap, Squarespace, Wix); --ask-all-registrars overrides`
  );
  console.error(
    `[rdap] not recorded (will retry): transport=${stats.transport_error} ` +
    `throttled=${stats.throttled} bad-status=${stats.bad_status} no-endpoint=${stats.no_endpoint}`
  );
  if (rejected.size) {
    console.error("[rdap] addresses rejected by the shared filter:");
    for (const [reason, n] of [...rejected].sort((a, b) => b[1] - a[1])) {
      console.error(`         ${String(n).padStart(4)}  ${reason}`);
    }
  }
  console.error(`[rdap] ${emails.length} addresses for ${new Set(emails.map((e) => e.business_id)).size} businesses`);

  if (dryRun) {
    console.error("\n[rdap] --dry-run: nothing written");
    for (const e of emails.slice(0, 15)) {
      console.error(`   ${String(e.confidence).padStart(3)}  ${e.email}`);
    }
    return;
  }
  if (!results.length) return;

  const payloadPath = path.join(SCRATCH, `rdap-${process.pid}.json`);
  writeFileSync(payloadPath, JSON.stringify({
    probes: results.map((r) => ({
      domain: r.domain, outcome: r.outcome, email: r.email,
      registrant: r.registrant, rdap_url: r.rdap_url, http_status: r.http_status,
    })),
    emails,
    rollups,
  }));

  // The rollup UPDATE is guarded on contact_email IS NULL. Between the SELECT at the
  // top of this run and this write, the crawl pass may have found a real address for
  // the same business; an observed address always outranks a registry one, and this
  // pass must never overwrite it.
  const sql = `
    BEGIN;
    CREATE TEMP TABLE rdap_payload (data jsonb);
    \\copy rdap_payload (data) FROM '${payloadPath}'

    INSERT INTO leads.domain_rdap (domain, outcome, email, registrant, rdap_url, http_status, probed_at)
    SELECT p->>'domain', p->>'outcome', p->>'email', p->>'registrant',
           p->>'rdap_url', (p->>'http_status')::int, now()
    FROM rdap_payload t, LATERAL jsonb_array_elements(t.data->'probes') p
    ON CONFLICT (domain) DO UPDATE SET
      outcome = EXCLUDED.outcome, email = EXCLUDED.email, registrant = EXCLUDED.registrant,
      rdap_url = EXCLUDED.rdap_url, http_status = EXCLUDED.http_status, probed_at = now();

    INSERT INTO leads.business_email
      (business_id, email, local_part, domain, source, source_url, page_kind,
       is_role, is_free_mail, matches_site_domain, confidence, extracted_at)
    SELECT (e->>'business_id')::uuid, e->>'email', e->>'local_part', e->>'domain',
           'rdap', e->>'source_url', NULL,
           (e->>'is_role')::boolean, (e->>'is_free_mail')::boolean,
           (e->>'matches_site_domain')::boolean, (e->>'confidence')::int, now()
    FROM rdap_payload t, LATERAL jsonb_array_elements(t.data->'emails') e
    ON CONFLICT (business_id, email) DO NOTHING;

    UPDATE leads.businesses b
    SET contact_email = r->>'contact_email',
        contact_email_count = (r->>'contact_email_count')::int,
        contact_email_is_role = (r->>'contact_email_is_role')::boolean,
        date_updated = now()
    FROM rdap_payload t, LATERAL jsonb_array_elements(t.data->'rollups') r
    WHERE b.id = (r->>'business_id')::uuid
      AND b.contact_email IS NULL;
    COMMIT;
  `;
  execFileSync("/usr/bin/psql", ["-X", "-v", "ON_ERROR_STOP=1", env.LEADS_DB_URL],
    { input: sql, encoding: "utf8", stdio: ["pipe", "inherit", "inherit"] });

  console.error(`[rdap] stored ${results.length} probe outcomes, ${emails.length} addresses`);
}

main().catch((err) => {
  console.error(`[rdap] ${err.message}`);
  process.exit(1);
});
