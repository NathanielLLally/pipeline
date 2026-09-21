// Google Ads Transparency Center: request construction and response decoding.
//
// The endpoint is an internal protobuf-JSON RPC, so every field below is a NUMERIC
// STRING KEY whose meaning was read off a real browser request captured with Playwright
// (see docs/ARCHITECTURE.md, "Google Ads Transparency: SOLVED"). There is no published
// schema. An earlier session lost hours probing positional-array payloads because a
// JSON array is also structurally valid protobuf-JSON: the server accepted every
// malformed probe and answered `{}` instead of erroring.
//
// THE DIAGNOSTIC TRAP, repeated here because it is cheap to fall into again: `{}` means
// "this advertiser runs no ads", NOT "your request was wrong". The two are
// indistinguishable from a single call. Any change to REQ_SHAPE below must therefore be
// checked against a domain KNOWN to advertise -- nike.com returns ~15KB, booking.com
// ~22KB -- before concluding anything from an empty response.

/** Field numbers in the response, named so the extractor below reads as prose. */
const F = {
  ADS: "1",             // top-level: array of creatives, page-size many
  CURSOR: "2",          // top-level: pagination token; absent on the last page
  ADVERTISER_ID: "1",   // per-ad: "AR..." stable across an advertiser's creatives
  CREATIVE_ID: "2",     // per-ad: "CR..."
  CONTENT: "3",         // per-ad: the creative itself; shape varies by format
  FORMAT: "4",          // per-ad: numeric format code, meaning undocumented
  FIRST_SHOWN: "6",     // per-ad: { "1": epoch_seconds_as_string, "2": nanos }
  LAST_SHOWN: "7",      // per-ad: same shape -- THE recency signal
  ADVERTISER_NAME: "12",
  REGION_COUNT: "13",   // per-ad: a count, believed to be regions; not relied on
  DOMAIN: "14",
};

// 2840 is the region code the live page sends for a US "all regions" search. It appears
// three times in the captured request and was not varied, so it is treated as opaque.
const REGION = 2840;
const PAGE_SIZE = 40;

export const ENDPOINT =
  "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser=";

export const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36";

/**
 * The form body for one domain.
 *
 * `3.12.2: true` is what scopes the search to a domain rather than an advertiser name;
 * without it the query is interpreted as free text and matches nothing useful.
 */
export function requestBody(domain) {
  const req = {
    "2": PAGE_SIZE,
    "3": { "8": [REGION], "12": { "1": domain, "2": true } },
    "7": { "1": 1, "2": 0, "3": REGION },
  };
  return `f.req=${encodeURIComponent(JSON.stringify(req))}`;
}

/**
 * Normalizes a stored `website` value to the bare host the RPC expects.
 *
 * The RPC matches on the registrable host with no scheme, no `www.`, no path and no
 * port. Returns null when the stored value is not a URL at all -- a handful of rows
 * hold phone numbers and free text.
 */
export function toDomain(website) {
  if (!website) return null;
  const raw = String(website).trim();
  if (!raw) return null;
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  let host;
  try { host = new URL(withScheme).hostname; } catch { return null; }
  host = host.toLowerCase().replace(/^www\./, "");
  // A bare label with no dot is not a domain; neither is a raw IP.
  if (!host.includes(".") || /^[\d.]+$/.test(host)) return null;
  return host;
}

/**
 * Extracts the ad's actual content from the per-ad `3` node.
 *
 * Three shapes appear in real responses, and the node is a union rather than a record
 * with optional fields, so each has to be probed for rather than read positionally:
 *
 *   {"3":{"2":"<img src=...>"},"5":true}   an image ad; the markup is one <img> tag
 *   {"1":{"4":"https://displayads-formats.googleusercontent.com/ads/preview/..."}}
 *                                          a rendered preview URL (text and rich ads)
 *
 * Both are kept: `image_url` is directly viewable, and `preview_url` renders the ad as
 * Google shows it in the Transparency Center. The raw markup is kept too, since it is
 * small (~160 bytes) and is the only fully faithful record -- a preview URL is signed
 * and will eventually stop resolving, so evidence that outlives the link is worth the
 * bytes. Across a 40-creative response the whole content set is ~6.5KB.
 */
function creativeContent(node) {
  if (!node || typeof node !== "object") return {};
  const out = {};

  // Image ad: the markup sits at 3.2 and is a single <img> tag.
  const markup = node["3"] && node["3"]["2"];
  if (typeof markup === "string" && markup) {
    out.markup = markup;
    const m = markup.match(/src\s*=\s*"([^"]+)"/i);
    if (m) out.image_url = m[1];
    const w = markup.match(/width\s*=\s*"(\d+)"/i);
    const h = markup.match(/height\s*=\s*"(\d+)"/i);
    if (w && h) out.dimensions = `${w[1]}x${h[1]}`;
  }

  // Text/rich ad: a signed preview URL at 1.4 that renders the creative.
  const preview = node["1"] && node["1"]["4"];
  if (typeof preview === "string" && preview) out.preview_url = preview;

  return out;
}

/** Epoch-seconds string -> ISO timestamp, or null when the field is missing. */
function epochAt(node) {
  const secs = node && node["1"];
  if (secs === undefined || secs === null) return null;
  const n = Number(secs);
  // Guard against a field that is present but not a plausible epoch: the column is
  // timestamptz and a garbage value would be stored as a real date nobody questions.
  if (!Number.isFinite(n) || n < 946684800 || n > 4102444800) return null;
  return new Date(n * 1000).toISOString();
}

/**
 * Decodes a SearchCreatives response into the fields the database stores.
 *
 * Returns `{ found: false }` for `{}` -- the advertiser runs no ads -- which is a real,
 * useful answer rather than a failure, and is stored as such so a re-run does not
 * re-query it.
 */
export function parseCreatives(json) {
  const ads = json && json[F.ADS];
  if (!Array.isArray(ads) || ads.length === 0) return { found: false, creatives: [] };

  const creatives = [];
  const formats = new Set();
  let advertiserId = null;
  let advertiserName = null;
  let domain = null;
  let first = null;
  let last = null;

  for (const ad of ads) {
    if (!ad || typeof ad !== "object") continue;
    const firstShown = epochAt(ad[F.FIRST_SHOWN]);
    const lastShown = epochAt(ad[F.LAST_SHOWN]);

    advertiserId ??= ad[F.ADVERTISER_ID] ?? null;
    advertiserName ??= ad[F.ADVERTISER_NAME] ?? null;
    domain ??= ad[F.DOMAIN] ?? null;
    if (ad[F.FORMAT] !== undefined && ad[F.FORMAT] !== null) formats.add(String(ad[F.FORMAT]));

    if (firstShown && (!first || firstShown < first)) first = firstShown;
    if (lastShown && (!last || lastShown > last)) last = lastShown;

    creatives.push({
      creative_id: ad[F.CREATIVE_ID] ?? null,
      format: ad[F.FORMAT] ?? null,
      first_shown: firstShown,
      last_shown: lastShown,
      ...creativeContent(ad[F.CONTENT]),
    });
  }

  return {
    found: creatives.length > 0,
    advertiser_id: advertiserId,
    advertiser_name: advertiserName,
    domain,
    creative_count: creatives.length,
    first_shown: first,
    last_shown: last,
    formats: [...formats].sort(),
    creatives,
    // The response is capped at PAGE_SIZE; a cursor means the advertiser has more than
    // one page. Recorded so `creative_count` is never mistaken for a total.
    truncated: creatives.length >= PAGE_SIZE && Boolean(json[F.CURSOR]),
  };
}

/**
 * Whether a last-shown date means the campaign is live.
 *
 * 30 days is chosen from the shape of the measured sample rather than arbitrarily: of
 * 19 confirmed advertisers among 25 top-tier prospects, the great majority were last
 * shown 0-1 days ago, and the nearest thing to a boundary case was 128 days -- a
 * business whose site still carries a pixel. There is a wide empty gap between "serving
 * today" and "served months ago", and 30 days sits in it.
 */
export const RECENT_DAYS = 30;

export function isRecent(lastShownIso, now = Date.now()) {
  if (!lastShownIso) return false;
  const t = Date.parse(lastShownIso);
  if (!Number.isFinite(t)) return false;
  return now - t <= RECENT_DAYS * 86400 * 1000;
}
