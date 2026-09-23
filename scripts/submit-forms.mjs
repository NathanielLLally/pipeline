#!/usr/bin/env node
// The contact-form channel: reaches the businesses that publish a form instead of an
// address.
//
// 242 businesses in the corpus have a working website, a contact form on it, and no
// email address anywhere -- 64 of them Tier 1. That is not an oversight on their part.
// They route inquiries through the form deliberately, which means the form IS their
// address and a submission is the only way to reach them short of the phone.
//
// HOW THIS DIFFERS FROM EVERY OTHER PASS IN THIS REPO. The crawl, the RDAP probe and
// the ads check all *ask questions*. This one *sends messages* to real businesses under
// our own name. Three consequences shape the design:
//
//   1. It is not idempotent and cannot be made so. A retried POST may be a second copy
//      of the same inquiry landing in a stranger's inbox. So the attempt is recorded
//      BEFORE the request goes out, and `unknown` -- sent, no confirmation seen -- is
//      terminal unless a human passes --retry-unknown.
//   2. It fails closed everywhere. A required field whose meaning is not understood
//      aborts that submission; a captcha, a file upload or a newsletter box is skipped.
//      Half an inquiry with our name on it is worse than no inquiry.
//   3. It will not run live without --send AND a sender identity in .env. --dry-run is
//      the default, and it parses, fills and reports without opening a connection.
//
// The message is a genuine inquiry from a real, monitored mailbox. Anything else is
// both useless -- a reply we never read reaches nobody -- and a straightforward abuse
// of these businesses' time.
//
// Usage:
//   node scripts/submit-forms.mjs --dry-run --limit 20        # default; no network
//   node scripts/submit-forms.mjs --dry-run --limit 20 --fetch  # refetch pages, no POST
//   node scripts/submit-forms.mjs --send --limit 10 --tier "Tier 1"
//   node scripts/submit-forms.mjs --send --retry-unknown       # deliberate duplicates
//
// Required in .env before --send will do anything:
//   FORM_SENDER_NAME, FORM_SENDER_EMAIL, FORM_SENDER_PHONE, FORM_SENDER_COMPANY
//   FORM_SENDER_ZIP, FORM_SENDER_CITY, FORM_SENDER_STATE, FORM_MESSAGE

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { q } from "./lib/q.mjs";
import { loadEnv } from "./lib/env.mjs";
import { loadProxies, rotator } from "./lib/proxies.mjs";
import { extractForms, parseForm, fillForm, readResponse } from "./lib/forms.mjs";
import { toText } from "./enrich-websites.mjs";

const execFileAsync = promisify(execFile);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const TIMEOUT_SEC = 30;
const MAX_BYTES = 4 << 20;

// Pages tried per business before giving up. The crawler stored up to four (homepage
// plus three sub-pages) and the contact page is tried first, so this is a ceiling on
// wasted fetches rather than a real limit.
const MAX_PAGES = 4;

// One submission at a time, with a real pause between them. Everywhere else in this
// repo concurrency is a throughput question; here it is a politeness question. A dozen
// parallel POSTs from one IP range into a dozen small businesses' inboxes is what an
// attack looks like from their side, and there is no hurry: 242 sends at one every few
// seconds is a twenty-minute job.
const GAP_MS = 4000;

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

const args = parseArgs(process.argv.slice(2));
const SEND = args.send === true;
const FETCH = args.fetch === true || SEND;
const RETRY_UNKNOWN = args["retry-unknown"] === true;
const LIMIT = args.limit ? Number(args.limit) : 25;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Same jsonb-literal hazards as the crawler: NUL aborts the transaction. */
function jsonLiteral(value) {
  return JSON.stringify(value)
    .replace(/\\u000[0-8bcef]|\\u001[0-9a-f]/gi, " ")
    .replace(/\$e\$/g, "");
}

/**
 * The sender identity, read from .env and validated hard.
 *
 * Every field is required and none has a default, deliberately. A placeholder identity
 * posted to 242 businesses is a mess that cannot be cleaned up, so the pass refuses to
 * send rather than inventing a plausible-looking name. The address must be a real
 * mailbox someone reads: these businesses will reply to it.
 */
function identityFrom(env) {
  const missing = [];
  const need = (k) => {
    const v = (env[k] || "").trim();
    if (!v) missing.push(k);
    return v;
  };
  const name = need("FORM_SENDER_NAME");
  const identity = {
    firstName: name.split(/\s+/)[0] || "",
    lastName: name.split(/\s+/).slice(1).join(" ") || "",
    email: need("FORM_SENDER_EMAIL"),
    phone: need("FORM_SENDER_PHONE"),
    company: need("FORM_SENDER_COMPANY"),
    zip: need("FORM_SENDER_ZIP"),
    city: need("FORM_SENDER_CITY"),
    state: need("FORM_SENDER_STATE"),
    subject: env.FORM_SUBJECT || "Question about your services",
  };
  const message = need("FORM_MESSAGE");
  if (name && !identity.lastName) missing.push("FORM_SENDER_NAME (needs a surname too)");
  return { identity, message, missing };
}

/** The businesses this pass is for: a form, no address, not rejected. */
function selectTargets(env) {
  const tierFilter = args.tier
    ? `AND b.icp_tier = ANY(ARRAY[${String(args.tier).split(",").map((t) => `'${t.trim().replace(/'/g, "")}'`).join(",")}])`
    : "";
  // Only pages whose form signal was actually detected, newest crawl first, and only
  // businesses with no attempt already on record. LEFT JOIN ... IS NULL rather than
  // NOT EXISTS so --retry-unknown can relax exactly one outcome.
  const retry = RETRY_UNKNOWN ? `AND s.outcome NOT IN ('sent','rejected','skipped')` : "";
  // All of a business's crawled pages, not just one.
  //
  // Picking a single page per business lost real forms: a homepage very often carries
  // only the footer newsletter box while the inquiry form sits on /contact/, and the
  // first dry run reported "no message field" for a business whose contact page has a
  // complete Gravity form on it. The pass now walks the pages in order and stops at
  // the first one that yields a submittable form.
  //
  // Contact pages first for that reason, then about/pricing, then the homepage last.
  return JSON.parse(q(`
    SELECT coalesce(jsonb_agg(t ORDER BY t.icp_score DESC), '[]'::jsonb) FROM (
      SELECT b.id AS business_id, b.name, b.icp_tier, b.icp_score, b.city, b.state,
             array_agg(coalesce(w.final_url, w.url)
                       ORDER BY (w.page_kind = 'contact') DESC,
                                (w.page_kind IS NOT NULL) DESC,
                                w.fetched_at DESC) AS page_urls
      FROM leads.businesses b
      JOIN leads.website_crawl w ON w.business_id = b.id
      LEFT JOIN leads.form_submission s ON s.business_id = b.id ${retry}
      WHERE b.qc_status <> 'REJECTED'
        AND b.contact_email IS NULL
        AND w.signals ? 'lead_form'
        AND s.id IS NULL
        ${tierFilter}
      GROUP BY b.id
      ORDER BY b.icp_score DESC
      LIMIT ${Number(LIMIT) || 25}
    ) t;
  `, { env, args: ["-t", "-A"] }));
}

/**
 * Hosts that have already received a submission on some previous run.
 *
 * Only `sent`, `rejected` and `unknown` count: those all put bytes on the wire. A
 * `blocked` or `skipped` row means nothing was delivered, so that host is still free.
 */
function alreadyContactedHosts(env) {
  return q(`
    SELECT DISTINCT regexp_replace(lower(action_url), '^https?://(www\\.)?([^/:]+).*$', '\\2')
    FROM leads.form_submission
    WHERE outcome IN ('sent', 'rejected', 'unknown');
  `, { env, args: ["-t", "-A"] }).split("\n").map((s) => s.trim()).filter(Boolean);
}

/** GET a page through the proxy pool. */
async function fetchPage(url, nextProxy) {
  const p = nextProxy();
  try {
    const { stdout } = await execFileAsync("/usr/bin/curl", [
      "-sS", "--compressed", "-L", "--max-redirs", "5",
      "--max-time", String(TIMEOUT_SEC), "--max-filesize", String(MAX_BYTES),
      "--socks5-hostname", `${p.host}:${p.port}`, "--proxy-user", `${p.user}:${p.pass}`,
      "-A", UA, "-w", "\n@@%{http_code}\t%{url_effective}", url,
    ], { encoding: "utf8", maxBuffer: MAX_BYTES + (1 << 20) });
    const i = stdout.lastIndexOf("\n@@");
    if (i < 0) return { status: 0, body: "", finalUrl: url };
    const [code, effective] = stdout.slice(i + 3).split("\t");
    return { status: Number(code) || 0, body: stdout.slice(0, i), finalUrl: (effective || url).trim() };
  } catch {
    return { status: 0, body: "", finalUrl: url };
  }
}

/**
 * POST the filled form.
 *
 * Note --max-time is generous and there is no retry: a timed-out POST may have been
 * delivered, so retrying it risks a duplicate. It is recorded as `blocked` only when
 * curl never got a connection at all, and `unknown` otherwise.
 *
 * The Referer is the page the form was on, because a good many WordPress security
 * plugins reject a POST without one -- and it is also simply true.
 */
async function postForm(target, fields, enctype, pageUrl, nextProxy) {
  const p = nextProxy();
  const argv = [
    "-sS", "-L", "--max-redirs", "5", "--compressed",
    "--max-time", String(TIMEOUT_SEC), "--max-filesize", String(MAX_BYTES),
    "--socks5-hostname", `${p.host}:${p.port}`, "--proxy-user", `${p.user}:${p.pass}`,
    "-A", UA, "-e", pageUrl,
    "-w", "\n@@%{http_code}\t%{url_effective}",
  ];
  // multipart when the form declared it -- Gravity Forms sets enctype and some
  // installs reject urlencoded outright. curl's -F is multipart, --data-urlencode
  // is not, and the two cannot be mixed in one request.
  for (const [name, value] of fields) {
    if (enctype.includes("multipart")) argv.push("-F", `${name}=${value}`);
    else argv.push("--data-urlencode", `${name}=${value}`);
  }
  argv.push(target);

  try {
    const { stdout } = await execFileAsync("/usr/bin/curl", argv, {
      encoding: "utf8", maxBuffer: MAX_BYTES + (1 << 20),
    });
    const i = stdout.lastIndexOf("\n@@");
    if (i < 0) return { status: 0, body: "" };
    const [code] = stdout.slice(i + 3).split("\t");
    return { status: Number(code) || 0, body: stdout.slice(0, i) };
  } catch (err) {
    // Never echo argv: it carries the proxy credentials and the whole message body.
    const raw = (err.stderr || "").toString();
    const m = raw.match(/curl:\s*\(\d+\)\s*(.{0,90})/);
    return { status: 0, body: "", error: m ? m[1].trim() : "post failed" };
  }
}

/** Writes one attempt. Called before the POST, and again with the outcome after. */
function record(row, env) {
  q(`
    CREATE TEMP TABLE IF NOT EXISTS fs_payload (data jsonb);
    TRUNCATE fs_payload;
    INSERT INTO fs_payload VALUES ($e$${jsonLiteral([row])}$e$::jsonb);
    INSERT INTO leads.form_submission
      (business_id, page_url, action_url, form_key, outcome, http_status, note, fields_sent)
    SELECT (r->>'business_id')::uuid, r->>'page_url', r->>'action_url', r->>'form_key',
           r->>'outcome', (r->>'http_status')::int, r->>'note', r->'fields_sent'
    FROM fs_payload p, LATERAL jsonb_array_elements(p.data) r
    ON CONFLICT (business_id, action_url, form_key) DO UPDATE SET
      outcome = EXCLUDED.outcome, http_status = EXCLUDED.http_status,
      note = EXCLUDED.note, fields_sent = EXCLUDED.fields_sent, submitted_at = now();
  `, { env });
}

async function main() {
  const env = loadEnv(ROOT);
  const { identity, message, missing } = identityFrom(env);

  if (SEND && missing.length) {
    console.error(`Refusing to send: missing in .env -- ${missing.join(", ")}`);
    console.error("These messages go to real businesses under this identity; there is no default.");
    process.exit(1);
  }
  // In dry-run the identity only has to be shaped right, so a placeholder stands in and
  // the parsing can be exercised before the mailbox exists.
  const dryIdentity = missing.length
    ? { firstName: "Firstname", lastName: "Lastname", email: "you@example.com",
        phone: "5555550100", company: "Company", zip: "00000", city: "City",
        state: "ST", subject: "Question about your services" }
    : identity;
  const dryMessage = message || "[FORM_MESSAGE not set -- placeholder for dry-run]";

  const targets = selectTargets(env);
  console.log(`${targets.length} businesses with a contact form and no address`);
  console.log(SEND ? "MODE: LIVE -- forms will be submitted" : "MODE: dry run -- nothing will be sent");
  if (!SEND && missing.length) console.log(`(identity incomplete: ${missing.join(", ")})`);
  console.log();

  const nextProxy = FETCH ? rotator(loadProxies(env)) : null;
  const stats = { sent: 0, rejected: 0, unknown: 0, blocked: 0, skipped: 0, no_form: 0 };
  const skipReasons = new Map();

  // Websites already written to, across every previous run as well as this one. Keyed
  // on host rather than business, because 242 target businesses sit on only 207
  // distinct websites -- multi-location trainers list each suburb separately on Google
  // while sharing one site and one inbox.
  const contacted = new Map(alreadyContactedHosts(env).map((h) => [h, "earlier run"]));

  for (const t of targets) {
    if (!FETCH) {
      // Dry run without --fetch reads the stored excerpt, which is text, not markup --
      // so it cannot parse forms. Report the target and move on.
      console.log(`  [${t.icp_tier}] ${t.name} -- ${t.page_urls[0]} (stored text only; pass --fetch to parse)`);
      continue;
    }

    // Walk the business's pages until one yields a submittable form. Contact pages come
    // first, so the common case costs one fetch.
    let form = null;
    let pageUrl = null;
    let why = null;
    let anyFetched = false;
    for (const url of t.page_urls.slice(0, MAX_PAGES)) {
      const res = await fetchPage(url, nextProxy);
      if (!(res.status >= 200 && res.status < 400 && res.body.length > 500)) continue;
      anyFetched = true;
      // The best form on the page: submittable ones first, then the one with the most
      // fields, which is the inquiry form rather than a stray signup.
      const parsed = extractForms(res.body).map((f) => parseForm(f, res.finalUrl || url));
      const usable = parsed.filter((p) => p.ok && p.action);
      if (usable.length) {
        form = usable.sort((a, b) => b.fields.length - a.fields.length)[0];
        pageUrl = res.finalUrl || url;
        break;
      }
      why = why || parsed.map((p) => p.reason).filter(Boolean)[0] || "no <form> on the page";
    }

    if (!anyFetched) {
      stats.blocked++;
      console.log(`  BLOCKED  ${t.name} -- could not fetch ${t.page_urls[0]}`);
      continue;
    }
    if (!form) {
      stats.no_form++;
      why = why || "no <form> on the page";
      skipReasons.set(why, (skipReasons.get(why) || 0) + 1);
      console.log(`  NO FORM  ${t.name} -- ${why}`);
      continue;
    }

    // One inquiry per website, not per listing. Four Midway Dog Academy rows are four
    // suburbs of one business sharing one site and one inbox; posting the same form
    // four times sends one owner four identical inquiries. The business rows stay
    // distinct -- they are distinct prospects -- but the contact attempt is per site.
    const host = (() => { try { return new URL(form.action).host.replace(/^www\./, ""); } catch { return null; } })();
    if (host && contacted.has(host)) {
      stats.skipped++;
      const why = `same website already contacted (${contacted.get(host)})`;
      skipReasons.set("shared website with another listing", (skipReasons.get("shared website with another listing") || 0) + 1);
      if (SEND) {
        record({ business_id: t.business_id, page_url: pageUrl, action_url: form.action,
                 form_key: form.key, outcome: "skipped", http_status: null,
                 note: why, fields_sent: null }, env);
      }
      console.log(`  SKIP     ${t.name} -- ${why}`);
      continue;
    }

    const filled = fillForm({
      form: form.form, fields: form.fields, labels: form.labels,
      identity: SEND ? identity : dryIdentity,
      message: SEND ? message : dryMessage,
    });
    if (!filled.ok) {
      stats.skipped++;
      skipReasons.set(filled.reason, (skipReasons.get(filled.reason) || 0) + 1);
      if (SEND) {
        record({ business_id: t.business_id, page_url: pageUrl, action_url: form.action,
                 form_key: form.key, outcome: "skipped", http_status: null,
                 note: filled.reason, fields_sent: null }, env);
      }
      console.log(`  SKIP     ${t.name} -- ${filled.reason}`);
      continue;
    }

    // Claimed before the send, and in dry run too, so the dry run's counts match what
    // a live run would actually do.
    if (host) contacted.set(host, t.name);

    if (!SEND) {
      const shown = filled.body.filter(([, v]) => v !== "").slice(0, 8)
        .map(([n, v]) => `${n}=${String(v).slice(0, 28)}`).join("  ");
      console.log(`  WOULD POST ${t.name}\n      -> ${form.action}\n      ${shown}`);
      continue;
    }

    // Written BEFORE the request. If this process dies mid-send, the record that we
    // tried survives, and the business does not get a second copy on the next run.
    record({ business_id: t.business_id, page_url: pageUrl, action_url: form.action,
             form_key: form.key, outcome: "unknown", http_status: null,
             note: "in flight", fields_sent: Object.fromEntries(filled.body) }, env);

    const res = await postForm(form.action, filled.body, form.enctype, pageUrl, nextProxy);
    const verdict = readResponse(res.status, res.body ? toText(res.body) : "");
    stats[verdict.outcome]++;
    record({ business_id: t.business_id, page_url: pageUrl, action_url: form.action,
             form_key: form.key, outcome: verdict.outcome, http_status: res.status,
             note: verdict.note, fields_sent: Object.fromEntries(filled.body) }, env);
    console.log(`  ${verdict.outcome.toUpperCase().padEnd(8)} ${t.name} -- ${verdict.note}`);

    await sleep(GAP_MS);
  }

  console.log("\n" + JSON.stringify(stats));
  if (skipReasons.size) {
    console.log("\nskips by reason:");
    for (const [why, n] of [...skipReasons].sort((a, b) => b[1] - a[1])) {
      console.log(`  ${String(n).padStart(4)}  ${why}`);
    }
  }
}

main().catch((err) => { console.error(err.message); process.exit(1); });
