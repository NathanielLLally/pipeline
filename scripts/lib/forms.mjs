// Contact-form parsing and filling. Pure functions over HTML, no network, so the
// rules that decide what gets POSTed to a stranger's website are testable.
//
// The stakes are asymmetric and worse than anywhere else in this pipeline. A bad email
// filter wastes a send; a bad form filler posts a malformed inquiry to a real business
// under our own name, and there is no way to unsend it. So every rule here fails
// closed: anything not confidently understood is skipped, never guessed at and sent.
//
// The field-naming conventions below were read off 14 live pages from the corpus rather
// than from documentation. Gravity Forms (WordPress) dominates -- it names every field
// `input_<n>` with the human label in a separate <label for="input_<formid>_<n>">, so
// the name attribute alone is useless and the label has to be matched back through the
// id. That single fact shapes the whole parser.

/** Splits the page into <form>...</form> blocks, keeping each one's opening tag. */
export function extractForms(html) {
  const out = [];
  const re = /<form\b([^>]*)>([\s\S]*?)<\/form>/gi;
  let m;
  let index = 0;
  while ((m = re.exec(html))) {
    out.push({ attrs: m[1], body: m[2], index: index++, raw: m[0] });
  }
  return out;
}

/** One attribute off a tag's attribute string. */
export function attr(attrs, name) {
  const m = attrs.match(new RegExp(`\\b${name}\\s*=\\s*["']([^"']*)["']`, "i"))
    || attrs.match(new RegExp(`\\b${name}\\s*=\\s*([^\\s>"']+)`, "i"));
  return m ? decodeEntities(m[1]) : null;
}

export function decodeEntities(s) {
  return String(s)
    .replace(/&#0?39;|&apos;|&rsquo;/g, "'")
    .replace(/&quot;|&ldquo;|&rdquo;/g, '"')
    .replace(/&#0?38;|&amp;/g, "&")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#(\d+);/g, (_, d) => String.fromCharCode(Number(d)));
}

/** Every control in a form body, with the attributes the filler needs. */
export function extractFields(body) {
  const fields = [];
  for (const m of body.matchAll(/<(input|textarea|select)\b([^>]*)>/gi)) {
    const tag = m[1].toLowerCase();
    const a = m[2];
    const name = attr(a, "name");
    if (!name) continue;
    fields.push({
      tag,
      name,
      id: attr(a, "id"),
      type: (attr(a, "type") || (tag === "textarea" ? "textarea" : tag)).toLowerCase(),
      value: attr(a, "value"),
      placeholder: attr(a, "placeholder"),
      required: /\brequired\b/i.test(a) || /aria-required\s*=\s*["']true/i.test(a),
      // Offset in the body, so a <select>'s options can be read from what follows.
      at: m.index,
      // The markup immediately enclosing the field. A honeypot is very often marked on
      // its wrapper rather than on the input -- Gravity Forms writes
      // <li class="gfield--type-honeypot"> around an input whose own name and label are
      // entirely innocent -- so the field tag alone cannot identify one.
      context: body.slice(Math.max(0, m.index - 500), m.index),
      attrs: a,
    });
  }
  return fields;
}

/** id -> label text, so a `input_3` field can be matched by its visible label. */
export function extractLabels(body) {
  const labels = new Map();
  for (const m of body.matchAll(/<label\b([^>]*)>([\s\S]*?)<\/label>/gi)) {
    const forId = attr(m[1], "for");
    if (!forId) continue;
    const text = decodeEntities(m[2].replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
    if (text) labels.set(forId, text);
  }
  return labels;
}

/** The option values of the <select> that starts at `at`. */
export function selectOptions(body, at) {
  const tail = body.slice(at);
  const end = tail.search(/<\/select>/i);
  const scope = end < 0 ? tail.slice(0, 4000) : tail.slice(0, end);
  const out = [];
  for (const m of scope.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/gi)) {
    const value = attr(m[1], "value");
    const text = decodeEntities(m[2].replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();
    if (value === null && !text) continue;
    out.push({ value: value ?? text, text });
  }
  return out;
}

// What a field is for, decided from its name, id, placeholder and label together. The
// first pattern to match wins, so the order is the priority order -- `first_name` must
// be tested before the looser `name`, or every first-name box is filled with a full name.
//
// `input_1.3` / `input_1.6` are Gravity Forms' encoding of a composite name field:
// .3 is first, .6 is last. They carry no label of their own beyond "First"/"Last", so
// the suffix is the only reliable signal and it is matched explicitly.
const ROLES = [
  ["first_name", /(?:^|[^a-z])(?:first[\s_-]*name|fname|given)|\binput_\d+\.3$|^first$/i],
  ["last_name", /(?:^|[^a-z])(?:last[\s_-]*name|lname|surname|family)|\binput_\d+\.6$|^last$/i],
  ["email", /e-?mail/i],
  ["phone", /phone|mobile|cell|telephone|\btel\b/i],
  ["company", /company|business[\s_-]*name|organi[sz]ation|\bbrand\b/i],
  ["message", /message|comment|question|inquiry|enquiry|details|tell[\s_-]*us|how[\s_-]*can[\s_-]*we|notes?\b/i],
  ["zip", /\bzip\b|postal|post[\s_-]*code/i],
  ["city", /\bcity\b|\btown\b/i],
  ["state", /\bstate\b|\bprovince\b/i],
  // "Where are you located?" is required on upstatecanine.com and aborted the whole
  // submission on the first dry run. It is a question we can answer honestly and
  // precisely, so it gets "City, ST" rather than a refusal.
  ["location", /where\s+(?:are|is)\s+you|your\s+location|\blocation\b|\barea\b/i],
  ["subject", /subject|topic|regarding/i],
  ["full_name", /\bname\b|your[\s_-]*name/i],
];

// Things that have a name but are not the sender. Every one of these boxes matches the
// loose `\bname\b` that catches "Your Name", and a dog-training contact form asks for
// at least one of them: the corpus has "Dog's Name", "Pet Name" and "Breed" sitting
// directly beside the person's name field. Filling one posts "Test Sender" as the dog's
// name, which reads as spam to the human who opens it.
const NOT_THE_SENDERS_NAME =
  /\b(?:dogs?|pets?|pup|pups|puppy|puppies|animals?|breed|cats?|kennel|user|screen|file|domain|furry|companion)\b/i;

/**
 * Classifies a field by role, using the label text in preference to the name.
 *
 * Label first because the machine-generated names are meaningless: Gravity Forms calls
 * the email box `input_3` on one site and `input_5` on the next, while both label it
 * "Email". Only when there is no label does the name attribute get a vote.
 */
export function roleOf(field, labelText) {
  const typeHint = { email: "email", tel: "phone" }[field.type];
  const haystacks = [labelText, field.placeholder, field.name, field.id].filter(Boolean);
  // The subject's own name is the only name we are entitled to supply. A box asking for
  // the dog's gets nothing, and if it is required the submission aborts rather than
  // inventing one.
  const describesSomethingElse = [labelText, field.placeholder].filter(Boolean)
    .some((h) => NOT_THE_SENDERS_NAME.test(h));
  for (const [role, re] of ROLES) {
    if (describesSomethingElse && /name/.test(role)) continue;
    if (haystacks.some((h) => re.test(h))) {
      // The type attribute is authoritative over a loose text match: a box the site
      // declared type="email" is an email box whatever its label says.
      if (typeHint && role !== typeHint && !/name|message/.test(role)) return typeHint;
      return role;
    }
  }
  return typeHint || null;
}

// Fields that exist to catch bots. Filling one is how a submission gets silently
// discarded -- the form returns a cheerful confirmation and the business never sees it,
// which is the worst possible outcome because it looks like success.
//
// `k9_hp_2` and `ak_hp_textarea` are both in the corpus: the WordPress convention is an
// `hp` (honeypot) infix, and Akismet's is the `ak_` prefix.
export const HONEYPOT =
  /(?:^|[_-])(?:hp|honeypot|honey|bot|trap|url|website|fax|comment_?field)(?:[_-]|$)|^ak_|_hp_|\bhoneypot\b/i;

// Honeypots declared on the field's wrapper instead of the field. Gravity Forms emits
// <li class="gfield--type-honeypot"> around an input named `input_8` labelled
// "Comments" -- nothing about the input itself gives it away, and the label is exactly
// what the message matcher looks for. Filling it made a submission look accepted while
// being discarded, which is the single worst failure available here.
const HONEYPOT_CONTEXT =
  /gfield--type-honeypot|gform_validation_container|\b(?:honeypot|hp-?field|visually-hidden-field|screen-reader-only-input)\b|(?:display\s*:\s*none|visibility\s*:\s*hidden|left\s*:\s*-\d{4,}px)/i;

export function isHoneypot(field, labelText) {
  if (field.type === "hidden") return false;      // hidden fields are handled separately
  if (HONEYPOT.test(field.name) || (field.id && HONEYPOT.test(field.id))) return true;
  // autocomplete="new-password" on a text input is the other half of the Gravity
  // convention: it stops a browser autofilling the trap.
  if (field.attrs && /autocomplete\s*=\s*["']new-password/i.test(field.attrs)
      && field.type === "text") return true;
  // Visually hidden but not type=hidden: read from the wrapper the field sits in.
  return Boolean(field.context && HONEYPOT_CONTEXT.test(field.context.slice(-400)));
}

// Forms that are not contact forms. Posting to a search box does nothing; posting to a
// newsletter opt-in subscribes us to their mailing list, which is both useless and
// rude, and posting to a login or checkout form is worse.
const NOT_CONTACT = /\b(?:search|login|log-?in|signin|sign-?in|register|cart|checkout|password|newsletter|subscribe|optin|opt-?in|mailpoet|zcampaign|maillist-manage|mc4wp|klaviyo)\b/i;

/**
 * Decides whether a parsed form is a contact form worth submitting.
 *
 * The test is deliberately strict: an email box AND a free-text message box AND no
 * newsletter/search markers. Requiring the message box is what separates a real
 * inquiry form from the newsletter widget in the footer, which also asks for an email
 * and nothing else -- 3 of the 14 sampled pages had exactly that shape, and two of them
 * were the ONLY form on the page.
 */
export function classifyForm(form, fields, labels) {
  const marker = `${form.attrs} ${attr(form.attrs, "id") || ""} ${attr(form.attrs, "class") || ""} ${attr(form.attrs, "action") || ""}`;
  if (NOT_CONTACT.test(marker)) return { ok: false, reason: "not a contact form (search/newsletter/auth)" };
  if ((attr(form.attrs, "method") || "get").toLowerCase() !== "post") {
    return { ok: false, reason: "form is GET, not a submission endpoint" };
  }

  const roles = new Set();
  for (const f of fields) {
    if (f.type === "hidden" || f.type === "submit" || f.type === "button") continue;
    if (isHoneypot(f, labels.get(f.id))) continue;
    const r = roleOf(f, labels.get(f.id));
    if (r) roles.add(r);
  }
  if (!roles.has("email")) return { ok: false, reason: "no email field" };
  if (!roles.has("message")) return { ok: false, reason: "no message field (likely a newsletter opt-in)" };
  if (fields.some((f) => /file/i.test(f.type))) return { ok: false, reason: "file upload field" };
  if (/recaptcha|hcaptcha|turnstile/i.test(form.body)) return { ok: false, reason: "captcha present" };
  return { ok: true, roles: [...roles] };
}

// Placeholder first options, which are not answers.
const PLACEHOLDER_OPTION = /^\s*(?:-+|select|choose|please|pick|none|--)/i;

// Option text that would be a lie if we picked it. "How did you find us?" is on a good
// many of these forms, and the honest answer is a web search: we found them on Google
// Maps. Answering "word of mouth" or "referral" -- which is what taking the first real
// option did on the first dry run -- invents a referrer who does not exist, and the
// business may well go looking for them.
const UNTRUE_OPTION =
  /\b(?:referr?al|word of mouth|friend|family|existing|current client|repeat|returning|drove by|drive[- ]?by|saw your sign|billboard|radio|tv|newspaper|flyer|mailer|vet|veterinarian|groomer|trainer recommend)/i;

// Where we actually came from, best first.
const TRUE_OPTION = /\b(?:google|web ?site|internet|online|web search|search engine|maps|other)\b/i;

/**
 * Picks an option for a dropdown whose meaning is not otherwise understood.
 *
 * Prefers a truthful answer, accepts a neutral one, and returns null rather than
 * asserting something false -- a null on a required field aborts the submission, which
 * is the right outcome. These are messages to real people; the small dishonesty of a
 * mis-picked dropdown is still a dishonesty, and it is the sort that gets noticed.
 */
export function chooseOption(options) {
  const real = (options || []).filter((o) => o.value && !PLACEHOLDER_OPTION.test(o.text || o.value));
  if (!real.length) return null;
  const truthful = real.find((o) => TRUE_OPTION.test(o.text || o.value));
  if (truthful) return truthful.value;
  const neutral = real.find((o) => !UNTRUE_OPTION.test(o.text || o.value));
  return neutral ? neutral.value : null;
}

/**
 * Builds the POST body.
 *
 * Three rules, each one learned from the sampled markup:
 *
 *  1. Every hidden field is echoed back with the value the page shipped. Gravity Forms
 *     will not accept a submission without `is_submit_<n>`, `gform_submit` and the
 *     per-form nonce, and Shopify needs `form_type`/`utf8`. Dropping them is how a
 *     submission gets a 200 and goes nowhere.
 *  2. Honeypots are left empty, explicitly, rather than omitted -- an absent field is
 *     itself a bot signal for some plugins.
 *  3. A required field whose role is not understood aborts the whole submission. Half
 *     an inquiry is worse than none: it reaches a human with our name on it and reads
 *     as spam. `selectOptions` covers the common "How did you hear about us?" dropdown;
 *     anything else required and unrecognised is a skip.
 */
export function fillForm({ form, fields, labels, identity, message }) {
  const body = [];
  const unfilled = [];

  for (const f of fields) {
    if (f.type === "submit" || f.type === "button" || f.type === "image") continue;

    if (f.type === "hidden") {
      body.push([f.name, f.value ?? ""]);
      continue;
    }
    const labelText = labels.get(f.id);
    if (isHoneypot(f, labelText)) {
      body.push([f.name, ""]);
      continue;
    }

    // Checkboxes and radios carry their meaning in the `value` attribute, not in free
    // text, and their labels are prose that the role matcher reads far too eagerly. The
    // live corpus has a consent checkbox labelled "By providing your email & phone
    // number, you agree to receive emails..." -- the word "email" in that sentence made
    // the first version post our address INTO the consent box, which both fails
    // validation and is nonsense. Ticked with their own value, never with our data.
    if (f.type === "checkbox" || f.type === "radio") {
      if (f.required) body.push([f.name, f.value ?? "on"]);
      continue;
    }

    const role = roleOf(f, labelText);
    let value = null;
    switch (role) {
      case "first_name": value = identity.firstName; break;
      case "last_name": value = identity.lastName; break;
      case "full_name": value = `${identity.firstName} ${identity.lastName}`; break;
      case "email": value = identity.email; break;
      case "phone": value = identity.phone; break;
      case "company": value = identity.company; break;
      case "message": value = message; break;
      case "subject": value = identity.subject; break;
      case "zip": value = identity.zip; break;
      case "city": value = identity.city; break;
      case "state": value = identity.state; break;
      case "location": value = `${identity.city}, ${identity.state}`; break;
      default: value = null;
    }

    // A <select> accepts only the values it lists. A role match can produce text that is
    // not among them -- "Select Location" reads as a location field but offers only two
    // branch names -- and posting an unlisted value is either rejected outright or, on a
    // server that does not validate, stored as a location that does not exist.
    if (value !== null && f.tag === "select") {
      const opts = f.options || selectOptions(form.body, f.at);
      if (!opts.some((o) => o.value === value)) value = null;
    }

    // An unrecognised dropdown is guessed at only when the site insists on an answer.
    // comesitstay.com offers an OPTIONAL "Select Location" of Parker or Littleton; the
    // first version picked Parker, which routes the inquiry to one of two branches on
    // the strength of nothing at all. Left blank, the form goes wherever the business
    // sends an unrouted inquiry, which is their decision to make rather than ours.
    if (value === null && f.tag === "select" && f.required) {
      // Resolved here when the caller has not already done it, because forgetting to
      // attach options would otherwise turn every select into an abort -- a silent
      // halving of the pass's reach that looks like sites being difficult.
      const chosen = chooseOption(f.options || selectOptions(form.body, f.at));
      if (chosen) value = chosen;
    }

    if (value === null) {
      if (f.required) unfilled.push(labelText || f.name);
      continue;
    }
    body.push([f.name, value]);
  }

  if (unfilled.length) {
    return { ok: false, reason: `required field not understood: ${unfilled.join(", ")}` };
  }
  return { ok: true, body };
}

// Response phrasing that indicates the submission landed, and phrasing that indicates
// it bounced. Checked against the extracted text of the response, because a form that
// re-renders with errors returns 200 exactly like one that succeeded -- the status code
// carries no information here at all.
export const CONFIRM_RE =
  /\b(?:thank you|thanks for (?:your|reaching|contacting|getting)|message (?:has been )?(?:sent|received|submitted)|we(?:'| ha)ve received|we(?:'ll| will) (?:be in touch|get back|contact you|reach out)|submission (?:was )?(?:successful|received)|form (?:was )?submitted|successfully submitted|your (?:request|inquiry|message) (?:has been|was) (?:sent|received))/i;

export const ERROR_RE =
  /\b(?:there was a problem|errors? (?:were|was) found|please (?:correct|complete|fill|enter|check)|this field is required|required field|invalid (?:email|entry|input)|could not be (?:sent|submitted)|please try again)/i;

/**
 * Reads the response to a submission.
 *
 * Returns `unknown` when neither pattern matches, and `unknown` is terminal -- the pass
 * will not resend. That is the conservative choice: a site that quietly redirects to a
 * thank-you page it renders with JavaScript has probably received the message, and the
 * cost of assuming otherwise is sending a stranger a duplicate.
 */
export function readResponse(status, text) {
  if (status === 0) return { outcome: "blocked", note: "no response" };
  if (status === 403 || status === 429 || status >= 500) {
    return { outcome: "blocked", note: `HTTP ${status}` };
  }
  const head = (text || "").slice(0, 4000);
  // Error first: a page that shows both "Thank you for visiting" chrome and a real
  // validation error is a failure, and the error is the more specific signal.
  const err = head.match(ERROR_RE);
  if (err) return { outcome: "rejected", note: err[0].slice(0, 200) };
  const ok = head.match(CONFIRM_RE);
  if (ok) return { outcome: "sent", note: ok[0].slice(0, 200) };
  return { outcome: "unknown", note: `HTTP ${status}, no confirmation or error text` };
}

/**
 * Absolutises a form action, which is very often empty or a bare fragment.
 *
 * The fragment is always stripped, from the action and from the page URL alike. It is
 * never sent to a server -- `action='/contact/#gf_1'` posts to `/contact/` -- but it is
 * also part of the row's unique key, and Gravity Forms puts the form id in it, so
 * leaving it on would make the same form look like a different endpoint depending on
 * which page it was found through.
 */
export function actionUrl(form, pageUrl) {
  const action = attr(form.attrs, "action");
  try {
    const u = !action || action.startsWith("#")
      ? new URL(pageUrl)
      : new URL(decodeEntities(action), pageUrl);
    u.hash = "";
    return u.href;
  } catch {
    return null;
  }
}

/**
 * Everything about one form on one page, ready to submit or to explain a skip.
 *
 * The original `form` is carried through on `.form` so the caller can hand the same
 * object straight to fillForm. Matching it back up by id afterwards looked tidier and
 * was wrong: a form whose only identifier is its index (`form-0`) matched nothing, and
 * the caller silently filled a different form from the one it had classified.
 */
export function parseForm(form, pageUrl) {
  const fields = extractFields(form.body);
  for (const f of fields) if (f.tag === "select") f.options = selectOptions(form.body, f.at);
  const labels = extractLabels(form.body);
  const verdict = classifyForm(form, fields, labels);
  return {
    key: attr(form.attrs, "id") || `form-${form.index}`,
    action: actionUrl(form, pageUrl),
    enctype: (attr(form.attrs, "enctype") || "application/x-www-form-urlencoded").toLowerCase(),
    form,
    fields,
    labels,
    ...verdict,
  };
}
