#!/usr/bin/env node
// Unit tests for email extraction. No network; the fixtures are shapes taken from real
// crawled pages in leads.website_crawl.
//
//   node scripts/lib/emails.test.mjs

import {
  extractFromText, classify, rejectReason, confidenceFor, bestOf, domainsMatch, siteHost,
} from "./emails.mjs";

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.error(`FAIL  ${name}`);
}
function eq(name, actual, expected) {
  ok(`${name} (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`,
     JSON.stringify(actual) === JSON.stringify(expected));
}

// --- extraction ------------------------------------------------------------------

eq("finds a plain address",
   extractFromText("Email us at erin@littlewolftraining.com today", "littlewolftraining.com")
     .map((r) => r.email),
   ["erin@littlewolftraining.com"]);

// Trailing punctuation is the single most common way an address gets mangled in prose.
eq("strips a trailing period",
   extractFromText("Contact bob@example-dogs.com.", "example-dogs.com").map((r) => r.email),
   ["bob@example-dogs.com"]);
eq("strips a trailing paren",
   extractFromText("(reach us: sue@k9.com)", "k9.com").map((r) => r.email),
   ["sue@k9.com"]);

eq("lowercases", extractFromText("Becky@PuppyManners.com", "puppymanners.com").map((r) => r.email),
   ["becky@puppymanners.com"]);

eq("deduplicates within a page",
   extractFromText("a@b-dogs.com and again a@b-dogs.com", "b-dogs.com").length, 1);

eq("empty text yields nothing", extractFromText("", "x.com"), []);
eq("null text yields nothing", extractFromText(null, "x.com"), []);

// --- rejection: the precision-over-recall rules ----------------------------------

// This one is not hypothetical: filler@godaddy.com was 86 of 88 junk matches in the
// first measured pass, present on every unconfigured parked domain.
ok("rejects the GoDaddy placeholder", rejectReason("filler@godaddy.com") !== null);
ok("rejects noreply", rejectReason("noreply@realbusiness.com") !== null);
ok("rejects example.com", rejectReason("johnsmith@example.com") !== null);
ok("rejects wix infrastructure", rejectReason("abc@sentry.wixpress.com") !== null);
// Image sprites named logo@2x.png are why the TLD must be alphabetic.
ok("rejects an asset filename", rejectReason("logo@2x.png") !== null);
ok("rejects a hex tracking token", rejectReason("a1b2c3d4e5f60718@cdn.com") !== null);
ok("rejects a local part with no letters", rejectReason("12345@x.com") !== null);
ok("keeps an ordinary business address", rejectReason("info@dogtraining.com") === null);
ok("keeps a personal address", rejectReason("erin@littlewolftraining.com") === null);
ok("keeps a gmail address", rejectReason("kraftyk9dogsports@gmail.com") === null);

eq("junk is filtered out of extraction",
   extractFromText("filler@godaddy.com but also real@mydogs.com", "mydogs.com").map((r) => r.email),
   ["real@mydogs.com"]);

// An @ in minified CSS must not become a contact address.
eq("minified source yields nothing usable",
   extractFromText("@media(max-width:64px){a@2x.png}", "x.com"), []);

// --- classification ---------------------------------------------------------------

const role = classify("info@k9drivenchicago.com", "https://www.k9drivenchicago.com/");
ok("info@ is a role address", role.is_role);
ok("role address on own domain matches", role.matches_site_domain);
ok("own domain is not free mail", !role.is_free_mail);

const personal = classify("erin@littlewolftraining.com", "littlewolftraining.com");
ok("a first name is not a role address", !personal.is_role);
ok("personal address on own domain matches", personal.matches_site_domain);

const free = classify("kraftyk9dogsports@gmail.com", "kraftyk9.com");
ok("gmail is free mail", free.is_free_mail);
ok("gmail does not match the site domain", !free.matches_site_domain);

// www. and a subdomain must not defeat the domain match.
ok("www is ignored when matching", domainsMatch("puppymanners.com", "www.puppymanners.com"));
ok("subdomain still matches", domainsMatch("mail.puppymanners.com", "puppymanners.com"));
ok("different domains do not match", domainsMatch("gmail.com", "puppymanners.com") === false);

eq("siteHost strips scheme and www", siteHost("https://www.Example.com/about"), "example.com");
eq("siteHost rejects junk", siteHost("(555) 123-4567"), null);

// --- confidence -------------------------------------------------------------------

// The ordering is what matters, not the absolute numbers: a named person on the
// business's own domain, found on the contact page, must outrank everything else.
const bestCase = confidenceFor(classify("erin@littlewolftraining.com", "littlewolftraining.com"), "contact");
const roleCase = confidenceFor(classify("info@littlewolftraining.com", "littlewolftraining.com"), "contact");
const freeCase = confidenceFor(classify("someone@gmail.com", "littlewolftraining.com"), "contact");
ok("personal on own domain beats role on own domain", bestCase > roleCase);
ok("role on own domain beats a free-mail address", roleCase > freeCase);
ok("confidence stays within bounds", bestCase <= 100 && freeCase >= 0);

// Page position matters: the same address is better evidence on the contact page.
const onContact = confidenceFor(classify("erin@x-dogs.com", "x-dogs.com"), "contact");
const onPricing = confidenceFor(classify("erin@x-dogs.com", "x-dogs.com"), "pricing");
ok("contact page outranks pricing page", onContact > onPricing);

// --- bestOf ------------------------------------------------------------------------

eq("bestOf picks the highest confidence",
   bestOf([
     { email: "info@x.com", confidence: 70, page_count: 3 },
     { email: "erin@x.com", confidence: 85, page_count: 1 },
   ]).email,
   "erin@x.com");

// A tie goes to the address the business repeats across its site.
eq("bestOf breaks ties on page count",
   bestOf([
     { email: "a@x.com", confidence: 80, page_count: 1 },
     { email: "b@x.com", confidence: 80, page_count: 4 },
   ]).email,
   "b@x.com");

eq("bestOf of nothing is null", bestOf([]), null);

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
