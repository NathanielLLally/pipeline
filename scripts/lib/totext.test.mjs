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

import { toText } from "../enrich-websites.mjs";

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

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
