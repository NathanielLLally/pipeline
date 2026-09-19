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
  // Added after a dry run over the real corpus produced these as "names". Each was a
  // capitalized word sitting where a name was expected -- mostly nouns from marketing
  // copy ("Founder & CEO Operations Manager", "Being Co", "Franchise Owner") and job
  // words that precede a real name ("Officer David Shade").
  "co", "founder", "cofounder", "owner", "ceo", "president", "director", "manager",
  "officer", "operations", "franchise", "franchisee", "being", "involvement",
  "moderate", "high", "low", "experience", "mission", "vision", "values", "story",
  "history", "philosophy", "approach", "method", "methods", "results", "success",
  "client", "clients", "customer", "customers", "family", "families", "friend",
  "friends", "puppies", "obedience", "agility", "therapy", "rescue", "shelter",
  "veteran", "veterans", "military", "police", "certified", "certification",
  "graduate", "member", "association", "council", "institute", "academy", "university",
  "college", "degree", "years", "year", "hours", "session", "sessions", "lesson",
  "lessons", "daycare", "boarding", "grooming", "walking", "sitting", "sitter",
  // Page-furniture words that lead a greedy capture: "Meet the Team Zephyr Dippel",
  // "Zephyr Dippel's Bio Nia Stevens Owner".
  // Marketing-copy nouns that pair up into fake two-word names: "Fresh Patch Pet
  // Tested, Owner Approved", "Training ... that Inspires Trust Owner", "Hound Haven
  // owner Peggy McCarty", "Frank Pugliese Master Trainer, Owner".
  "master", "haven", "hound", "patch", "approved", "inspires", "trust", "fresh",
  "bio", "biography", "profile", "team", "teams", "shop", "members", "message",
  "send", "view", "quick", "plans", "ongoing", "first", "last", "background",
  "check", "verified", "tested", "citizen", "evaluator", "senior", "vice", "asst",
  "assistant", "deputy", "clinic", "veterinary", "camps", "camp", "kids", "pup",
  // Business-name and marketing nouns that survived into captures in the corpus audit:
  // "Sport Club", "Solutions Marcus", "Professionals Paula Weir", "States Mondioring".
  "sport", "sports", "club", "clubs", "solution", "solutions", "professional",
  "professionals", "specialist", "specialists", "expert", "experts", "consultant",
  "consultants", "partner", "partners", "group", "center", "centre", "companion",
  "companions", "harmony", "states", "united", "america", "american", "national",
  "international", "society", "federation", "league", "network", "alliance",
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

  // Trim trailing non-name words rather than rejecting the whole capture. A greedy
  // pattern routinely runs one word past the name into the title or the next clause:
  // "Jamie Miller Co-Founder", "Tucker Co-Owner", "Andrew Fraser Driven". Dropping the
  // whole match would lose a real name; trimming keeps "Jamie Miller". Leading junk is
  // trimmed too, for "Officer David Shade" and "Jen JENNIFER HIGGINS".
  //
  // Trimming must leave at least two words standing. Otherwise it manufactures names
  // out of phrases: "Puppy Kindergarten" would trim to "Kindergarten", which passes
  // every later check while being nothing of the sort. Reducing a capture to a single
  // word means the capture was not a name, so the whole thing is rejected instead.
  const junk = (w) =>
    NOT_A_NAME.has(w.toLowerCase().replace(/^co-?/, "")) ||
    NOT_A_NAME.has(w.toLowerCase()) ||
    (w === w.toUpperCase() && w.length > 1);
  while (words.length > 2 && junk(words[words.length - 1])) words.pop();
  while (words.length > 2 && junk(words[0])) words.shift();
  // Any junk word surviving in a two-word capture disqualifies it outright.
  if (words.some(junk)) return null;

  if (words.length === 0 || words.length > 3) return null;
  for (const w of words) {
    // A trailing hyphen or apostrophe is stripped markup, not part of the name: the
    // corpus produced "Greg Winters-" from "Greg Winters- PDT Owner/Dog Behaviorist".
    if (!/^[A-Z][a-zA-Z'’\-]*[a-zA-Z]$/.test(w)) return null;
    if (w.length < 2) return null;                      // stray initials like "J"
    if (NOT_A_NAME.has(w.toLowerCase())) return null;
    if (CREDENTIAL.test(w)) return null;
    // Contractions and possessives are prose, not names: the corpus produced "I've"
    // and "Officer David Shade's" as candidates. A possessive also means the name is
    // being used attributively ("Shade's method"), which is not a title attribution.
    if (/'(?:s|ve|ll|re|d|t|m)$/i.test(w)) return null;
  }
  // The same word twice is a rendering artifact, not a name: the corpus produced
  // "Maria Maria", "Barak Barak" and "Stefanie For Stefanie" from repeated markup.
  const lowered = words.map((w) => w.toLowerCase());
  if (new Set(lowered).size !== lowered.length) return null;
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
  // "Vice President" is not the decision maker, and TITLE_ALT matches its "President"
  // tail -- which both records the wrong role and eats the name ("Jake Satterlee Vice"
  // as the name, "President" as the title). "Karen Vinton ... Senior Vice President
  // role" is worse still: a past corporate job in a bio, not this business at all.
  if (canonical[1] === "President" && /\b(?:vice|asst\.?|assistant|deputy|senior|sr\.?)[-\s]*$/i.test(text.slice(Math.max(0, idx - 12), idx))) {
    return null;
  }
  if (canonical[1] === "Owner" && !isBusinessOwnerSense(text, idx)) return null;
  // "Co-Owner" is a distinct, more junior claim than "Owner", and the TITLE_ALT
  // alternation would otherwise match the "Owner" tail and record it as sole ownership.
  // A staff block listing "Valerie Fry Owner / CEO" beside "Keisha Tucker Co-Owner /
  // CFO" must not promote the CFO to Owner.
  if (canonical[1] === "Owner" && /co-?\s*$/i.test(text.slice(Math.max(0, idx - 4), idx))) {
    return "Co-owner";
  }
  return canonical[1];
}

/** A quote of the surrounding sentence, for the audit trail Prompt 9 requires. */
function quoteAround(text, idx, len) {
  const start = Math.max(0, idx - 90);
  const end = Math.min(text.length, idx + len + 90);
  return text.slice(start, end).replace(/\s+/g, " ").trim();
}

/**
 * Case-insensitive form of a literal, as explicit character classes.
 *
 * These patterns must NOT carry the /i flag. Capitalization is the only thing marking a
 * name in running prose, and under /i the `[A-Z]` in NAME_WORD matches lowercase too --
 * so "My name is Donald Hutcherson and I" captured "Donald Hutcherson and", and an email
 * address ahead of a name contributed its "com". Every such capture was then thrown out
 * by cleanName, making the patterns look dead when they were merely greedy. Titles still
 * need to match any casing ("Owner", "OWNER", "owner"), hence this.
 */
const ci = (s) => s.replace(/[a-z]/gi, (c) => `[${c.toUpperCase()}${c.toLowerCase()}]`);

const TITLE_ALT = TITLES.map(([t]) => ci(t).replace(/[-\s]/g, "[-\\s]?")).join("|");

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
    re: new RegExp(`(${NAME_RE.source})\\s*[\\(\\[]\\s*(${TITLE_ALT})\\s*[\\)\\]]`, "g"),
    name: 1, title: 2,
  },
  {
    // "Michael Sandman, Owner" / "Ian Dunbar - Founder & Head Trainer"
    //
    // The separator may also be a slash or nothing at all: staff blocks render as
    // "Valerie Fry Owner / CEO" once the markup is stripped. Without the bare-space
    // form, such a block yields only the junior colleague who happens to be written
    // "Keisha Tucker Co-Owner / CFO" -- the senior person is silently skipped, which
    // is worse than finding nobody. The separator is captured (group 2) because the
    // bare-space form needs a stricter check downstream: see requireAdjacent below.
    key: "name_comma_title",
    confidence: "high",
    re: new RegExp(`(${NAME_RE.source})(\\s*(?:,|\\u2013|\\u2014|-|\\||\\/)\\s*|\\s+)(?:the\\s+)?(${TITLE_ALT})\\b`, "g"),
    name: 1, title: 3, sep: 2, requireAdjacent: true,
  },
  {
    // "Owner: Jane Smith" / "Founder & CEO Kelly Gorman Dunbar"
    //
    // Medium, not high, despite being an explicit pattern: it is the loosest of the
    // three title-bound forms, because whatever capitalized words happen to follow a
    // title get captured. In the corpus dry run it supplied most of the false
    // positives ("Founder ... Operations", "Franchise Owner"). It reaches high only
    // through corroboration in pickDecisionMaker -- a second pattern or a matching
    // person-shaped email.
    key: "title_then_name",
    confidence: "medium",
    re: new RegExp(`\\b(${TITLE_ALT})\\b(?:\\s*(?:and|&|\\/)\\s*[A-Za-z ]{0,20})?\\s*[:\\-\\u2013]?\\s+((?:Dr\\.?\\s+)?${NAME_RE.source})`, "g"),
    name: 2, title: 1,
  },
  {
    // "Jane Smith is the owner of" / "Michael founded" -- verb-linked, still explicit.
    key: "name_is_title",
    confidence: "medium",
    re: new RegExp(`(${NAME_RE.source})\\s+(?:is|was)\\s+(?:the|a|our)\\s+(?:[a-z\\-]+\\s+){0,2}(${TITLE_ALT})\\b`, "g"),
    name: 1, title: 2,
  },
  {
    // "My name is Donald Hutcherson" on an about page. The title is inferred from the
    // page being a first-person about page, so it is deliberately the weakest tier and
    // the caller must supply the title separately.
    key: "self_intro",
    confidence: "low",
    re: new RegExp(`\\bMy name is\\s+((?:Dr\\.?\\s+)?${NAME_RE.source})`, "g"),
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

      // A name immediately preceded by a preposition or article is part of a
      // surrounding phrase, not an attribution: "At Pet Harmony, we..." and "the United
      // States Mondioring... President" both produced business words as names. A real
      // attribution reads "..., Owner" or "Founder Jane Smith", never "at <Name>,".
      //
      // This is anchored to the CLEANED name rather than the raw capture. The greedy
      // pattern routinely swallows a junk word ahead of the real name -- "Meet the Team
      // Zephyr Dippel Owner" captures "Team Zephyr Dippel" -- and testing the raw
      // capture put the article in front of "Team", discarding a perfectly good name.
      // The article qualifies the junk word, not the person.
      const rawStart = m.index + m[0].indexOf(rawName);
      const kept = rawName.indexOf(name.split(" ")[0]);
      const nameStart = rawStart + (kept >= 0 ? kept : 0);
      const before = text.slice(Math.max(0, nameStart - 16), nameStart);
      if (/\b(?:at|of|with|for|from|to|in|on|by|the|a|an|our|your|their)\s+$/i.test(before)) continue;

      // Adjacency guard for the bare-space separator.
      //
      // cleanName TRIMS junk words rather than rejecting, which is right when the greed
      // runs into a title ("Jamie Miller Co-Founder" -> "Jamie Miller"). But with a bare
      // space as the separator, trimming can manufacture an attribution that the source
      // never made: "As Featured In Client Steph Curry Meet the Founder" captured
      // "Steph Curry Meet", trimmed "Meet", and recorded a client testimonial as the
      // founder. Likewise "Zephyr Dippel's Bio Nia Stevens Owner" credited the wrong
      // person entirely. Punctuation ("Jane Doe, Owner") binds the two explicitly and
      // is trusted; a bare space only binds them if nothing was trimmed off the end --
      // that is, the name really does sit immediately against the title.
      if (pat.requireAdjacent && pat.sep && !/[,\u2013\u2014\-|\/]/.test(m[pat.sep])) {
        const tail = name.split(" ").pop();
        if (!new RegExp(`${tail.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`).test(rawName.trim())) continue;
        // A bare space also has to carry the whole burden of proving these three words
        // are one person's name. Over the 204-row audit, every three-word bare-space
        // capture was wrong -- "Shop Jake Wright", "Members Tara Stermer", "Town
        // Margaret Fraser", "Tong Heather Mozingo" -- because the preceding word is
        // simply the end of the previous sentence. Punctuation earns three words;
        // a space does not.
        if (name.split(" ").length > 2) continue;
      }

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

      // A single-word name is never a usable decision-maker record: "Lisa, Owner" is
      // real but not actionable for outreach, and a lone capitalized word is the shape
      // most of this pattern's false positives take. Prompt 9 wants a name that can be
      // verified and addressed, so first-name-only hits are dropped entirely rather
      // than stored at reduced confidence.
      if (!name.includes(" ")) continue;

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
// Note the trailing \w* rather than a word boundary: these appear in URL path segments
// as "/instructors", "/our-team", "/meet-the-trainers", and a \b-anchored "instructor"
// does not match "instructors".
const STAFF_PAGE = /(?:instructor|trainer|our-?team|meet-the|staff|employee)\w*/i;

const TITLE_RANK = { Founder: 6, "Co-founder": 5, Owner: 4, CEO: 4, President: 3, "Co-owner": 2.5, "General Manager": 2 };
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
