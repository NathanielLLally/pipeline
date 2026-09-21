#!/usr/bin/env node
// Tests for the RDAP parsing rules. No network.
//
// The fixtures are the shapes actually returned by Verisign, PIR and GoDaddy during
// the hit-rate sample, trimmed to the fields the parser reads. They matter because the
// failure mode here is silent and expensive: a privacy proxy that slips through puts a
// vendor's forwarding address into a send list, and every RDAP record on earth carries
// `abuse@<registrar>`, so a parser that does not exclude it "finds" GoDaddy's abuse
// desk once per domain.
//
//   node scripts/lib/rdap.test.mjs

import {
  toDomain, vcardEmails, vcardName, contactFrom, registrarLink,
  localIsDomain, PLATFORM_HOST, NO_YIELD_REGISTRAR,
  VENDOR_DOMAIN, LEGACY_ISP_DOMAIN, localMatchesRegistrant,
} from "./rdap.mjs";

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.error(`FAIL  ${name}`);
}

const vcard = (fields) => ["vcard", [["version", {}, "text", "4.0"], ...fields]];
const entity = (roles, fields) => ({ roles, vcardArray: vcard(fields) });

// --- toDomain -----------------------------------------------------------------------

ok("a bare host passes through", toDomain("example.com") === "example.com");
ok("the scheme is stripped", toDomain("https://example.com") === "example.com");
ok("www is stripped", toDomain("https://www.example.com/contact") === "example.com");
ok("case is normalized", toDomain("HTTP://WWW.Example.COM") === "example.com");
// RDAP is keyed on the registrable domain; a subdomain 404s at the registry.
ok("a subdomain is trimmed to the registrable domain",
   toDomain("https://shop.example.com") === "example.com");
ok("a deep subdomain is trimmed too",
   toDomain("https://a.b.c.example.com") === "example.com");
ok("a hostless string is rejected", toDomain("localhost") === null);
ok("an empty value is rejected", toDomain("") === null);
ok("null is rejected", toDomain(null) === null);

// --- vcard readers ------------------------------------------------------------------

const person = entity(["registrant"], [
  ["fn", {}, "text", "Desiree Lomer"],
  ["org", { type: "work" }, "text", "Arlington Dog Nanny"],
  ["email", { type: "work" }, "text", "Desiree.Lomer@Gmail.com"],
]);

ok("an email is read out of a jCard", vcardEmails(person)[0] === "desiree.lomer@gmail.com");
ok("the address is lowercased", !/[A-Z]/.test(vcardEmails(person)[0]));
ok("fn and org are both captured", vcardName(person) === "Desiree Lomer / Arlington Dog Nanny");
ok("an entity with no vcard yields no emails", vcardEmails({ roles: ["registrant"] }).length === 0);
ok("an entity with no vcard yields a null name", vcardName({}) === null);

// --- contactFrom: the outcomes ------------------------------------------------------

// The common good case.
ok("a registrant address is found",
   contactFrom({ entities: [person] }).outcome === "found");
ok("the found address is returned",
   contactFrom({ entities: [person] }).email === "desiree.lomer@gmail.com");
ok("the registrant name comes with it",
   contactFrom({ entities: [person] }).registrant.includes("Desiree Lomer"));

// 62 of 92 answered domains in the sample looked like this: entities present, contacts
// redacted under ICANN's post-GDPR policy.
ok("a record with no contact entity is redacted",
   contactFrom({ entities: [entity(["registrar"], [["fn", {}, "text", "GoDaddy.com, LLC"]])] }).outcome === "redacted");
ok("a record with no entities at all is redacted",
   contactFrom({ entities: [] }).outcome === "redacted");
ok("a missing entities key is redacted", contactFrom({}).outcome === "redacted");
ok("a null document is redacted", contactFrom(null).outcome === "redacted");

// 23 of 92. This is the fixture Verisign/GoDaddy actually returned.
const proxied = {
  entities: [
    entity(["registrant"], [
      ["kind", {}, "text", "org"],
      ["fn", {}, "text", "Registration Private"],
      ["org", { type: "work" }, "text", "Domains By Proxy, LLC"],
    ]),
    entity(["technical"], [
      ["fn", {}, "text", "Registration Private"],
      ["org", { type: "work" }, "text", "Domains By Proxy, LLC"],
    ]),
    entity(["registrar"], [
      ["fn", {}, "text", "GoDaddy.com, LLC"],
      ["email", { type: "work" }, "text", "abuse@godaddy.com"],
    ]),
  ],
};
ok("a Domains By Proxy registrant is privacy, not found",
   contactFrom(proxied).outcome === "privacy");
ok("a privacy record returns no address", contactFrom(proxied).email === null);

// The proxy's own forwarding address is the trap: it is a real, deliverable mailbox
// that reaches the vendor rather than the business.
ok("a proxy forwarding address is privacy, not found",
   contactFrom({ entities: [entity(["registrant"], [
     ["fn", {}, "text", "Withheld for Privacy ehf"],
     ["email", {}, "text", "abc123@withheldforprivacy.com"],
   ])] }).outcome === "privacy");
ok("a jewellaprivacy address is caught",
   contactFrom({ entities: [entity(["registrant"], [
     ["fn", {}, "text", "Private Registrant"],
     ["email", {}, "text", "narniapets.com@dnic.jewellaprivacy.com"],
   ])] }).outcome === "privacy");
ok("a WhoisGuard address is caught",
   contactFrom({ entities: [entity(["registrant"], [
     ["fn", {}, "text", "WhoisGuard Protected"],
     ["email", {}, "text", "x@whoisguard.com"],
   ])] }).outcome === "privacy");
ok("a REDACTED FOR PRIVACY name is caught",
   contactFrom({ entities: [entity(["registrant"], [
     ["fn", {}, "text", "REDACTED FOR PRIVACY"],
     ["email", {}, "text", "someone@example-proxy.com"],
   ])] }).outcome === "privacy");

// Every record carries this by policy; without the exclusion the pass "finds" 1,100
// copies of one abuse desk.
ok("a registrar abuse address alone is junk, not found",
   contactFrom({ entities: [entity(["technical"], [
     ["fn", {}, "text", "Register.com"],
     ["email", {}, "text", "abuse@register.com"],
   ])] }).outcome === "junk");
ok("a whois-request address is junk",
   contactFrom({ entities: [entity(["technical"], [
     ["fn", {}, "text", "PIR"],
     ["email", {}, "text", "WHOISrequest@pir.org"],
   ])] }).outcome === "junk");

// A real address alongside a redacted proxy entity should still be reported: the
// domain IS reachable, and that is the fact we want.
ok("a real technical address is found even when the registrant is proxied",
   contactFrom({ entities: [
     entity(["registrant"], [["fn", {}, "text", "Domains By Proxy, LLC"]]),
     entity(["technical"], [["fn", {}, "text", "Bob"], ["email", {}, "text", "bob@k9academy.com"]]),
   ] }).email === "bob@k9academy.com");

// Registrant outranks technical: the technical contact is often the web developer.
ok("the registrant is preferred over the technical contact",
   contactFrom({ entities: [
     entity(["technical"], [["fn", {}, "text", "Dev"], ["email", {}, "text", "dev@agency.com"]]),
     entity(["registrant"], [["fn", {}, "text", "Owner"], ["email", {}, "text", "owner@k9.com"]]),
   ] }).email === "owner@k9.com");

// --- privacy addresses that a name-based check alone lets through ---------------------
//
// Every one of these came out of a live dry run against the corpus, having passed the
// first version of the filter. They are deliverable mailboxes, which is exactly what
// makes them dangerous: they survive validation and reach a privacy vendor instead of
// the prospect.

const proxyAddr = (email, fn = "Domain Administrator") =>
  contactFrom({ entities: [entity(["registrant"], [
    ["fn", {}, "text", fn], ["email", {}, "text", email],
  ])] }, "example.com");

ok("a privacyguardian.org hashed alias is privacy",
   proxyAddr("pwp-b52a8e864f035ff4484cfe31202b07f0@privacyguardian.org").outcome === "privacy");
ok("a whoisprotection.cc numbered alias is privacy",
   proxyAddr("reg_21375655@whoisprotection.cc").outcome === "privacy");
ok("a bluehost pending-deletion placeholder is privacy",
   proxyAddr("pendingrenewalordeletion@bluehostprivatename.com").outcome === "privacy");
ok("info@privatename.com is privacy despite a role local part",
   proxyAddr("info@privatename.com").outcome === "privacy");

// The local part being the domain is the signature of a per-domain forwarder.
ok("localIsDomain recognizes a per-domain forwarder",
   localIsDomain("narniapets.com@dnic.jewellaprivacy.com", "narniapets.com"));
ok("localIsDomain is false for an ordinary address",
   !localIsDomain("sarah@narniapets.com", "narniapets.com"));
ok("a per-domain forwarding alias is privacy",
   contactFrom({ entities: [entity(["registrant"], [
     ["fn", {}, "text", "Registrant"], ["email", {}, "text", "narniapets.com@dnic.example.net"],
   ])] }, "narniapets.com").outcome === "privacy");

// The shape rules must not eat legitimate addresses.
ok("an ordinary personal address still passes the shape rules",
   proxyAddr("sarah@auntiesdawghouse.com", "Sarah Miller").outcome === "found");
ok("a hyphenated human name is not mistaken for a proxy alias",
   proxyAddr("mary-beth@k9academy.com", "Mary Beth").outcome === "found");
ok("a role address on the business's own domain still passes",
   proxyAddr("info@auntiesdawghouse.com", "Auntie's Dawg House").outcome === "found");

// Link-in-bio and social hosts: probing them returns the platform's domain desk.
ok("beacons.ai is recognized as a platform host", PLATFORM_HOST.test("beacons.ai"));
ok("instagram.com is recognized as a platform host", PLATFORM_HOST.test("instagram.com"));
ok("linktr.ee is recognized as a platform host", PLATFORM_HOST.test("linktr.ee"));
ok("a real business domain is not a platform host", !PLATFORM_HOST.test("auntiesdawghouse.com"));

// A second dry run, after the shape rules were added, leaked these two. Neither name
// nor local part gives them away -- `info@domain-contact.org` reads like an ordinary
// role address -- so the proxy's own domain has to be recognized.
ok("an anonymised.email forwarder is privacy",
   proxyAddr("myportfolio.com-registrant@anonymised.email").outcome === "privacy");
ok("a domain-contact.org role address is privacy",
   proxyAddr("info@domain-contact.org").outcome === "privacy");
ok("a -registrant suffix is recognized regardless of domain",
   proxyAddr("somesite.com-registrant@someforwarder.net").outcome === "privacy");

// --- third-party vendor domains -----------------------------------------------------
//
// An RDAP-specific hazard with no crawl equivalent: web developers and IT shops
// register domains for clients and leave their own address as the registrant. All of
// these reached leads.business_email in the first live run before the rule existed.

ok("an IT vendor domain is recognized", VENDOR_DOMAIN.test("milesit.com"));
ok("a web-design agency domain is recognized", VENDOR_DOMAIN.test("imatrix.com"));
ok("a site-builder domain is recognized", VENDOR_DOMAIN.test("weebly.com"));
ok("a host's domain is recognized", VENDOR_DOMAIN.test("bluehost.com"));
ok("a real business domain is not a vendor domain", !VENDOR_DOMAIN.test("fitdog.com"));
ok("a subdomain of a vendor is caught", VENDOR_DOMAIN.test("mail.weebly.com"));

// Legacy ISP mailboxes are consumer addresses, not vendors -- emails.mjs's FREE_MAIL
// list predates them, so without this they read as suspicious third-party domains.
ok("pacbell.net is a consumer mailbox", LEGACY_ISP_DOMAIN.test("pacbell.net"));
ok("netzero.com is a consumer mailbox", LEGACY_ISP_DOMAIN.test("netzero.com"));
ok("qwest.net is a consumer mailbox", LEGACY_ISP_DOMAIN.test("qwest.net"));
ok("a business domain is not a legacy ISP", !LEGACY_ISP_DOMAIN.test("flashdogtraining.com"));

// The rescue: an owner running two businesses registers one under the other's domain.
ok("an address naming the registrant is theirs",
   localMatchesRegistrant("robert@rawk9food.com", "Rodriguez, robert"));
ok("a first-name match counts",
   localMatchesRegistrant("sarah@otherbiz.com", "Sarah Westcott"));
ok("a dotted local part is split before matching",
   localMatchesRegistrant("jane.doe@otherbiz.com", "Jane Doe"));
ok("an unrelated local part does not match",
   !localMatchesRegistrant("domains1@imatrix.com", "Chuck Hoover"));
ok("a generic local part does not match",
   !localMatchesRegistrant("questions@weebly.com", "c/o Weebly Domains"));
ok("a null registrant never matches", !localMatchesRegistrant("bob@x.com", null));
// Two characters would match an initial against almost any name.
ok("a two-character local part does not match", !localMatchesRegistrant("jo@x.com", "John Smith"));

// --- the no-yield registrar list ----------------------------------------------------
//
// Measured, not assumed: these registrars bundle privacy registration, so over two
// samples they returned a proxy or a redaction on every single domain. The list is a
// cost optimisation only -- it must never swallow a registrar that does publish.

ok("GoDaddy is on the no-yield list", NO_YIELD_REGISTRAR.test("rdap.godaddy.com"));
ok("Wild West Domains (GoDaddy reseller) is on the list",
   NO_YIELD_REGISTRAR.test("rdap.wildwestdomains.com"));
ok("Namecheap is on the no-yield list", NO_YIELD_REGISTRAR.test("rdap.namecheap.com"));
ok("Squarespace is on the no-yield list", NO_YIELD_REGISTRAR.test("rdap.squarespace.domains"));
ok("Wix is on the no-yield list", NO_YIELD_REGISTRAR.test("rdap.wix.com"));

// These produced every address the samples recovered; skipping them would zero the pass.
ok("web.com is NOT skipped", !NO_YIELD_REGISTRAR.test("rdap.web.com"));
ok("Network Solutions is NOT skipped", !NO_YIELD_REGISTRAR.test("rdap.networksolutions.com"));
ok("Amazon Registrar is NOT skipped", !NO_YIELD_REGISTRAR.test("rdap.registrar.amazon.com"));
ok("NameSilo is NOT skipped", !NO_YIELD_REGISTRAR.test("rdap.namesilo.com"));
ok("Tucows/OpenSRS is NOT skipped", !NO_YIELD_REGISTRAR.test("opensrs.rdap.tucows.com"));
ok("Porkbun is NOT skipped", !NO_YIELD_REGISTRAR.test("cart-before.porkbun.horse"));

// --- registrarLink ------------------------------------------------------------------
//
// Verisign's .com records are thin: contacts live on the registrar's server, one hop
// on. Without this the pass reports `redacted` for every .com in the corpus.

const thin = {
  links: [
    { href: "https://rdap.verisign.com/com/v1/domain/robsdogs.com" },
    { href: "https://rdap.godaddy.com/v1/domain/ROBSDOGS.COM" },
  ],
};
ok("the registrar's own RDAP server is found",
   registrarLink(thin, "https://rdap.verisign.com/com/v1/") === "https://rdap.godaddy.com/v1/domain/ROBSDOGS.COM");
ok("the registry's own self-link is not followed",
   registrarLink({ links: [{ href: "https://rdap.verisign.com/com/v1/domain/x.com" }] },
                 "https://rdap.verisign.com/com/v1/") === null);
ok("a non-rdap link is ignored",
   registrarLink({ links: [{ href: "https://www.icann.org/wicf" }] }, "https://rdap.verisign.com/com/v1/") === null);
// An http:// hop would send the query, and our user-agent, in clear text.
ok("a plain-http link is not followed",
   registrarLink({ links: [{ href: "http://rdap.example.com/domain/x.com" }] }, "https://rdap.verisign.com/com/v1/") === null);
ok("a document with no links yields null", registrarLink({}, "https://rdap.verisign.com/com/v1/") === null);
ok("a malformed link entry does not throw",
   registrarLink({ links: [{}, { href: 42 }] }, "https://x/") === null);

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
