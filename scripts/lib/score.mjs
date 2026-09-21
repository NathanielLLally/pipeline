// ICP scoring, shared by transform-and-score.mjs (ingest) and rescore.mjs (backfill)
// so there is exactly one definition of what a good prospect is.
//
// The rule that matters: score the BUSINESS, never the query that found it. The
// previous version put the search keyword first in its text blob, so every result of
// a "board and train" search scored base 60 whether it was a behaviorist or a subway
// station -- which is how 55% of the database became "Tier 1", veterinarians, dog
// parks and a hot dog restaurant included.

// Google's category taxonomy for the ICP. An allowlist, not a denylist: the junk tail
// ("Train station", "Brewpub", "Museum" -- all real rows pulled in by "board and
// train" / "dog training" queries) is unbounded, while genuine dog-service categories
// are a short closed set.
const CATEGORY_MAP = new Map([
  ["dog trainer", "dog_training"],
  ["pet trainer", "dog_training"],
  ["dog day care center", "daycare_boarding"],
  ["pet boarding service", "daycare_boarding"],
  ["kennel", "daycare_boarding"],
  ["cat boarding service", "daycare_boarding"],
  ["pet groomer", "grooming"],
  ["pet grooming station", "grooming"],
  ["dog walker", "dog_walking_petsitting"],
  ["pet sitter", "dog_walking_petsitting"],
  ["pet care service", "dog_walking_petsitting"],
]);

// Categories that disqualify a record when they are what the business primarily IS.
// A veterinary practice with a boarding sideline is not a buyer of training leads and
// a shelter is not a commercial prospect at all -- but a busy dog daycare that also
// sells leashes is, so these only disqualify in the primary slot. Matching them
// anywhere would reject prospects like Dogtopia, whose secondary categories include
// "Pet supply store".
const DISQUALIFYING = new Set([
  "veterinarian", "animal hospital", "veterinary care", "emergency veterinarian service",
  "animal shelter", "animal control service", "animal protection organization",
  "pet adoption service", "dog breeder", "non-profit organization",
  "dog park", "park", "state park", "public beach",
  "pet supply store", "pet store",
]);

// Gate for the secondary-category fallback: the business must read as a pet/dog
// business in its own right before a sideline category can classify it.
const PET_PRIMARY_RE = /\b(dog|pet|puppy|canine|k-?9)\b/i;

const BASE_BY_CATEGORY = {
  dog_training: 55,
  daycare_boarding: 45,
  grooming: 35,
  dog_walking_petsitting: 25,
};

// High-ticket / recurring service signals, weighted by commercial value per CLAUDE.md's
// priority list. Matched against the business's own text, never the search keyword.
const PREMIUM_SIGNALS = [
  [/board.{0,5}(and|&|n).{0,5}train|\bb&t\b/i, 12, "board-and-train"],
  [/aggress|reactiv|behavio(u)?r modification|behavio(u)?rist/i, 10, "behavior/aggression specialist"],
  [/private (dog )?(train|lesson)|one.on.one|in.home train/i, 8, "private/in-home training"],
  [/puppy (program|class|training|kindergarten)/i, 6, "puppy program"],
  // "K9" is deliberately absent here. It is branding, not a specialty: a large slice
  // of the database is named "<something> K9" and means nothing by it but "dog".
  // Counting it promoted generic trainers ("KJ K9", "Beachwood K9") to Tier 1 on
  // their name alone -- the keyword bug wearing a different hat.
  [/service dog|therapy dog|working dog|protection (dog|train)/i, 8, "specialty/working dog"],
  [/mobile (groom|pet)/i, 8, "mobile grooming"],
  [/luxury|premium|resort|suite|boutique/i, 5, "premium positioning"],
  [/daycare|day care|overnight|boarding/i, 4, "recurring daycare/boarding"],
];

const lower = (s) => (s || "").toString().toLowerCase();

/** Derives the service_category from the business's own Google categories.
 *  Returns null when nothing in the taxonomy matches -- the record is off-ICP. */
export function deriveServiceCategory(primaryCategory, categories) {
  const all = [primaryCategory, ...(categories || [])].map(lower).filter(Boolean);
  if (DISQUALIFYING.has(lower(primaryCategory))) return null;

  // Primary category wins; it is what Google considers the business to actually be.
  const primary = CATEGORY_MAP.get(lower(primaryCategory));
  if (primary) return primary;

  // No primary match. Fall back to secondary categories only when the business is
  // itself a pet business by its primary category -- otherwise a resort hotel that
  // boards dogs, or a park with a dog run, would qualify on a sideline. Categories
  // like "Training center" or "Pet care service" are the real reason this fallback
  // exists.
  if (!PET_PRIMARY_RE.test(lower(primaryCategory))) return null;

  // Highest-priority secondary wins, in the ICP priority order from CLAUDE.md
  // (trainers > daycare/boarding > groomers > walkers).
  for (const want of ["dog_training", "daycare_boarding", "grooming", "dog_walking_petsitting"]) {
    if (all.some((c) => CATEGORY_MAP.get(c) === want)) return want;
  }
  return null;
}

/**
 * Scores one business against the ICP.
 *
 * `keyword` is accepted but deliberately contributes at most 5 points, and only as a
 * tiebreaker when the business's own text already corroborates it. Enrichment fields
 * (bookingPresent, growthScore, ads*) are optional and default to null -- passing them
 * lets later phases raise a score without a second scoring implementation. Ingest does
 * not have them, since a newly discovered business has not been enriched yet; they
 * arrive when rescore.mjs runs after an enrichment pass.
 */
export function scoreIcp({
  name, keyword, primaryCategory, categories, description, about,
  rating, reviewCount, website, priceRange,
  bookingPresent = null, growthScore = null,
  adsConfirmedActive = null, adsCreativeCount = null,
}) {
  const serviceCategory = deriveServiceCategory(primaryCategory, categories);

  if (!serviceCategory) {
    return { score: 0, tier: "Tier 4", qcStatus: "REJECTED", serviceCategory: null,
             signals: ["off-ICP category"] };
  }

  // The business's own words -- its name, categories, description and Google "about"
  // attributes. The name carries real signal here ("Dog Dynamix | Dog Board and Train",
  // "Off Leash K9 Training") and matters more than usual because only 50 of 3,686
  // records have a description at all. The search keyword is pointedly absent.
  const blob = [
    name,
    primaryCategory,
    ...(categories || []),
    description,
    ...(about || []),
  //
  // Joined with newlines, not spaces, so no signal phrase can straddle two fields.
  // Every PREMIUM_SIGNALS pattern uses `.` for its gaps and `.` does not match "\n",
  // so a boundary is a hard stop. With a space, "Pet boarding service" followed by
  // "Dog day care center" produced the substring "service dog" and filed an ordinary
  // boarding kennel as a service-dog specialist.
  ].filter(Boolean).join("\n").toLowerCase();

  let score = BASE_BY_CATEGORY[serviceCategory];
  const signals = [];

  for (const [re, points, label] of PREMIUM_SIGNALS) {
    if (re.test(blob)) {
      score += points;
      signals.push(label);
    }
  }

  // Keyword as weak corroboration only: it can add at most 5, and only when the
  // business's own text already says the same thing.
  if (keyword && PREMIUM_SIGNALS.some(([re]) => re.test(keyword) && re.test(blob))) {
    score += 5;
  }

  const r = rating != null ? Number(rating) : null;
  const rc = reviewCount != null ? Number(reviewCount) : null;

  // Established local presence, calibrated against this database's actual review
  // distribution (p50=54, p75=124, p90=272) rather than round numbers. Thresholds set
  // at "50 reviews" handed the top bonus to the median business, which is how half the
  // database ended up Tier 1. Rating alone is noise without volume behind it.
  if (r != null && rc != null && r >= 4.6 && rc >= 270) score += 20;      // ~p90
  else if (r != null && rc != null && r >= 4.6 && rc >= 125) score += 14; // ~p75
  else if (r != null && rc != null && r >= 4.5 && rc >= 55) score += 8;   // ~p50
  else if (r != null && rc != null && r >= 4.0 && rc >= 20) score += 4;

  // A website is table stakes (93% of the database has one), so it is worth little on
  // its own; the signals that cost a business real money are worth more.
  if (website) score += 4;
  if (priceRange) score += 5;
  if (bookingPresent) score += 8;
  if (growthScore != null && growthScore >= 60) score += 8;

  // Confirmed ad spend, from leads.ads_transparency. This is the strongest single
  // commercial signal available, and it is weighted above booking or growth for a
  // specific reason: a business currently paying Google to acquire customers has
  // already decided that buying customers is worth money, which is exactly the
  // decision a lead buyer has to have made. The others are evidence of a business
  // being well-run; this is evidence of budget.
  //
  // It is deliberately NOT scored off `marketing_active`, which only proves a pixel is
  // installed. Measured across 3,347 checked domains, the two disagree in both
  // directions: 302 confirmed advertisers had no pixel at all, and 35 carried a pixel
  // while running no ads. A pixel outlives the campaign that installed it.
  if (adsConfirmedActive) {
    score += 12;
    signals.push("confirmed ad spend (last 30d)");
    // Scale of spend, as a floor: 40 is the API's page size, so a business at the cap
    // is running at least that many creatives. Treated as a modest bonus rather than a
    // linear term, since creative count measures production volume, not dollars.
    if (adsCreativeCount != null && adsCreativeCount >= 10) {
      score += 4;
      signals.push("multiple concurrent creatives");
    }
  } else if (adsCreativeCount != null && adsCreativeCount > 0) {
    // Advertised at some point but nothing live in the last 30 days. Worth something --
    // the business has bought ads before and knows what they cost -- but not the full
    // bonus, because the campaign is off and may have been turned off for budget.
    score += 4;
    signals.push("lapsed ad spend");
  }

  // Thin listing: no reviews and no website means nothing to sell to and nothing to
  // verify against.
  if ((rc == null || rc < 3) && !website) score -= 20;

  score = Math.max(0, Math.min(100, score));

  // Tier cutoffs, recalibrated once enrichment began contributing (2026-09-19).
  //
  // The original 70/50/30 was set when the scorer saw only the Google Maps record. With
  // booking, growth and confirmed ad spend in play there are up to 32 further points
  // available, and at 70 the top tier drifted to 36% of the database -- 1,417 rows,
  // which is not a priority list. These are set from the actual score distribution so
  // Tier 1 holds ~15%: the point of the enrichment signals is to decide WHO is in the
  // top tier, not to grow it.
  //
  // Cumulative share measured over 3,888 scored rows: >=83 is 15.0%, >=70 is 36.4%,
  // >=50 is 78.1%. Re-measure before moving these again; a cutoff chosen as a round
  // number rather than from the distribution is what inflated Tier 1 the first time.
  const tier = score >= 83 ? "Tier 1" : score >= 70 ? "Tier 2" : score >= 50 ? "Tier 3" : "Tier 4";

  let qcStatus;
  if (!website && (rc == null || rc === 0)) qcStatus = "NEEDS_ENRICHMENT";
  else if (score < 30) qcStatus = "LOW_PRIORITY";
  else qcStatus = "VALID";

  return { score, tier, qcStatus, serviceCategory, signals };
}

/** Flattens raw_scrape.about[] into the option names used as scoring text. */
export function aboutOptionNames(about) {
  if (!Array.isArray(about)) return [];
  const names = [];
  for (const section of about) {
    for (const opt of section?.options || []) {
      if (opt?.name && opt?.enabled !== false) names.push(opt.name);
    }
  }
  return names;
}
