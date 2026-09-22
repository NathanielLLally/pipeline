// Email extraction and classification, shared by extract-emails.mjs and its tests.
//
// The addresses this finds are the pipeline's actual deliverable, so the bias
// throughout is toward precision over recall: a junk address in a send list costs
// sender reputation, which is expensive and slow to repair, while a missed address
// costs one prospect that the next crawl may pick up anyway.
//
// Everything here operates on text already fetched and stored in
// leads.website_crawl.text_excerpt. No network calls, and no guessing: an address is
// only ever reported when it appears verbatim in a page belonging to that business.

// Deliberately stricter than the usual permissive pattern. The TLD must be alphabetic
// and at least two characters, which rejects the version-number and filename debris
// ("foo@2x.png", "bar@1.5") that a looser pattern pulls out of minified CSS.
const EMAIL_RE = /[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}/g;

// Local parts that reach a mailbox nobody reads, or that belong to infrastructure
// rather than the business. `filler@godaddy.com` alone accounted for 86 of 88 junk
// matches in the first measured pass: it is GoDaddy's placeholder, present on every
// unconfigured parked site.
const JUNK_LOCAL = /^(?:noreply|no-reply|donotreply|do-not-reply|filler|example|test|user|username|email|youremail|name|firstname|lastname|johnsmith|janedoe|abc|xxx|yourname|someone)$/i;

// Domains that are never a business's real contact address: platform infrastructure,
// analytics vendors, and the placeholder domains that ship inside templates.
const JUNK_DOMAIN = /(?:^|\.)(?:sentry\.(?:io|wixpress\.com)|wixpress\.com|wix\.com|squarespace\.com|godaddy\.com|example\.(?:com|org|net)|domain\.com|email\.com|yourdomain\.com|yoursite\.com|sentry-next\.wixpress\.com|schema\.org|w3\.org|googleapis\.com|gstatic\.com|cloudflare\.com|jquery\.com|bootstrapcdn\.com|fontawesome\.com|placeholder\.com|test\.com|website\.com|company\.com|business\.com|mysite\.com)$/i;

// File extensions that mean the "address" is really a filename or asset reference that
// happened to contain an @. Image sprites named "logo@2x.png" are the common case.
const ASSET_EXT = /\.(?:png|jpe?g|gif|webp|svg|ico|css|js|woff2?|ttf|eot|mp4|webm|pdf|zip)$/i;

// Role accounts: they reach the business but not a named decision maker. Kept, not
// discarded -- for a one-person dog training business info@ often IS the owner -- but
// scored below a personal address, and flagged so outreach copy can adapt.
const ROLE_LOCAL = /^(?:info|contact|hello|hi|admin|office|support|help|sales|team|mail|email|enquir(?:y|ies)|inquir(?:y|ies)|booking|bookings|book|schedule|training|service|services|general|reception|frontdesk|front-desk|customerservice|care|woof|bark|dogs|pets)$/i;

// Consumer mailbox providers. Extremely common and entirely legitimate for businesses
// of this size -- 285 of 880 measured addresses -- but the address cannot be
// corroborated against the site's own domain, so it scores slightly lower.
const FREE_MAIL = /^(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|msn|aol|icloud|me|mac|comcast|verizon|att|sbcglobal|bellsouth|cox|charter|earthlink|protonmail|proton|gmx|mail|zoho|yandex)\./i;

/** Strips a host to its registrable-ish form for comparison: no scheme, no www. */
export function siteHost(website) {
  if (!website) return null;
  const raw = String(website).trim();
  if (!raw) return null;
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  let host;
  try { host = new URL(withScheme).hostname; } catch { return null; }
  host = host.toLowerCase().replace(/^www\./, "");
  return host.includes(".") ? host : null;
}

/**
 * True when an address plausibly belongs to the site it was found on.
 *
 * Compares the last two labels so `mail.example.com` matches `example.com`. This is
 * approximate on multi-part TLDs (`co.uk`), which is acceptable: the value is only
 * ever used as a positive signal, so an occasional missed match costs a few points of
 * confidence and never produces a wrong address.
 */
export function domainsMatch(emailDomain, host) {
  if (!emailDomain || !host) return false;
  const tail = (d) => d.toLowerCase().split(".").slice(-2).join(".");
  return tail(emailDomain) === tail(host);
}

/** Classifies one already-normalized address. */
export function classify(email, website) {
  const [localPart, domain] = email.split("@");
  const host = siteHost(website);
  return {
    email,
    local_part: localPart,
    domain,
    is_role: ROLE_LOCAL.test(localPart),
    is_free_mail: FREE_MAIL.test(domain + "."),
    matches_site_domain: domainsMatch(domain, host),
  };
}

/**
 * Rejects addresses that are not real contact points.
 *
 * Returns a reason string when the address should be dropped, or null to keep it.
 * A reason rather than a boolean so a run can report WHY things were discarded --
 * silent filtering is how a bad rule survives unnoticed.
 */
export function rejectReason(email) {
  const at = email.indexOf("@");
  if (at < 1) return "malformed";
  const localPart = email.slice(0, at);
  const domain = email.slice(at + 1);

  if (ASSET_EXT.test(email)) return "asset filename";
  if (JUNK_LOCAL.test(localPart)) return "placeholder local part";
  if (JUNK_DOMAIN.test(domain)) return "infrastructure domain";
  // A 64-character local part is legal but never a real small-business address; in
  // practice a match this long is a concatenated blob from minified source.
  if (localPart.length > 40) return "implausibly long local part";
  if (email.length > 254) return "exceeds maximum email length";
  // Hex-looking local parts are cache-buster and tracking tokens, not people.
  if (/^[0-9a-f]{16,}$/i.test(localPart)) return "hex token";
  if (!/[A-Za-z]/.test(localPart)) return "no letters in local part";
  return null;
}

/**
 * Pulls every plausible address out of one page's text.
 *
 * Returns normalized, deduplicated, classified records. Order is preserved so the
 * first occurrence on a page -- usually the one in the header or contact block --
 * comes first.
 */
export function extractFromText(text, website) {
  if (!text) return [];
  const seen = new Set();
  const out = [];
  for (const raw of text.match(EMAIL_RE) || []) {
    // Trailing punctuation clings to addresses in prose: "email me at bob@x.com."
    const email = raw.toLowerCase().replace(/[.,;:)\]}>'"]+$/, "");
    if (seen.has(email)) continue;
    seen.add(email);
    if (rejectReason(email)) continue;
    out.push(classify(email, website));
  }
  return out;
}

// Where an address was found, as a confidence contribution. A contact page is the
// business stating how to reach it; an address in a pricing page's footer is weaker.
const PAGE_KIND_POINTS = { contact: 20, about: 16, home: 12, pricing: 8 };

/**
 * Scores one address 0-100 for "is this the right address to actually email".
 *
 * The weights encode a specific judgement: an address on the business's own domain is
 * the strongest signal available that it belongs to them, and a named local part is
 * the strongest signal that a human reads it. Role addresses on the correct domain
 * still score respectably, because for a small dog-training business info@ frequently
 * is the owner's inbox.
 */
export function confidenceFor(rec, pageKind) {
  let score = 40;
  if (rec.matches_site_domain) score += 30;
  else if (!rec.is_free_mail) score += 5;   // some other custom domain: weak, not nothing
  if (!rec.is_role) score += 15;
  if (rec.is_free_mail) score -= 10;
  score += PAGE_KIND_POINTS[pageKind] ?? 6;
  return Math.max(0, Math.min(100, score));
}

/**
 * Picks the single best address for a business from its candidates.
 *
 * Highest confidence wins; ties break toward the address seen on the most pages, since
 * a business that lists the same address on three pages means it.
 */
export function bestOf(records) {
  if (!records.length) return null;
  return [...records].sort((a, b) =>
    (b.confidence - a.confidence) || ((b.page_count || 1) - (a.page_count || 1))
  )[0];
}
