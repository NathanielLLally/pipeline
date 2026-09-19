// Detection rules for Phase B (Prompt 7). Pure functions over fetched HTML, kept
// separate from the crawler so they can be tested without network access.
//
// Every detector records the token it matched. A boolean on its own is not auditable
// and Prompt 7 is explicit that paid advertising must not be claimed without evidence,
// so the matched string goes into website_crawl.signals alongside the verdict.

/**
 * Paid-media pixels. These, and only these, can set marketing_active.
 *
 * Note the deliberate asymmetry with analytics below: GA4 and GTM say a site is
 * instrumented, not that anyone is buying traffic. A business with GA4 and no ad
 * pixel is a business measuring its organic traffic.
 */
export const PAID_MEDIA = {
  google_ads: [
    [/gtag\/js\?id=AW-/i, "gtag AW- conversion id"],
    [/googleadservices\.com/i, "googleadservices.com"],
    [/google_conversion_id/i, "google_conversion_id"],
    [/\bAW-\d{9,}/i, "AW- conversion id"],
    [/googleads\.g\.doubleclick\.net/i, "doubleclick ads"],
  ],
  meta_ads: [
    [/connect\.facebook\.net\/[a-z_]+\/fbevents\.js/i, "fbevents.js"],
    [/\bfbq\s*\(\s*['"]init['"]/i, "fbq('init')"],
    [/facebook\.com\/tr\?id=/i, "facebook pixel tr?id"],
  ],
};

/** Instrumentation that indicates marketing effort but not ad spend. */
export const MARTECH = [
  [/gtag\/js\?id=G-|googletagmanager\.com\/gtag\/js\?id=G-/i, "GA4"],
  [/googletagmanager\.com\/gtm\.js|GTM-[A-Z0-9]{4,}/i, "Google Tag Manager"],
  [/static\.hotjar\.com|hj\s*\(/i, "Hotjar"],
  [/klaviyo\.com\/onsite|klaviyo\.js/i, "Klaviyo"],
  [/js\.hs-scripts\.com|hubspot/i, "HubSpot"],
  [/mailchimp|mc\.us\d+\.list-manage\.com/i, "Mailchimp"],
  [/cdn\.callrail\.com/i, "CallRail"],
  [/tidio|intercom|drift\.com|tawk\.to/i, "live chat"],
  [/snap\.licdn\.com/i, "LinkedIn insight tag"],
  [/analytics\.tiktok\.com/i, "TikTok pixel"],
];

/** Booking vendors. Mirrors the list in enrich-from-raw.mjs, which reads the same
 *  vendors out of raw_scrape.order_online -- the two phases see the same booking
 *  stack from different angles, so keeping the names identical lets them agree. */
export const BOOKING = [
  [/calendly\.com/i, "Calendly"],
  [/acuityscheduling|squarespace\.com\/scheduling/i, "Acuity"],
  [/mindbodyonline|mindbody/i, "Mindbody"],
  [/vagaro\.com/i, "Vagaro"],
  [/gingrapp|gingr\.com/i, "Gingr"],
  [/booksy\.com/i, "Booksy"],
  [/setmore\.com/i, "Setmore"],
  [/timetopet\.com/i, "TimeToPet"],
  [/precisepetcare\.com/i, "PrecisePetCare"],
  [/pawpartner|paw-?partner/i, "PawPartner"],
  [/square\.site|squareup\.com\/appointments/i, "Square Appointments"],
  [/schedulicity|simplybook|appointy|bookeo/i, "scheduling widget"],
  [/\b(?:book|schedule|request)\s+(?:a\s+)?(?:free\s+)?(?:consult(?:ation)?|evaluation|assessment|discovery call)\b/i,
    "book-a-consultation copy"],
];

/** Third-party form embeds. A native <form> is detected separately, structurally. */
export const FORM_EMBEDS = [
  [/typeform\.com/i, "Typeform"],
  [/jotform\.com/i, "Jotform"],
  [/formstack|wufoo|gravityforms|formidable/i, "form builder"],
  [/hsforms\.net|js\.hsforms/i, "HubSpot form"],
];

/**
 * Off-site profiles, matched only against href/src/content attribute values.
 *
 * Matching bare strings anywhere in the HTML produces false positives that look
 * convincing: facebook.com/2008/fbml is the XHTML namespace declaration on a great
 * many pages, and bbb.org/inc/legacy.js is a script path, not a BBB listing.
 */
export const PROFILES = [
  ["facebook", (u) => /(?:^|\/\/|\.)facebook\.com\//i.test(u) && !/\/(?:2008|sharer|share|plugins|tr\b|dialog|v\d)/i.test(u)],
  ["instagram", (u) => /(?:^|\/\/|\.)instagram\.com\/[^/?#]+/i.test(u)],
  ["youtube", (u) => /youtube\.com\/(?:c\/|channel\/|user\/|@)/i.test(u)],
  ["tiktok", (u) => /tiktok\.com\/@/i.test(u)],
  ["linkedin", (u) => /linkedin\.com\/(?:in|company|pub)\//i.test(u)],
  ["yelp", (u) => /yelp\.com\/biz\//i.test(u)],
  ["twitter", (u) => /(?:twitter\.com|(?:\/\/|^)x\.com)\/(?!intent|share|i\/)[A-Za-z0-9._]+/i.test(u)],
  ["google_business", (u) => /(?:g\.page|goo\.gl\/maps|google\.com\/maps\/place)/i.test(u)],
];

/** Growth/scale evidence readable from navigation and page copy. */
export const GROWTH = [
  ["hiring", [/\b(?:we(?:'re| are)\s+hiring|join\s+our\s+team|now\s+hiring|careers?)\b/i, "hiring copy"]],
  ["multi_location", [/\b(?:our\s+locations|all\s+locations|choose\s+a\s+location|two\s+locations|second\s+location)\b/i, "multiple locations"]],
  ["pricing_page", [/\b(?:pricing|our\s+rates|packages|program\s+pricing|tuition|investment)\b/i, "pricing/packages"]],
  ["franchise", [/\bfranchis(?:e|ing)\b/i, "franchise"]],
];

const HREF_ATTR = /(?:href|src|content)\s*=\s*["']([^"']+)["']/gi;
const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(?:com|net|org|co|io|us|biz)\b/g;

// Local parts that name a role rather than a person. An address that survives this
// filter is a candidate decision-maker signal; one that doesn't is a front desk.
const ROLE_LOCALS = new Set([
  "info", "contact", "hello", "admin", "office", "support", "sales", "team", "help",
  "booking", "bookings", "book", "inquiries", "inquiry", "enquiries", "training",
  "mail", "email", "service", "services", "customer", "schedule", "scheduling",
  "reservations", "frontdesk", "front", "general", "questions", "appointments",
  "noreply", "no-reply", "webmaster", "billing", "accounts", "hr", "jobs", "careers",
]);

// Hosts that appear in embedded third-party snippets rather than belonging to the
// business, e.g. sentry DSNs and CMS boilerplate.
const NOT_A_BUSINESS_EMAIL = /(sentry|wixpress|example\.|godaddy|squarespace|jquery|schema\.org|w3\.org|shopify|sentry\.io)/i;

function firstMatch(rules, html) {
  for (const [re, token] of rules) if (re.test(html)) return token;
  return null;
}

export function extractUrls(html) {
  return [...html.matchAll(HREF_ATTR)].map((m) => m[1]);
}

export function extractEmails(html) {
  const seen = new Set();
  for (const e of html.match(EMAIL) || []) {
    if (NOT_A_BUSINESS_EMAIL.test(e)) continue;
    if (/\.(?:png|jpe?g|gif|webp|svg)$/i.test(e)) continue;
    seen.add(e.toLowerCase());
  }
  return [...seen];
}

/** Splits addresses into person-shaped and role-shaped. See ROLE_LOCALS. */
export function classifyEmails(emails) {
  const person = [], role = [];
  for (const e of emails) {
    const local = e.split("@")[0].replace(/\d+$/, "");
    if (ROLE_LOCALS.has(local)) role.push(e);
    // A bare name, or a single initial joined to a surname (j.smith, m-jones). The
    // one-character branch is only allowed when a separator and a second part follow,
    // so a lone "j@" stays out.
    else if (/^[a-z]{2,}(?:[._-][a-z]+)?$/.test(local) || /^[a-z][._-][a-z]{2,}$/.test(local)) person.push(e);
    else role.push(e);
  }
  return { person, role };
}

/**
 * A native lead form: a <form> containing an email or phone input. Checked
 * structurally rather than by keyword because "contact us" appears on every site
 * whether or not it can actually capture a lead.
 */
function hasNativeForm(html) {
  for (const m of html.matchAll(/<form\b[\s\S]{0,4000}?<\/form>/gi)) {
    if (/type\s*=\s*["'](?:email|tel)["']/i.test(m[0])) return true;
    if (/name\s*=\s*["'][^"']*(?:email|phone|tel)[^"']*["']/i.test(m[0])) return true;
  }
  return false;
}

/**
 * Everything detectable on one page.
 *
 * Returns only what was found. An absent key means "not observed on this page,"
 * which for a single page is weaker than "the business does not do this" -- the
 * caller aggregates across pages before drawing that conclusion.
 */
export function detectPage(html, pageUrl) {
  const s = {};

  const googleAds = firstMatch(PAID_MEDIA.google_ads, html);
  const metaAds = firstMatch(PAID_MEDIA.meta_ads, html);
  if (googleAds) s.google_ads = googleAds;
  if (metaAds) s.meta_ads = metaAds;

  const martech = MARTECH.filter(([re]) => re.test(html)).map(([, t]) => t);
  if (martech.length) s.martech = martech;

  const booking = firstMatch(BOOKING, html);
  if (booking) s.booking = booking;

  const formEmbed = firstMatch(FORM_EMBEDS, html);
  if (formEmbed) s.lead_form = formEmbed;
  else if (hasNativeForm(html)) s.lead_form = "native form with email/phone input";

  const urls = extractUrls(html);
  const profiles = {};
  for (const [name, test] of PROFILES) {
    const hit = urls.find(test);
    if (hit) profiles[name] = hit.slice(0, 200);
  }
  if (Object.keys(profiles).length) s.profiles = profiles;

  const growth = [];
  for (const [key, [re, token]] of GROWTH) if (re.test(html)) growth.push({ key, token });
  if (growth.length) s.growth = growth;

  const { person, role } = classifyEmails(extractEmails(html));
  if (person.length) s.person_emails = person.slice(0, 5);
  if (role.length) s.role_emails = role.slice(0, 3);

  if (pageUrl) s.page_url = pageUrl;
  return s;
}

/**
 * Rolls per-page signals into the columns on leads.businesses.
 *
 * marketing_score is a 0-100 sum weighted by how much each signal costs the business
 * to have: an ad pixel means someone is paying for traffic, a booking integration
 * means a subscription and a workflow, analytics means someone set up a tag once.
 */
export function aggregate(pages) {
  const all = pages.filter(Boolean);
  const pick = (k) => all.map((p) => p[k]).find(Boolean) || null;

  const google_ads = pick("google_ads");
  const meta_ads = pick("meta_ads");
  const booking = pick("booking");
  const lead_form = pick("lead_form");
  const martech = [...new Set(all.flatMap((p) => p.martech || []))];
  const profiles = Object.assign({}, ...all.map((p) => p.profiles || {}));
  const growth = [...new Map(all.flatMap((p) => p.growth || []).map((g) => [g.key, g])).values()];
  const personEmails = [...new Set(all.flatMap((p) => p.person_emails || []))];
  const roleEmails = [...new Set(all.flatMap((p) => p.role_emails || []))];

  let score = 0;
  if (google_ads) score += 25;
  if (meta_ads) score += 25;
  if (booking) score += 15;
  if (lead_form) score += 10;
  score += Math.min(martech.length * 4, 12);
  // Breadth of social presence, not which platform. Measured across 372 sites: 3+
  // linked platforms roughly doubles median review count and multiplies the Tier 1
  // rate several-fold, while LinkedIn specifically correlates with nothing.
  score += Math.min(Object.keys(profiles).length * 3, 12);
  if (growth.some((g) => g.key === "hiring")) score += 5;
  if (growth.some((g) => g.key === "multi_location")) score += 5;
  score = Math.min(score, 100);

  const evidence = [];
  if (google_ads) evidence.push(`Google Ads (${google_ads})`);
  if (meta_ads) evidence.push(`Meta Ads (${meta_ads})`);
  if (booking) evidence.push(`booking: ${booking}`);
  if (lead_form) evidence.push(`lead form: ${lead_form}`);
  if (martech.length) evidence.push(`martech: ${martech.join(", ")}`);
  const profileNames = Object.keys(profiles);
  if (profileNames.length) evidence.push(`social: ${profileNames.join(", ")}`);
  for (const g of growth) evidence.push(g.token);

  return {
    // Prompt 7: paid advertising is only claimed on a confirmed ad pixel. Analytics,
    // a booking widget and a busy Instagram are all marketing, but none is ad spend.
    //
    // Read this as "paid acquisition infrastructure present", not "currently running
    // ads" -- a pixel survives the campaign that installed it, so this is evidence of
    // intent and capability, not of live spend. See the script header and the
    // "Claiming ad spend" section of docs/ARCHITECTURE.md.
    marketing_active: Boolean(google_ads || meta_ads),
    google_ads_signal: Boolean(google_ads),
    meta_ads_signal: Boolean(meta_ads),
    lead_form_present: Boolean(lead_form),
    booking_present: booking ? true : null,
    marketing_score: score,
    marketing_evidence: evidence.length ? evidence.join("; ") : null,
    profiles,
    growth,
    person_emails: personEmails,
    role_emails: roleEmails,
  };
}
