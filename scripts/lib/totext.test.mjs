#!/usr/bin/env node
// Regression tests for toText() in enrich-websites.mjs, which produces the
// text_excerpt that extract-emails.mjs reads.
//
// These exist because of one specific bug. toText stripped every tag before writing
// text_excerpt, so an address published only as <a href="mailto:...">Email us</a> --
// the ordinary shape of a contact page -- was destroyed on the way into the database.
// The crawler's own signal extractor read raw HTML and did see it, so the address sat
// in website_crawl.signals while businesses.contact_email stayed null for 496
// businesses, 126 of them Tier 1. Nothing failed; the data just was not there.
//
//   node scripts/lib/totext.test.mjs

import { toText, usableBody } from "../enrich-websites.mjs";

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.error(`FAIL  ${name}`);
}

// --- the bug this file exists for ---------------------------------------------------

const linkOnly = toText('<p>Questions? <a href="mailto:erin@littlewolf.com">Email us</a></p>');
ok("an address only in a mailto: href survives the tag strip",
   linkOnly.includes("erin@littlewolf.com"));

ok("the link text is still kept", linkOnly.includes("Email us"));

// Case is normalized on the way out, since extraction lowercases and dedupes.
ok("a mailto: address is lowercased",
   toText('<a href="MAILTO:Becky@PuppyManners.com">mail</a>')
     .includes("becky@puppymanners.com"));

// Real markup puts whitespace and query parameters in there.
ok("a subject query parameter does not corrupt the address",
   toText('<a href="mailto:info@k9.com?subject=Hello%20there">x</a>').includes("info@k9.com"));
ok("leading whitespace after the scheme is tolerated",
   toText('<a href="mailto: sue@k9.com">x</a>').includes("sue@k9.com"));

// A page listing several trainers must not lose all but the first.
const many = toText([1, 2, 3].map((i) => `<a href="mailto:t${i}@dogs.com">T${i}</a>`).join(""));
ok("several addresses on one page all survive",
   ["t1@dogs.com", "t2@dogs.com", "t3@dogs.com"].every((e) => many.includes(e)));

// Deduplicated: a header/footer address repeated on every page should not be emitted
// once per occurrence and eat the excerpt budget.
const repeated = toText('<a href="mailto:a@x.com">1</a><a href="mailto:a@x.com">2</a>');
ok("a repeated address appears once in the prefix",
   (repeated.match(/a@x\.com/g) || []).length === 1);

// The prefix is capped; a staff directory must not crowd out the prose Phase C reads.
const fifteen = toText(Array.from({ length: 15 }, (_, i) =>
  `<a href="mailto:p${i}@x.com">P</a>`).join(""));
ok("the prefix is capped at ten addresses",
   (fifteen.match(/p\d+@x\.com/g) || []).length === 10);

// Prefixed, not appended: the caller truncates at EXCERPT_CHARS, so a long page would
// cut a suffix off entirely.
ok("addresses come first so truncation cannot drop them",
   toText(`<a href="mailto:first@x.com">x</a><p>${"filler ".repeat(500)}</p>`)
     .indexOf("first@x.com") < 40);

// --- the existing behaviour that must not regress ------------------------------------

ok("script bodies are removed",
   !toText("<script>var e='hidden@x.com';</script><p>Hi</p>").includes("hidden@x.com"));
ok("style bodies are removed", !toText("<style>.a{color:red}</style><p>Hi</p>").includes("color"));
ok("comments are removed", !toText("<!-- secret --><p>Hi</p>").includes("secret"));

// A NUL reaching Postgres aborts the whole transaction; this once discarded a
// completed 1,542-site crawl.
ok("C0 control characters are stripped", !/[\u0000-\u0008]/.test(toText("<p>a\u0000b</p>")));

ok("entities are decoded", toText("<p>Bob&nbsp;&amp;&nbsp;Sue</p>").includes("Bob & Sue"));
ok("whitespace is collapsed", !/ {2}/.test(toText("<p>a</p>\n\n\n   <p>b</p>")));
ok("a page with no mail link gains no prefix", toText("<p>Hello</p>") === "Hello");
ok("empty input is handled", toText("") === "");

// --- usableBody: status is not the test, but neither is body size -------------------
//
// Some sites -- WordPress behind a caching proxy -- answer GET with 403 while serving
// their complete homepage, so gating on status discards real content. But an elaborately
// styled error page is also well over 500 bytes of markup, so gating on body size lets
// it through: that mistake stored 230 nginx 403 stubs as successful crawls, each about
// 87 characters of text. The test has to be made on the extracted prose.

const realBody = `<html><head><title>Brooklyn Pawffice</title></head><body>` +
  `<p>We are a dog daycare and boarding facility in Williamsburg. ${"Our trainers work with every dog. ".repeat(40)}</p>` +
  `</body></html>`;

// The stub below is the real shape observed in the corpus: plenty of markup, almost no text.
const styledErrorBody = `<html><head><title>403 - Forbidden</title>` +
  `<style>${"/* padding */".repeat(80)}</style></head>` +
  `<body><div class="bg_error_lines"></div><div class="circle_dots"></div>` +
  `<h1>403 - Forbidden</h1><p>Access to this page is forbidden.</p>` +
  `<div class="clouds_shape"></div></body></html>`;

ok("a 403 serving the real homepage is usable", usableBody({ status: 403, body: realBody }));
ok("a 200 is usable without inspecting text", usableBody({ status: 200, body: realBody }));
ok("a 301 is usable", usableBody({ status: 301, body: realBody }));

// The regression that matters: this passed the old body-length check.
ok("a styled 403 error page is NOT usable", !usableBody({ status: 403, body: styledErrorBody }));
ok("a bare nginx 403 is not usable",
   !usableBody({ status: 403, body: `<html><head><title>403 Forbidden</title></head><body>` +
     `<center><h1>403 Forbidden</h1></center><hr><center>nginx</center>${"<!-- x -->".repeat(60)}</body></html>` }));

ok("a Cloudflare challenge is not usable",
   !usableBody({ status: 403, body: `<html><title>Just a moment...</title><body><p>Checking your browser before accessing.</p>${"<div></div>".repeat(80)}</body></html>` }));
ok("a captcha page is not usable",
   !usableBody({ status: 403, body: `<html><body><p>Please complete the captcha to continue.</p>${"<div></div>".repeat(80)}</body></html>` }));
ok("a parked domain is not usable",
   !usableBody({ status: 403, body: `<html><body><p>This domain is parked and may be for sale.</p>${"<div></div>".repeat(80)}</body></html>` }));

// A themed 404 is long -- it carries the site's whole nav -- but is still not content.
ok("a themed 404 with full site navigation is not usable",
   !usableBody({ status: 404, body: `<html><head><title>404 page | Doggie District</title></head><body>` +
     `<nav>Find a Location Book Now About Us Contact Us Blog FAQ Careers Dog Daycare Boarding Grooming Training ${"Locations ".repeat(60)}</nav>` +
     `</body></html>` }));
ok("a Sucuri firewall notice is not usable",
   !usableBody({ status: 403, body: `<html><body><h1>Sucuri WebSite Firewall - Not Configured</h1><p>The site you requested is not configured.</p>${"<div></div>".repeat(80)}</body></html>` }));

// Cloudflare's DNS-failure interstitial is served as 409 with the site's own name in
// the title, which makes it look convincingly like a real page.
ok("a Cloudflare DNS resolution error is not usable",
   !usableBody({ status: 409, body: `<html><head><title>DNS resolution error | www.masterdog-training.com | Cloudflare</title></head>` +
     `<body><p>Please enable cookies. Error 1001 Ray ID: a3dceaddca5d950f</p>${"<div></div>".repeat(80)}</body></html>` }));

// Short bodies are error stubs regardless of status.
ok("a tiny body is not usable", !usableBody({ status: 200, body: "<html></html>" }));
ok("an empty body is not usable", !usableBody({ status: 200, body: "" }));
ok("a missing body is not usable", !usableBody({ status: 0, body: null }));

// The marker check reads only the opening of the text: a real page may discuss refused
// access in a policy section without being a block page.
ok("a block phrase far into real prose does not disqualify the page",
   usableBody({ status: 403, body: realBody.replace("</body>", "<p>access denied</p></body>") }));

// The caller passes the text it already computed; that path must agree with the
// internal one, or the crawler and these tests would be checking different things.
ok("a caller-supplied text argument is honoured",
   usableBody({ status: 403, body: styledErrorBody }, toText(realBody)));

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
