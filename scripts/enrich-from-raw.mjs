#!/usr/bin/env node
// Phase A enrichment (Prompt 6): derives signals from data already stored in
// raw_scrape. No network calls -- every field below was captured at scrape time and
// has simply never been read.
//
//   booking_present      <- raw_scrape.order_online[].link        (934 rows have it)
//   growth_signal/score  <- raw_scrape.user_reviews[].published_at (3,285 have >=5)
//   description          <- raw_scrape.description                 (49 rows)
//
// What this does NOT write: decision_maker_name. raw_scrape.owner exists on every row
// but holds the Google Business Profile display name ("BARK U (Owner)"), which is the
// business, not a person. Prompt 9 forbids guessing names, so owner data is used only
// as a responsiveness signal.
//
// Usage:
//   node scripts/enrich-from-raw.mjs --dry-run   # report distributions, write nothing
//   node scripts/enrich-from-raw.mjs             # apply
//   node scripts/enrich-from-raw.mjs --limit 100

import { q } from "./lib/q.mjs";

// Booking vendors worth naming in the evidence string. A self-hosted /book-online page
// is still a booking link, so an unrecognized host is not a negative -- it is just less
// specific.
const BOOKING_VENDORS = [
  [/mindbody/i, "Mindbody"], [/vagaro/i, "Vagaro"], [/gingr/i, "Gingr"],
  [/calendly/i, "Calendly"], [/acuity|squarespace.*scheduling/i, "Acuity"],
  [/jotform/i, "Jotform"], [/typeform/i, "Typeform"], [/square\.site|squareup/i, "Square"],
  [/vetstoria/i, "Vetstoria"], [/timify/i, "Timify"], [/setmore/i, "Setmore"],
  [/booksy/i, "Booksy"], [/paw\s?partner|pawpartner/i, "PawPartner"],
  [/precisepetcare/i, "PrecisePetCare"], [/timetopet/i, "TimeToPet"],
];

const DAY = 24 * 60 * 60 * 1000;

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith("--")) {
      const key = argv[i].slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
      out[key] = val;
    }
  }
  return out;
}

function bookingFrom(orderOnline) {
  if (!Array.isArray(orderOnline) || orderOnline.length === 0) return null;
  const links = orderOnline.map((o) => o?.link).filter(Boolean);
  if (links.length === 0) return null;
  const blob = links.join(" ");
  const vendors = BOOKING_VENDORS.filter(([re]) => re.test(blob)).map(([, name]) => name);
  return {
    present: true,
    evidence: vendors.length
      ? `booking via ${[...new Set(vendors)].join(", ")}`
      : `booking link on ${orderOnline[0]?.source || "own site"}`,
  };
}

/**
 * Review velocity as a 0-100 growth score.
 *
 * The scraper returns only the most recent slice of reviews (~10 per business), not the
 * full history, so this measures *recent cadence*, never lifetime totals. Two parts:
 *
 *   recency (0-50): how fresh the newest review is. A business whose last review is two
 *   years old is not taking customers the way one reviewed last week is.
 *   cadence (0-50): reviews per month across the captured window.
 *
 * Returns null when there is too little to judge (<3 dated reviews), rather than
 * inventing a score -- CLAUDE.md says not to fabricate fields.
 */
function growthFrom(userReviews, now = Date.now()) {
  if (!Array.isArray(userReviews)) return null;
  const dates = userReviews
    .map((r) => Date.parse(r?.published_at))
    .filter((t) => Number.isFinite(t) && t <= now)
    .sort((a, b) => b - a);
  if (dates.length < 3) return null;

  const newest = dates[0];
  const oldest = dates[dates.length - 1];
  const daysSinceNewest = (now - newest) / DAY;

  let recency;
  if (daysSinceNewest <= 30) recency = 50;
  else if (daysSinceNewest <= 90) recency = 40;
  else if (daysSinceNewest <= 180) recency = 28;
  else if (daysSinceNewest <= 365) recency = 15;
  else if (daysSinceNewest <= 730) recency = 5;
  else recency = 0;

  // Cadence, with a caveat baked in: the scraper caps user_reviews at 8, and 2,165
  // rows sit exactly at that cap. For a busy business the captured window is therefore
  // truncated -- 8 reviews spanning 4 days means "at least 8 in 4 days", not a
  // measured rate of 35/month. Rather than publish that artifact, a window shorter
  // than 30 days is treated as a floor of 30: the business is clearly active, and the
  // top cadence band is reached either way, but the reported rate stays defensible.
  const spanDays = (newest - oldest) / DAY;
  const truncated = dates.length >= 8 && spanDays < 30;
  const spanMonths = Math.max(spanDays, 30) / 30.44;
  const perMonth = dates.length / spanMonths;

  let cadence;
  if (perMonth >= 4) cadence = 50;
  else if (perMonth >= 2) cadence = 40;
  else if (perMonth >= 1) cadence = 30;
  else if (perMonth >= 0.5) cadence = 20;
  else if (perMonth >= 0.25) cadence = 10;
  else cadence = 4;

  const score = Math.max(0, Math.min(100, Math.round(recency + cadence)));
  const fmt = (t) => new Date(t).toISOString().slice(0, 10);
  const signal =
    `${dates.length} recent reviews ${fmt(oldest)}..${fmt(newest)} ` +
    `(${truncated ? ">=" : "~"}${perMonth.toFixed(1)}/mo, newest ${Math.round(daysSinceNewest)}d ago)` +
    (truncated ? " [capped sample]" : "");

  return { score, signal };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const dryRun = Boolean(args["dry-run"]);
  const limit = args.limit ? parseInt(args.limit, 10) : null;

  const rows = JSON.parse(q(`
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
      'id', id,
      'name', name,
      'description', description,
      'raw_description', raw_scrape->>'description',
      'order_online', CASE WHEN jsonb_typeof(raw_scrape->'order_online') = 'array'
                           THEN raw_scrape->'order_online' ELSE '[]'::jsonb END,
      'user_reviews', CASE WHEN jsonb_typeof(raw_scrape->'user_reviews') = 'array'
                           THEN raw_scrape->'user_reviews' ELSE '[]'::jsonb END,
      'booking_present', booking_present,
      'growth_score', growth_score
    )), '[]'::jsonb)
    FROM (SELECT * FROM leads.businesses ORDER BY id ${limit ? `LIMIT ${limit}` : ""}) b
  `, { args: ["-t", "-A"] }));

  console.error(`[enrich-from-raw] loaded ${rows.length} businesses`);

  const updates = [];
  const stats = { booking: 0, growth: 0, description: 0, no_growth: 0 };
  const scoreBuckets = new Map();

  for (const r of rows) {
    const booking = bookingFrom(r.order_online);
    const growth = growthFrom(r.user_reviews);
    const desc = !r.description && r.raw_description ? r.raw_description : null;

    if (booking) stats.booking++;
    if (growth) {
      stats.growth++;
      // Clamped so a perfect 100 lands in the 80-100 bucket rather than a phantom
      // "100-119" one.
      const lo = Math.min(80, Math.floor(growth.score / 20) * 20);
      const b = `${lo}-${lo === 80 ? 100 : lo + 19}`;
      scoreBuckets.set(b, (scoreBuckets.get(b) || 0) + 1);
    } else {
      stats.no_growth++;
    }
    if (desc) stats.description++;

    if (!booking && !growth && !desc) continue;

    updates.push({
      id: r.id,
      // booking_present is only ever set true here: absence of an order_online entry
      // is absence of evidence, not evidence the business takes no bookings. The
      // website crawler (Work Item 5) is what can observe a true negative.
      booking_present: booking ? true : null,
      marketing_evidence: booking ? booking.evidence : null,
      growth_score: growth ? growth.score : null,
      growth_signal: growth ? growth.signal : null,
      description: desc,
    });
  }

  console.error(
    `[enrich-from-raw] booking_present=${stats.booking}  growth_score=${stats.growth} ` +
    `(${stats.no_growth} with too few dated reviews to judge)  description backfill=${stats.description}`
  );
  console.error(
    "[enrich-from-raw] growth_score distribution: " +
    [...scoreBuckets.entries()].sort((a, b) => a[0].localeCompare(b[0]))
      .map(([k, n]) => `${k}=${n}`).join("  ")
  );
  console.error(`[enrich-from-raw] ${updates.length} rows would change`);

  if (dryRun) {
    console.error("\n[enrich-from-raw] --dry-run: nothing written. Samples:");
    for (const u of updates.filter((u) => u.growth_score != null).slice(0, 6)) {
      const name = rows.find((r) => r.id === u.id)?.name;
      console.error(`  ${String(u.growth_score).padStart(3)}  ${name} -- ${u.growth_signal}`);
    }
    for (const u of updates.filter((u) => u.booking_present).slice(0, 4)) {
      const name = rows.find((r) => r.id === u.id)?.name;
      console.error(`  booking  ${name} -- ${u.marketing_evidence}`);
    }
    return;
  }
  if (updates.length === 0) return;

  // COALESCE on every column so a re-run never blanks a field this pass could not
  // derive, and so values written by later phases (the website crawler sets
  // booking_present too) survive.
  q(`
    BEGIN;
    CREATE TEMP TABLE enrich_payload (data jsonb);
    INSERT INTO enrich_payload VALUES ($e$${JSON.stringify(updates)}$e$::jsonb);
    UPDATE leads.businesses b
    SET booking_present    = COALESCE((u->>'booking_present')::boolean, b.booking_present),
        marketing_evidence = COALESCE(u->>'marketing_evidence', b.marketing_evidence),
        growth_score       = COALESCE((u->>'growth_score')::int, b.growth_score),
        growth_signal      = COALESCE(u->>'growth_signal', b.growth_signal),
        description        = COALESCE(b.description, u->>'description'),
        date_updated       = now()
    FROM enrich_payload p, LATERAL jsonb_array_elements(p.data) u
    WHERE b.id = (u->>'id')::uuid;
    COMMIT;
  `);

  console.error(`[enrich-from-raw] applied ${updates.length} updates`);
}

main();
