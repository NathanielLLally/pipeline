// RDAP lookup helpers: bootstrap, endpoint selection, and contact extraction.
//
// Split out from the pass itself so the vcard parsing and the privacy-service
// detection can be tested without touching the network. Both are the kind of rule that
// looks obviously right and is quietly wrong on real data -- a privacy proxy that goes
// undetected puts `whoisguard-protected@namecheap.com` in a send list, which is worse
// than finding nothing.

// Privacy and proxy registration services, matched against the whole vcard and against
// the address itself. These are the shapes actually observed in the corpus plus the
// major vendors; the list is deliberately broad, because a false positive here costs
// one lead and a false negative costs a bounce and a spam complaint.
export const PRIVACY_RE =
  /domains?\s?by\s?proxy|whoisguard|privacy\s?-?\s?guardian|privacyguardian|privacyprotect|privacy\s?protect|withheld\s?for\s?privacy|contact\s?privacy|perfect\s?privacy|identity\s?protect|privacy\s?service|private\s?by\s?design|super\s?privacy|whois\s?privacy|whoisprivacy|whoisprotection|data\s?protected|domain\s?protection|proxy\s?protection|redacted|not\s?disclosed|statutory\s?masking|gdpr\s?masked|jewellaprivacy|anonymize|protecteddomainservices|namecheap\.com|withheldforprivacy|privatename|private\s?name|pendingrenewalordeletion|registrant\s?of\b/i;

// Addresses whose *shape* gives them away as machine-issued proxy mailboxes rather
// than something a person chose, regardless of the domain they sit on. The corpus
// produced `pwp-b52a8e864f035ff4484cfe31202b07f0@...` and `reg_21375655@...`: a
// per-domain forwarding alias, which is deliverable and therefore dangerous -- it
// passes every ordinary validity check while reaching a privacy vendor, not the
// prospect. Matched on the local part only.
export const PROXY_LOCAL_SHAPE =
  /^(?:pwp[-_]|reg[-_]\d|whois[-_]|priv[-_]|proxy[-_]|dm[-_][0-9a-f]{8}|[0-9a-f]{24,}$|[a-z]{1,4}[-_][0-9a-f]{16,}$)|[-_]registrant$|^registrant[-_]/i;

// Domains that exist only to receive proxied registrant mail. Unlike the name checks
// above, these give nothing away in the vcard -- `info@domain-contact.org` reads like
// an ordinary role address -- so the domain itself has to be listed.
export const PROXY_DOMAIN =
  /(?:^|\.)(?:anonymised\.email|anonymize\.com|domain-contact\.org|domainsbyproxy\.com|whoisguard\.com|withheldforprivacy\.com|privacyguardian\.org|whoisprotection\.cc|privacyprotect\.org|contactprivacy\.com|identity-protect\.org|privatewhois\.\w+|bluehostprivatename\.com|privatename\.com|namecheap\.com|perfectprivacy\.com|networksolutionsprivateregistration\.com|tieredaccess\.com|protecteddomainservices\.com|privacyadvocate\.org)$/i;

// Web developers, hosting resellers and IT shops that register domains on a client's
// behalf and put *their own* address in the registrant field. This is a hazard specific
// to RDAP: the crawl channel never produces these, because the agency's address is not
// printed on the client's contact page. `purchasing@milesit.com` reaching an IT vendor
// is not a dog-training prospect, and emailing it damages the sender reputation that
// protects every other address in the table.
export const VENDOR_DOMAIN =
  /(?:^|\.)(?:weebly\.com|wix\.com|squarespace\.com|godaddy\.com|imatrix\.com|milesit\.com|web\.com|networksolutions\.com|register\.com|hostgator\.com|bluehost\.com|siteground\.com|hostinger\.com|ionos\.com|1and1\.com|dreamhost\.com|inmotionhosting\.com|webmasters?\.com)$/i;

// Legacy ISP mailboxes. Functionally consumer free-mail -- and common among
// longer-established small businesses -- but missing from emails.mjs's FREE_MAIL list,
// which only knows the modern providers. Listed so they are not mistaken for a
// third-party vendor domain by the caller's own-domain check.
export const LEGACY_ISP_DOMAIN =
  /(?:^|\.)(?:netzero\.com|pacbell\.net|qwest\.net|juno\.com|roadrunner\.com|rr\.com|optonline\.net|windstream\.net|frontier\.com|centurylink\.net|ameritech\.net|swbell\.net|prodigy\.net|mindspring\.com|peoplepc\.com)$/i;

/**
 * True when the address's local part is part of the registrant's own name.
 *
 * This rescues a real case the vendor rule would otherwise discard: an owner who runs
 * two businesses and registered one domain under the other's address. "Dog Trainer Rob"
 * lists `robert@rawk9food.com` with registrant `Rodriguez, robert` -- a third-party
 * domain by the mechanical test, but plainly the owner's own address, and a dog
 * nutrition business at that. Meanwhile `domains1@imatrix.com` under registrant
 * `Chuck Hoover` matches nothing and is a marketing agency's mailbox.
 *
 * Requiring three characters avoids matching an initial against any name containing it.
 */
export function localMatchesRegistrant(email, registrant) {
  if (!registrant) return false;
  const local = email.split("@")[0].toLowerCase().replace(/[._-]+/g, " ").trim();
  if (local.length < 3) return false;
  const name = registrant.toLowerCase();
  return local.split(/\s+/).some((part) => part.length >= 3 && name.includes(part));
}

// A local part that is the domain itself (`narniapets.com@dnic.example`) is the
// signature of a per-domain forwarder, never a mailbox a human picked.
export function localIsDomain(email, domain) {
  const [local] = email.split("@");
  return Boolean(domain) && local.toLowerCase() === domain.toLowerCase();
}

// Addresses belonging to the registrar or the registry rather than the registrant.
// Every RDAP record carries `abuse@<registrar>` by policy, so without this the pass
// would "find" GoDaddy's abuse desk 1,100 times.
export const REGISTRAR_LOCAL =
  /^(?:abuse|compliance|registrar|whois|whoisrequest|whois-request|legal|support|noc|hostmaster|postmaster|admin-c|dnsadmin)$/i;

// Platform domains that appear as a business's "website" when the listing linked a
// social profile instead of a site. Probing instagram.com yields Meta's domain desk.
export const PLATFORM_HOST =
  /(?:^|\.)(?:instagram|facebook|fb|linkedin|twitter|x|tiktok|youtube|yelp|google|sites\.google|wixsite|weebly|squarespace|godaddysites|business\.site|linktr|beacons|carrd|bio\.link|about\.me|nextdoor|rover|wagwalking)\.(?:com|ee|be|me|ai|co|link)$/i;

// Registrars that never expose a registrant address, measured rather than assumed.
//
// Over two samples of the uncovered corpus, these answered with a privacy proxy or a
// redaction every single time: GoDaddy 0 found / 8 privacy, Namecheap 0 / 7 privacy,
// Squarespace 0 / 8 redacted, Wix refused every request. That is not bad luck -- these
// registrars bundle proxy registration by default (GoDaddy ships Domains By Proxy free
// with every domain), so the registrant field is structurally unavailable, not merely
// often missing.
//
// Between them they hold well over half this corpus, and they are also the registrars
// that rate-limit hardest: GoDaddy answers 429 with a 15-second sliding window, Wix
// refuses outright. Skipping them turns the pass from an hours-long fight with rate
// limits into a few minutes against registrars that actually answer, and it costs no
// yield, because the yield was zero.
//
// This is recorded as `skipped_registrar`, NOT as a miss. If one of these ever changes
// policy the domains must be re-askable, so nothing here is written to domain_rdap.
//
// The trailing boundary is `(?:\.|$)`, not `\.`: some registrars' RDAP hostnames end on
// the brand itself (rdap.squarespace.domains), and requiring a following label silently
// exempted them.
export const NO_YIELD_REGISTRAR =
  /(?:^|\.)(?:godaddy|wildwestdomains|namecheap|squarespace|wix|dynadot|spaceship|cloudflare|names4ever|ionos|markmonitor)(?:\.|$)/i;

/**
 * Fetches IANA's RDAP bootstrap file: the authoritative TLD -> registry endpoint map.
 *
 * Using this rather than the rdap.org redirector because rdap.org answers every
 * request from this network with 403. The bootstrap is a single ~100KB fetch and the
 * registries themselves answer fine.
 */
export async function loadBootstrap(fetchImpl = fetch) {
  const res = await fetchImpl("https://data.iana.org/rdap/dns.json", {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`IANA bootstrap returned ${res.status}`);
  const doc = await res.json();
  const map = new Map();
  for (const [tlds, urls] of doc.services || []) {
    // Prefer https; the file lists http first for a handful of registries.
    const url = urls.find((u) => u.startsWith("https://")) || urls[0];
    for (const tld of tlds) map.set(tld.toLowerCase(), url.replace(/\/?$/, "/"));
  }
  return map;
}

/** Strips a stored website URL to the bare registrable host RDAP expects. */
export function toDomain(website) {
  if (!website) return null;
  const raw = String(website).trim();
  if (!raw) return null;
  let host;
  try {
    host = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`).hostname;
  } catch {
    return null;
  }
  host = host.toLowerCase().replace(/^www\./, "");
  if (!host.includes(".")) return null;
  // RDAP is registered against the registrable domain, so a subdomained site
  // (shop.example.com) must be trimmed or the registry returns 404. Two labels is
  // right for .com/.net/.org, which is 96% of this corpus; the multi-part TLDs it gets
  // wrong (.co.uk) simply 404 and are recorded as a miss.
  const labels = host.split(".");
  return labels.length > 2 ? labels.slice(-2).join(".") : host;
}

/** All email values out of an RDAP entity's jCard. */
export function vcardEmails(entity) {
  const out = [];
  for (const field of entity?.vcardArray?.[1] || []) {
    if (field[0] === "email" && typeof field[3] === "string") out.push(field[3].trim().toLowerCase());
  }
  return out;
}

/** The entity's display name and organization, for auditing what a match came from. */
export function vcardName(entity) {
  const parts = [];
  for (const field of entity?.vcardArray?.[1] || []) {
    if ((field[0] === "fn" || field[0] === "org") && typeof field[3] === "string") parts.push(field[3]);
  }
  return parts.join(" / ") || null;
}

/**
 * Reads a contact address out of an RDAP domain object.
 *
 * Returns { outcome, email, registrant }. The outcomes are exclusive and ordered by
 * what the record actually said, not by what we wanted: a record with both a privacy
 * proxy and a usable address reports the address, but one with only a proxy reports
 * `privacy` rather than `redacted`, because those are different facts about the domain
 * and conflating them would hide how much of the corpus is genuinely reachable.
 */
export function contactFrom(doc, domain = null) {
  const entities = doc?.entities || [];
  const contacts = entities.filter((e) =>
    (e.roles || []).some((r) => /^(?:registrant|administrative|technical|abuse)$/i.test(r))
  );
  if (!contacts.length) return { outcome: "redacted", email: null, registrant: null };

  let sawPrivacy = false;
  let sawJunk = false;
  for (const role of ["registrant", "administrative", "technical"]) {
    for (const c of contacts) {
      if (!(c.roles || []).some((r) => r.toLowerCase() === role)) continue;
      const name = vcardName(c);
      const isProxy = PRIVACY_RE.test(JSON.stringify(c.vcardArray || []));
      if (isProxy) sawPrivacy = true;
      for (const email of vcardEmails(c)) {
        const [local] = email.split("@");
        if (REGISTRAR_LOCAL.test(local)) { sawJunk = true; continue; }
        // Three separate privacy tests, because the vendors defeat each other's:
        // the entity may name the service, the address may name it, or neither may
        // while the local part is plainly a machine-issued forwarding alias.
        if (isProxy || PRIVACY_RE.test(email)) { sawPrivacy = true; continue; }
        if (PROXY_LOCAL_SHAPE.test(local) || localIsDomain(email, domain)
            || PROXY_DOMAIN.test(email.split("@")[1] || "")) {
          sawPrivacy = true;
          continue;
        }
        return { outcome: "found", email, registrant: name };
      }
    }
  }
  if (sawPrivacy) return { outcome: "privacy", email: null, registrant: null };
  if (sawJunk) return { outcome: "junk", email: null, registrant: null };
  return { outcome: "redacted", email: null, registrant: null };
}

/**
 * The registrar's own RDAP server, if the registry's answer points at one.
 *
 * Verisign runs a thin registry: its .com/.net records carry nameservers and a
 * registrar link but no contacts at all. The contacts, such as they are, live one hop
 * further on. Without following this link the pass would report `redacted` for every
 * .com in the corpus.
 */
export function registrarLink(doc, registryBase) {
  for (const link of doc?.links || []) {
    const href = link?.href;
    if (typeof href !== "string") continue;
    if (!/^https:\/\//i.test(href)) continue;
    if (!/rdap/i.test(href)) continue;
    if (registryBase && href.toLowerCase().startsWith(registryBase.toLowerCase())) continue;
    return href;
  }
  return null;
}
