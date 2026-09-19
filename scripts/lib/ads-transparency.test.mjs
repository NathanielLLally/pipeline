#!/usr/bin/env node
// Unit tests for the Ads Transparency decoder. No network: the fixtures below are
// trimmed from real captured responses.
//
//   node scripts/lib/ads-transparency.test.mjs

import { parseCreatives, toDomain, isRecent, requestBody, RECENT_DAYS } from "./ads-transparency.mjs";

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

// --- toDomain ------------------------------------------------------------------

eq("toDomain strips scheme and www", toDomain("https://www.Example.com/about?x=1"), "example.com");
eq("toDomain bare host", toDomain("example.com"), "example.com");
eq("toDomain keeps subdomain", toDomain("http://shop.example.co.uk/"), "shop.example.co.uk");
eq("toDomain rejects empty", toDomain(""), null);
eq("toDomain rejects null", toDomain(null), null);
// Rows in this database really do hold phone numbers in the website column.
eq("toDomain rejects a phone number", toDomain("(555) 123-4567"), null);
eq("toDomain rejects a dotless label", toDomain("localhost"), null);
eq("toDomain rejects a raw IP", toDomain("192.168.1.1"), null);

// --- requestBody ---------------------------------------------------------------

ok("requestBody is form-encoded under f.req", requestBody("example.com").startsWith("f.req="));
ok("requestBody url-encodes the JSON", !requestBody("example.com").includes("{"));
ok("requestBody carries the domain",
   decodeURIComponent(requestBody("example.com")).includes('"1":"example.com"'));
// 3.12.2 is what scopes the search to a domain; losing it silently matches nothing.
ok("requestBody keeps the domain-scope flag",
   decodeURIComponent(requestBody("example.com")).includes('"2":true'));

// --- parseCreatives: the empty case --------------------------------------------

// This is the single most important assertion in the file. `{}` is a SUCCESSFUL
// response meaning "runs no ads" -- misreading it as an error cost an earlier session
// its entire conclusion about this endpoint.
eq("empty object means no ads, not an error", parseCreatives({}).found, false);
eq("empty object yields no creatives", parseCreatives({}).creatives, []);
eq("null response is handled", parseCreatives(null).found, false);
eq("empty ad array is handled", parseCreatives({ "1": [] }).found, false);

// --- parseCreatives: a real response -------------------------------------------

// Trimmed from the live nike.com response, with a second creative added so the
// min/max date folding is actually exercised.
const NIKE = {
  "1": [
    {
      "1": "AR16735076323512287233",
      "2": "CR04565460797249028097",
      "3": { "3": { "2": "<img src=\"https://tpc.googlesyndication.com/archive/simgad/2815\">" }, "5": true },
      "4": 1,
      "6": { "1": "1700171600", "2": 87516000 },
      "7": { "1": "1789842712", "2": 24726000 },
      "12": "Nike, Inc.",
      "13": 899,
      "14": "nike.com",
    },
    {
      "1": "AR16735076323512287233",
      "2": "CR1750404634047",
      "3": { "1": ["something"] },
      "4": 2,
      "6": { "1": "1690000000" },
      "7": { "1": "1789900000" },
      "12": "Nike, Inc.",
      "14": "nike.com",
    },
  ],
};

const nike = parseCreatives(NIKE);
eq("real response is found", nike.found, true);
eq("creative count", nike.creative_count, 2);
eq("advertiser id", nike.advertiser_id, "AR16735076323512287233");
eq("advertiser name", nike.advertiser_name, "Nike, Inc.");
eq("domain", nike.domain, "nike.com");
eq("formats are distinct and sorted", nike.formats, ["1", "2"]);
// first_shown must be the EARLIEST and last_shown the LATEST across all creatives;
// taking the first row's values would misreport how long the advertiser has been live.
eq("first_shown folds to the earliest", nike.first_shown, new Date(1690000000 * 1000).toISOString());
eq("last_shown folds to the latest", nike.last_shown, new Date(1789900000 * 1000).toISOString());
// Creative markup is large and nobody reads it; only the ids are kept as evidence.
ok("creative markup is not stored", !JSON.stringify(nike.creatives).includes("img src"));
eq("creative ids are kept", nike.creatives.map((c) => c.creative_id),
   ["CR04565460797249028097", "CR1750404634047"]);
// Two creatives is well under the 40-row page size, so nothing was cut off.
eq("not truncated below page size", nike.truncated, false);

// --- parseCreatives: malformed and hostile input -------------------------------

// A garbage epoch must not become a plausible-looking timestamp in a timestamptz
// column, where nothing downstream would ever question it.
const BAD_DATE = { "1": [{ "2": "CR1", "6": { "1": "0" }, "7": { "1": "99999999999999" } }] };
const bad = parseCreatives(BAD_DATE);
eq("implausible epochs are rejected", [bad.first_shown, bad.last_shown], [null, null]);
eq("a creative with no usable date still counts", bad.creative_count, 1);

const MISSING = { "1": [{ "2": "CR1" }] };
const missing = parseCreatives(MISSING);
eq("missing advertiser fields become null",
   [missing.advertiser_id, missing.advertiser_name, missing.domain], [null, null, null]);
eq("missing format yields no formats", missing.formats, []);
ok("non-object entries are skipped", parseCreatives({ "1": [null, "x", 7] }).found === false);

// A full page WITH a cursor means there are more creatives than were returned, so
// creative_count is a floor rather than a total.
const full = { "1": Array.from({ length: 40 }, (_, i) => ({ "2": `CR${i}` })), "2": "cursor-token" };
eq("full page plus cursor is truncated", parseCreatives(full).truncated, true);
// A full page with no cursor is exactly complete.
const exact = { "1": Array.from({ length: 40 }, (_, i) => ({ "2": `CR${i}` })) };
eq("full page without cursor is not truncated", parseCreatives(exact).truncated, false);

// --- isRecent ------------------------------------------------------------------

const NOW = Date.parse("2026-09-19T12:00:00Z");
ok("today is recent", isRecent("2026-09-19T00:00:00Z", NOW));
ok("just inside the window is recent", isRecent("2026-08-21T12:00:00Z", NOW));
ok("just outside the window is not", !isRecent("2026-08-19T12:00:00Z", NOW));
// The 128-day case from the sample: a stale advertiser that still has a pixel. This is
// precisely the row that marketing_active gets wrong and this signal gets right.
ok("128 days ago is not recent", !isRecent("2026-05-14T12:00:00Z", NOW));
ok("null last_shown is not recent", !isRecent(null, NOW));
ok("unparseable date is not recent", !isRecent("not a date", NOW));
eq("window is 30 days", RECENT_DAYS, 30);

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
