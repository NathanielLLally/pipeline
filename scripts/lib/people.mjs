// Decision-maker extraction (Prompt 9). Pure functions over the page text already
// stored in leads.website_crawl, so this costs no network and is unit testable.
//
// Prompt 9's binding constraint is "do not guess names." Everything here is therefore
// built to *refuse*: each candidate must be produced by an explicit pattern, must carry
// a verbatim quote from the source text, and must survive a name-shape check. When the
// patterns disagree or the shape is doubtful, the correct output is no row at all. An
// empty decision_maker_name is a normal result, not a failure.
//
// The niche's defining hazard: in dog-training copy, "owner" nearly always means the
// DOG's owner. Measured across the crawled corpus, 1,355 of 1,994 pages containing
// "owner" (68%) use it in phrases like "dog owner", "pet owners", "their owner". A bare
// proximity match between a capitalized word and "owner" would be wrong far more often
// than right, so ownership sense is disambiguated explicitly below.

/** Titles Prompt 9 accepts, longest first so "co-founder" wins over "founder". */
const TITLES = [
  ["co-founder", "Co-founder"], ["cofounder", "Co-founder"], ["co founder", "Co-founder"],
  ["general manager", "General Manager"],
  ["founder", "Founder"], ["owner", "Owner"], ["ceo", "CEO"], ["president", "President"],
];

// "owner" in the business sense vs the dog-owner sense. The left context decides:
// "dog owner", "pet owners", "every owner" are the customer; "owner of X", "owner and
// head trainer", "Name (Owner)" are the principal.
const DOG_OWNER_LEFT = /\b(?:dog|dogs|pet|pets|puppy|puppies|canine|animal|their|your|our|each|every|any|the|a|new|responsible|first-time|fellow)\s+$/i;

// Words that look like names to a capitalization test but are not people. Kept tight:
// an over-broad stoplist silently drops real surnames.
const NOT_A_NAME = new Set([
  "dog", "dogs", "puppy", "puppies", "training", "trainer", "trainers", "academy",
  "kennel", "kennels", "canine", "canines", "board", "train", "behavior", "behaviour",
  "obedience", "school", "services", "service", "company", "llc", "inc", "team",
  "staff", "home", "about", "contact", "pricing", "menu", "read", "more", "learn",
  "our", "the", "we", "us", "why", "how", "what", "who", "when", "where", "meet",
  "welcome", "hello", "click", "call", "book", "schedule", "today", "now", "new",
  "best", "top", "premier", "professional", "certified", "private", "puppy", "adult",
  "class", "classes", "program", "programs", "package", "consultation", "free",
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
  "january", "february", "march", "april", "may", "june", "july", "august",
  "september", "october", "november", "december",
  "north", "south", "east", "west", "city", "county", "street", "avenue", "road",
  "facebook", "instagram", "google", "yelp", "youtube", "tiktok", "linkedin",
  "copyright", "rights", "reserved", "privacy", "policy", "terms", "site", "website",
]);

// Honorifics that legitimately precede a name and should be kept out of the name itself.
const HONORIFIC = /^(?:dr|mr|mrs|ms|miss|prof|professor)\.?$/i;

// Credential suffixes that follow trainer names constantly (KPA-CTP, CPDT-KA, CCPDT).
// They must not be absorbed into the surname.
const CREDENTIAL = /^(?:[A-Z]{2,6}(?:-[A-Z]{1,4})+|CPDT|KPA|CCPDT|CBCC|IAABC|ABCDT|CDBC|PhD|DVM|MA|MS|BS|VSPDT)$/;

const NAME_WORD = "[A-Z][a-zA-Z'’\\-]{1,20}";
// One to three capitalized words: "Ian Dunbar", "Kelly Gorman Dunbar", "Stephanie
// Zablah-Kruger". Single words are allowed by the regex but filtered later -- a lone
// first name is only accepted from patterns that make the reference unambiguous.
const NAME_RE = new RegExp(`${NAME_WORD}(?:\\s+${NAME_WORD}){0,2}`);

/**
 * Validates a captured string as a plausible human name.
 *
 * Returns the cleaned name or null. Rejection is cheap and silent by design: this runs
 * on every candidate and Prompt 9 prefers nothing over a guess.
 */
export function cleanName(raw) {
  if (!raw) return null;
  let words = raw
    .replace(/[‘’]/g, "'")
    .split(/\s+/)
    .map((w) => w.replace(/^[^A-Za-z]+|[^A-Za-z'\-]+$/g, ""))
    .filter(Boolean);

  // Drop a leading honorific ("Dr. Ian Dunbar" -> "Ian Dunbar") and trailing credentials.
  while (words.length && HONORIFIC.test(words[0])) words.shift();
  while (words.length && CREDENTIAL.test(words[words.length - 1])) words.pop();

  if (words.length === 0 || words.length > 3) return null;
  for (const w of words) {
    if (!/^[A-Z][a-zA-Z'’\-]*$/.test(w)) return null;
    if (w.length < 2) return null;                      // stray initials like "J"
    if (NOT_A_NAME.has(w.toLowerCase())) return null;
    if (CREDENTIAL.test(w)) return null;
  }
  // ALLCAPS runs are headings ("BOARD AND TRAIN"), not names.
  if (words.every((w) => w === w.toUpperCase() && w.length > 1)) return null;
  return words.join(" ");
}

/** True when "owner" at this offset refers to the business, not to a dog's owner. */
function isBusinessOwnerSense(text, idx) {
  const left = text.slice(Math.max(0, idx - 40), idx);
  if (DOG_OWNER_LEFT.test(left)) return false;
  // "owners" plural is almost always the customer base in this niche.
  if (/^owners\b/i.test(text.slice(idx))) return false;
  return true;
}

function titleAt(text, idx, matched) {
  const canonical = TITLES.find(([t]) => t === matched.toLowerCase());
  if (!canonical) return null;
  if (canonical[1] === "Owner" && !isBusinessOwnerSense(text, idx)) return null;
  return canonical[1];
}

/** A quote of the surrounding sentence, for the audit trail Prompt 9 requires. */
function quoteAround(text, idx, len) {
  const start = Math.max(0, idx - 90);
  const end = Math.min(text.length, idx + len + 90);
  return text.slice(start, end).replace(/\s+/g, " ").trim();
}

const TITLE_ALT = TITLES.map(([t]) => t.replace(/[-\s]/g, "[-\\s]?")).join("|");

/**
 * The extraction patterns, ordered by how much they constrain the relationship between
 * the name and the title. Confidence follows directly from which pattern fired, since
 * that is the only honest basis for it.
 */
const PATTERNS = [
  {
    // "Stephanie Zablah-Kruger (Owner)" -- name and title bound by punctuation.
    key: "name_paren_title",
    confidence: "high",
    re: new RegExp(`(${NAME_RE.source})\\s*[\\(\\[]\\s*(${TITLE_ALT})\\s*[\\)\\]]`, "gi"),
    name: 1, title: 2,
  },
  {
    // "Michael Sandman, Owner" / "Ian Dunbar - Founder & Head Trainer"
    key: "name_comma_title",
    confidence: "high",
    re: new RegExp(`(${NAME_RE.source})\\s*(?:,|\\u2013|\\u2014|-|\\|)\\s*(?:the\\s+)?(${TITLE_ALT})\\b`, "gi"),
    name: 1, title: 2,
  },
  {
    // "Owner: Jane Smith" / "Founder & CEO Kelly Gorman Dunbar"
    key: "title_then_name",
    confidence: "high",
    re: new RegExp(`\\b(${TITLE_ALT})\\b(?:\\s*(?:and|&|\\/)\\s*[A-Za-z ]{0,20})?\\s*[:\\-\\u2013]?\\s+((?:Dr\\.?\\s+)?${NAME_RE.source})`, "gi"),
    name: 2, title: 1,
  },
  {
    // "Jane Smith is the owner of" / "Michael founded" -- verb-linked, still explicit.
    key: "name_is_title",
    confidence: "medium",
    re: new RegExp(`(${NAME_RE.source})\\s+(?:is|was)\\s+(?:the|a|our)\\s+(?:[a-z\\-]+\\s+){0,2}(${TITLE_ALT})\\b`, "gi"),
    name: 1, title: 2,
  },
  {
    // "My name is Donald Hutcherson" on an about page. The title is inferred from the
    // page being a first-person about page, so it is deliberately the weakest tier and
    // the caller must supply the title separately.
    key: "self_intro",
    confidence: "low",
    re: new RegExp(`\\bMy name is\\s+((?:Dr\\.?\\s+)?${NAME_RE.source})`, "gi"),
    name: 1, title: null,
  },
];

/**
 * Finds decision-maker candidates in one page's text.
 *
 * `pageKind` matters: a "self_intro" on an about page is the site owner introducing
 * themselves, which is meaningful; the same phrase in a testimonial is not. Instructor
 * and team pages are handled by the caller, which down-ranks them -- the Sirius Dog
 * Training instructors page lists a dozen staff, none of whom is the decision maker.
 */
export function extractCandidates(text, { pageKind = null, pageUrl = null } = {}) {
  if (!text || text.length < 40) return [];
  const out = [];

  for (const pat of PATTERNS) {
    pat.re.lastIndex = 0;
    let m;
    while ((m = pat.re.exec(text)) !== null) {
      const rawName = m[pat.name];
      const name = cleanName(rawName);
      if (!name) continue;

      let title = null;
      if (pat.title !== null) {
        const rawTitle = m[pat.title];
        const titleIdx = m.index + m[0].indexOf(rawTitle);
        title = titleAt(text, titleIdx, rawTitle);
        if (!title) continue;               // dog-owner sense, or not an accepted title
      } else {
        // self_intro carries no title of its own. Only an about page justifies
        // assuming the speaker is the principal, and even then at low confidence.
        if (pageKind !== "about") continue;
        title = "Owner";
      }

      // A single-word name is only trustworthy when the pattern bound it tightly.
      if (!name.includes(" ") && pat.confidence !== "high") continue;

      out.push({
        name,
        title,
        confidence: pat.confidence,
        pattern: pat.key,
        evidence_quote: quoteAround(text, m.index, m[0].length),
        source_url: pageUrl,
        page_kind: pageKind,
      });
    }
  }
  return out;
}

// Pages whose named people are staff rather than principals. A name found only here is
// not promoted; it needs corroboration from an about/home/contact page.
const STAFF_PAGE = /\b(?:instructor|trainers?|our-?team|staff|employees)\b/i;

const TITLE_RANK = { Founder: 6, "Co-founder": 5, Owner: 4, CEO: 4, President: 3, "General Manager": 2 };
const CONF_RANK = { high: 3, medium: 2, low: 1 };

/**
 * Picks one decision maker for a business from candidates across all its pages.
 *
 * Corroboration is what raises confidence: the same name found by two independent
 * patterns, or a name whose parts match a person-shaped email address on the site, is
 * materially more trustworthy than a single regex hit. This mirrors the reasoning in
 * docs/ARCHITECTURE.md about not treating one weak signal as proof.
 */
export function pickDecisionMaker(candidates, { personEmails = [] } = {}) {
  if (!candidates.length) return null;

  const byName = new Map();
  for (const c of candidates) {
    const key = c.name.toLowerCase();
    if (!byName.has(key)) byName.set(key, []);
    byName.get(key).push(c);
  }

  const emailLocals = personEmails.map((e) => e.split("@")[0].toLowerCase());
  const scored = [];

  for (const [key, group] of byName) {
    const best = group.slice().sort(
      (a, b) => (CONF_RANK[b.confidence] - CONF_RANK[a.confidence]) ||
                (TITLE_RANK[b.title] || 0) - (TITLE_RANK[a.title] || 0),
    )[0];

    const parts = key.split(" ");
    const emailMatch = emailLocals.some((l) => {
      const stripped = l.replace(/[._-]/g, "");
      return parts.every((p) => stripped.includes(p)) ||
             (parts.length > 1 && stripped.includes(parts[0][0] + parts[parts.length - 1]));
    });

    const patterns = new Set(group.map((c) => c.pattern));
    const kinds = new Set(group.map((c) => c.page_kind));
    const onlyStaffPage = [...group].every(
      (c) => STAFF_PAGE.test(c.source_url || "") && c.page_kind !== "about",
    );

    let score = CONF_RANK[best.confidence] * 10 + (TITLE_RANK[best.title] || 0);
    if (patterns.size > 1) score += 8;          // two independent patterns agree
    if (emailMatch) score += 10;                // the site's own email corroborates
    if (kinds.has("about")) score += 3;
    if (onlyStaffPage) score -= 12;

    // Confidence is promoted only on real corroboration, never on repetition of the
    // same pattern on the same page.
    let confidence = best.confidence;
    if (emailMatch && confidence !== "high") confidence = "high";
    else if (patterns.size > 1 && confidence === "low") confidence = "medium";
    if (onlyStaffPage) confidence = "low";

    scored.push({ ...best, confidence, score, corroboration: {
      patterns: [...patterns], email_match: emailMatch, mentions: group.length,
    } });
  }

  scored.sort((a, b) => b.score - a.score);

  // An ambiguous top pair -- two different names tied on evidence -- is exactly the
  // situation Prompt 9's "do not guess" covers. Emit nothing rather than pick one.
  if (scored.length > 1 && scored[0].score === scored[1].score) return null;
  return scored[0];
}
