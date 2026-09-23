#!/usr/bin/env node
// Tests for the contact-form parser. No network.
//
// The fixtures are trimmed verbatim from pages in the corpus: Gravity Forms as served by
// sitmeanssitforsyth.com and happypupmanor.com, Elementor from blueridgedogtrainers.com,
// Shopify's newsletter box from whitakerdog.com, and MailPoet from wonderdogs.net. They
// matter more here than anywhere else in this repo, because the failure mode is not a
// bad row in a table -- it is an outbound message to a real business that we cannot
// recall.
//
//   node scripts/lib/forms.test.mjs

import {
  extractForms, extractFields, extractLabels, selectOptions, roleOf, isHoneypot,
  classifyForm, fillForm, readResponse, actionUrl, parseForm, attr, decodeEntities,
  chooseOption,
} from "./forms.mjs";

let pass = 0, fail = 0;
function ok(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.error(`FAIL  ${name}`);
}

const identity = {
  firstName: "Test", lastName: "Sender", email: "t@example.com", phone: "5555550100",
  company: "Example Co", subject: "Inquiry", zip: "30004", city: "Atlanta", state: "GA",
};
const MESSAGE = "Hello -- a question about your training programs.";

// --- attribute and entity helpers ---------------------------------------------------

ok("a single-quoted attribute is read", attr("id='gform_2' method='post'", "id") === "gform_2");
ok("an unquoted attribute is read", attr("novalidate data-formid=2", "data-formid") === "2");
ok("a missing attribute is null", attr("method='post'", "action") === null);
ok("&#039; decodes to an apostrophe", decodeEntities("Dog&#039;s Name") === "Dog's Name");
ok("&#038; decodes to an ampersand", decodeEntities("a&#038;b") === "a&b");

// --- Gravity Forms: the dominant shape in the corpus ---------------------------------
//
// The critical property: the name attributes (`input_3`, `input_2`) say nothing, and
// the human meaning lives only in <label for="input_1_3">. A parser that reads names
// alone cannot tell the email box from the zip box.

const GRAVITY = `
<form method='post' enctype='multipart/form-data' id='gform_1' action='/contact/#gf_1' data-formid='1' novalidate>
  <label for='input_1_1_3'>First</label>
  <input name='input_1.3' id='input_1_1_3' type='text' placeholder='First Name*' required>
  <label for='input_1_1_6'>Last</label>
  <input name='input_1.6' id='input_1_1_6' type='text' placeholder='Last Name*' required>
  <label for='input_1_3'>Email   *</label>
  <input name='input_3' id='input_1_3' type='email' placeholder='Email*' required>
  <label for='input_1_2'>Phone   *</label>
  <input name='input_2' id='input_1_2' type='tel' placeholder='Phone*' required>
  <label for='input_1_4'>Zip Code   *</label>
  <input name='input_4' id='input_1_4' type='text' placeholder='Zip Code*' required>
  <label for='input_1_5'>Dog&#039;s Name</label>
  <input name='input_5' id='input_1_5' type='text'>
  <label for='input_1_20'>How Did You Find Us?   *</label>
  <select name='input_20' id='input_1_20' required>
    <option value=''>-- Please Select --</option>
    <option value='Google'>Google</option>
    <option value='Referral'>Referral</option>
  </select>
  <label for='input_1_6'>Message</label>
  <textarea name='input_6' id='input_1_6'></textarea>
  <input name='input_7' type='hidden' value=''>
  <input name='gform_submission_method' type='hidden' value='postback'>
  <input name='is_submit_1' type='hidden' value='1'>
  <input name='gform_submit' type='hidden' value='1'>
</form>`;

const gForms = extractForms(GRAVITY);
ok("one form is extracted", gForms.length === 1);

const gFields = extractFields(gForms[0].body);
const gLabels = extractLabels(gForms[0].body);
ok("every named control is extracted", gFields.length === 12);
ok("labels are keyed by the id they point at", gLabels.get("input_1_3") === "Email *");
ok("an entity in a label is decoded", gLabels.get("input_1_5") === "Dog's Name");

// The composite-name suffixes. `.3` is first and `.6` is last; nothing else says so.
const byName = (n) => gFields.find((f) => f.name === n);
ok("input_N.3 is the first-name box",
   roleOf(byName("input_1.3"), gLabels.get("input_1_1_3")) === "first_name");
ok("input_N.6 is the last-name box",
   roleOf(byName("input_1.6"), gLabels.get("input_1_1_6")) === "last_name");
ok("the email box is found by its label, not its name",
   roleOf(byName("input_3"), gLabels.get("input_1_3")) === "email");
ok("the phone box is found by its label",
   roleOf(byName("input_2"), gLabels.get("input_1_2")) === "phone");
ok("the zip box is not mistaken for something else",
   roleOf(byName("input_4"), gLabels.get("input_1_4")) === "zip");
ok("the textarea is the message box",
   roleOf(byName("input_6"), gLabels.get("input_1_6")) === "message");
// "Dog's Name" contains "name" -- it must NOT capture the full-name role and get our
// sender name, which would post "Test Sender" as the dog's name and read as nonsense.
ok("a dog's-name box is not filled as a person's name",
   fillForm({ form: gForms[0], fields: gFields, labels: gLabels, identity, message: MESSAGE })
     .body.every(([n, v]) => n !== "input_5" || v !== "Test Sender"));

// And if the site insists on knowing the dog's name, we abort rather than invent one.
const REQ_DOG = GRAVITY.replace("id='input_1_5' type='text'", "id='input_1_5' type='text' required");
const rdForm = extractForms(REQ_DOG)[0];
ok("a REQUIRED dog's-name box aborts rather than being invented",
   !fillForm({ form: rdForm, fields: extractFields(rdForm.body), labels: extractLabels(rdForm.body),
               identity, message: MESSAGE }).ok);

ok("a Gravity contact form classifies as submittable",
   classifyForm(gForms[0], gFields, gLabels).ok);

const gFilled = fillForm({ form: gForms[0], fields: gFields, labels: gLabels, identity, message: MESSAGE });
ok("the fill succeeds", gFilled.ok);
const gMap = new Map(gFilled.body);
ok("the email is posted to the email field", gMap.get("input_3") === "t@example.com");
ok("the message reaches the textarea", gMap.get("input_6") === MESSAGE);
ok("first and last name are posted separately",
   gMap.get("input_1.3") === "Test" && gMap.get("input_1.6") === "Sender");
// Gravity rejects a submission missing these outright; dropping them yields a 200 that
// goes nowhere, which is indistinguishable from success.
ok("is_submit_N is echoed back", gMap.get("is_submit_1") === "1");
ok("gform_submit is echoed back", gMap.get("gform_submit") === "1");
ok("an empty hidden field is still sent", gMap.has("input_7"));

// --- selects -------------------------------------------------------------------------

const selField = gFields.find((f) => f.tag === "select");
const opts = selectOptions(gForms[0].body, selField.at);
ok("the select's options are read", opts.length === 3);
ok("a required select gets a real option, not the placeholder",
   gMap.get("input_20") === "Google");

// "How did you find us?" is on many of these forms and the honest answer is a search.
// The first dry run answered "Word Of Mouth" on a live site, which invents a referrer
// the business may then go looking for.
ok("the placeholder option is never chosen",
   chooseOption([{ value: "", text: "-- Please Select --" }, { value: "G", text: "Google" }]) === "G");
ok("a truthful option is preferred over the first one",
   chooseOption([{ value: "W", text: "Word Of Mouth" }, { value: "G", text: "Google Search" }]) === "G");
ok("a referral is not claimed when no truthful option exists",
   chooseOption([{ value: "W", text: "Word Of Mouth" }, { value: "V", text: "My Veterinarian" }]) === null);
ok("a neutral option is acceptable when nothing is truthful",
   chooseOption([{ value: "W", text: "Word Of Mouth" }, { value: "X", text: "Puppy Class" }]) === "X");
ok("'Other' counts as truthful", chooseOption([{ value: "O", text: "Other" }]) === "O");
ok("an empty option list yields null", chooseOption([]) === null);

// And a required dropdown offering only untrue answers aborts the whole submission.
const LIE_ONLY = GRAVITY.replace("<option value='Google'>Google</option>", "<option value='Friend'>A Friend</option>");
const lf = extractForms(LIE_ONLY)[0];
ok("a required select with only untrue options aborts the submission",
   !fillForm({ form: lf, fields: extractFields(lf.body), labels: extractLabels(lf.body),
               identity, message: MESSAGE }).ok);

// --- honeypots -----------------------------------------------------------------------
//
// Filling one gets the submission silently discarded behind a cheerful confirmation --
// the worst outcome available, because it is recorded as a success.

ok("a WordPress hp-infix honeypot is recognized",
   isHoneypot({ name: "k9_hp_2", type: "text", id: null }, null));
ok("an Akismet ak_ prefix honeypot is recognized",
   isHoneypot({ name: "ak_hp_textarea", type: "textarea", id: null }, null));
ok("a plain honeypot name is recognized",
   isHoneypot({ name: "honeypot", type: "text", id: null }, null));
ok("an ordinary field is not a honeypot",
   !isHoneypot({ name: "input_3", type: "email", id: "input_1_3" }, "Email"));

const HONEY = GRAVITY.replace("<label for='input_1_1_3'>", "<input name='k9_hp_2' type='text'><label for='input_1_1_3'>");
const hForm = extractForms(HONEY)[0];
const hFilled = fillForm({
  form: hForm, fields: extractFields(hForm.body), labels: extractLabels(hForm.body),
  identity, message: MESSAGE,
});
ok("the honeypot is sent, and sent empty",
   new Map(hFilled.body).get("k9_hp_2") === "");

// The other honeypot convention, and the dangerous one: the marker is on the *wrapper*,
// not the input. thek9class.com serves a Gravity form whose `input_8` is labelled
// "Comments" and named like any other field -- nothing about the tag itself is
// suspicious, and the message regex filled it happily. Only the enclosing
// <li class="gfield--type-honeypot"> gives it away.
const WRAPPER_HONEYPOT = `
<form method='post' id='gform_8' action='/contact-us/'>
  <label for='input_8_3'>Email</label>
  <input name='input_3' id='input_8_3' type='email' required>
  <label for='input_8_4'>Message</label>
  <textarea name='input_4' id='input_8_4' required></textarea>
  <li id='field_1_8' class='gfield gfield--type-honeypot gform_validation_container'>
    <label for='input_8_8'>Comments</label>
    <input name='input_8' id='input_8_8' type='text' autocomplete='new-password'>
  </li>
  <input name='is_submit_8' type='hidden' value='1'>
</form>`;

const whForm = extractForms(WRAPPER_HONEYPOT)[0];
const whFields = extractFields(whForm.body);
const whLabels = extractLabels(whForm.body);
const whHoney = whFields.find((f) => f.name === "input_8");
ok("a gfield--type-honeypot wrapper marks the field it encloses",
   isHoneypot(whHoney, whLabels.get("input_8_8")));
ok("autocomplete='new-password' on a text box marks a honeypot",
   isHoneypot({ name: "x", type: "text", id: null, attrs: "autocomplete='new-password'" }, null));
const whFilled = fillForm({ form: whForm, fields: whFields, labels: whLabels, identity, message: MESSAGE });
// The failure this guards against is the worst one available: the message goes into the
// trap, the site returns its ordinary confirmation, and we record a send that nobody
// will ever read.
ok("the message goes to the real textarea and not to the trap",
   new Map(whFilled.body).get("input_4") === MESSAGE
   && new Map(whFilled.body).get("input_8") === "");

// --- prose labels that capture the wrong role ------------------------------------------
//
// blueridgedogtrainers.com puts a consent checkbox beside the email box, labelled with a
// sentence that contains the word "email". The role matcher read that sentence and
// posted our address INTO the consent box: it fails validation, and if it did not, it
// would be gibberish. Checkboxes carry their meaning in `value`, never in our data.
const CONSENT = `
<form method='post' id='gform_22' action='/contact/'>
  <label for='input_22_5'>Email</label>
  <input name='input_5' id='input_22_5' type='email' required>
  <label for='input_22_7'>Message</label>
  <textarea name='input_7' id='input_22_7' required></textarea>
  <label for='input_22_1_1'>* By providing your email &amp; phone number, you agree to receive emails, calls and texts.</label>
  <input name='input_22.1' id='input_22_1_1' type='checkbox' value='I Agree' required>
  <input name='is_submit_22' type='hidden' value='1'>
</form>`;

const cForm = extractForms(CONSENT)[0];
const cFilled = fillForm({
  form: cForm, fields: extractFields(cForm.body), labels: extractLabels(cForm.body),
  identity, message: MESSAGE,
});
const cMap = new Map(cFilled.body);
ok("a consent checkbox is ticked with its own value, not with our email",
   cMap.get("input_22.1") === "I Agree");
ok("the email still reaches the real email box", cMap.get("input_5") === "t@example.com");

// The pet-name guard has to cover the words the corpus actually uses. "Pup's Name" sits
// next to "Full Name" on the same form, and matched the loose name rule.
ok("a pup's-name box is not the sender's name",
   roleOf({ name: "pup", type: "text", id: null }, "Pup's Name") === null);
ok("a full-name box beside it still resolves",
   roleOf({ name: "fullname", type: "text", id: null }, "Full Name") === "full_name");

// --- dropdowns we have no answer for ---------------------------------------------------
//
// comesitstay.com offers an optional "Select Location" whose options are its two branch
// names. The first version picked the first one and routed the inquiry to Parker on the
// strength of nothing; an optional question we cannot answer is left alone.
const OPTIONAL_LOCATION = `
<form method='post' id='gform_1' action='/'>
  <label for='input_1_11'>Select Location</label>
  <select name='input_11' id='input_1_11'>
    <option value='' selected='selected'>Select Location</option>
    <option value='Parker'>Parker</option>
    <option value='Littleton'>Littleton</option>
  </select>
  <label for='input_1_8'>Email</label>
  <input name='input_8' id='input_1_8' type='email' required>
  <label for='input_1_5'>Message</label>
  <textarea name='input_5' id='input_1_5' required></textarea>
</form>`;

const olForm = extractForms(OPTIONAL_LOCATION)[0];
const olFilled = fillForm({
  form: olForm, fields: extractFields(olForm.body), labels: extractLabels(olForm.body),
  identity, message: MESSAGE,
});
ok("an optional branch-picker is left blank rather than guessed", olFilled.ok);
ok("no branch is chosen for us", !new Map(olFilled.body).has("input_11"));
// A <select> takes only the values it lists. "Select Location" reads as a location
// field, and our "Atlanta, GA" is not one of its two options -- posting it is either
// rejected or stored as a branch that does not exist.
ok("free text is never posted as an unlisted select value",
   !olFilled.body.some(([, v]) => v === "Atlanta, GA"));

// "Where are you located?" is required on upstatecanine.com and aborted the whole
// submission until it was given a role. It is a question we can answer truthfully.
const LOCATED = `
<form method='post' id='gform_2' action='/contact-us/'>
  <label for='input_2_9'>Where are you located? (Required)</label>
  <input name='input_9' id='input_2_9' type='text' required>
  <label for='input_2_2'>Email</label>
  <input name='input_2' id='input_2_2' type='email' required>
  <label for='input_2_3'>Message</label>
  <textarea name='input_3' id='input_2_3' required></textarea>
</form>`;

const locForm = extractForms(LOCATED)[0];
const locFilled = fillForm({
  form: locForm, fields: extractFields(locForm.body), labels: extractLabels(locForm.body),
  identity, message: MESSAGE,
});
ok("a required where-are-you-located box no longer aborts", locFilled.ok);
ok("it is answered with the sender's real city and state",
   new Map(locFilled.body).get("input_9") === "Atlanta, GA");

// --- forms that must NOT be submitted -------------------------------------------------
//
// Three of the 14 sampled pages had a newsletter box as their only form. Posting to one
// subscribes us to a mailing list and reaches nobody.

const NEWSLETTER = `
<form method="post" action="https://zvhwd.maillist-manage.com/weboptin.zc" id="zcampaignOptinForm">
  <input type="text" name="CONTACT_EMAIL" placeholder="Email">
  <input type="hidden" name="submitType" value="optinCustomView">
</form>`;
const nf = extractForms(NEWSLETTER)[0];
ok("a newsletter opt-in is refused",
   !classifyForm(nf, extractFields(nf.body), extractLabels(nf.body)).ok);

const SHOPIFY_SIGNUP = `
<form method="post" action="/contact#contact_form" id="contact_form" class="email-signup__form">
  <input type="hidden" name="form_type" value="customer">
  <input type="hidden" name="utf8" value="X">
  <input type="email" name="contact[email]" required>
</form>`;
const sf = extractForms(SHOPIFY_SIGNUP)[0];
// It has an email field and posts to /contact -- but with no message box it is the
// footer signup, not an inquiry form. Requiring a message box is what tells them apart.
ok("an email-only signup is refused for want of a message field",
   classifyForm(sf, extractFields(sf.body), extractLabels(sf.body)).reason.includes("message"));

const SEARCH = `<form role="search" method="get" class="search-form" action="https://x.com/">
  <input type="search" name="s"><input type="submit"></form>`;
const searchForm = extractForms(SEARCH)[0];
ok("a search form is refused",
   !classifyForm(searchForm, extractFields(searchForm.body), extractLabels(searchForm.body)).ok);

const GET_FORM = GRAVITY.replace("method='post'", "method='get'");
const gf = extractForms(GET_FORM)[0];
ok("a GET form is refused",
   classifyForm(gf, extractFields(gf.body), extractLabels(gf.body)).reason.includes("GET"));

const CAPTCHA = GRAVITY.replace("</form>", '<div class="g-recaptcha" data-sitekey="x"></div></form>');
const cf = extractForms(CAPTCHA)[0];
ok("a captcha-protected form is refused",
   classifyForm(cf, extractFields(cf.body), extractLabels(cf.body)).reason.includes("captcha"));

const UPLOAD = GRAVITY.replace("<textarea", "<input type='file' name='input_30'><textarea");
const uf = extractForms(UPLOAD)[0];
ok("a form with a file upload is refused",
   classifyForm(uf, extractFields(uf.body), extractLabels(uf.body)).reason.includes("file"));

// --- an unrecognised required field aborts --------------------------------------------
//
// Half an inquiry is worse than none: it reaches a human with our name on it and reads
// as spam. Fail closed.

const ODD = GRAVITY.replace(
  "<label for='input_1_6'>Message</label>",
  "<label for='input_1_44'>Kennel license number *</label>" +
  "<input name='input_44' id='input_1_44' type='text' required>" +
  "<label for='input_1_6'>Message</label>"
);
const of_ = extractForms(ODD)[0];
const oFilled = fillForm({
  form: of_, fields: extractFields(of_.body), labels: extractLabels(of_.body),
  identity, message: MESSAGE,
});
ok("an unrecognised REQUIRED field aborts the submission", !oFilled.ok);
ok("the abort names the field", oFilled.reason.includes("Kennel license number"));

const OPTIONAL_ODD = ODD.replace("id='input_1_44' type='text' required", "id='input_1_44' type='text'");
const oof = extractForms(OPTIONAL_ODD)[0];
ok("an unrecognised OPTIONAL field is simply left out",
   fillForm({ form: oof, fields: extractFields(oof.body), labels: extractLabels(oof.body),
              identity, message: MESSAGE }).ok);

// --- Elementor ------------------------------------------------------------------------

const ELEMENTOR = `
<form class="elementor-form" method="post" name="Contact Form">
  <input type="hidden" name="post_id" value="1712">
  <input type="hidden" name="form_id" value="36cf5d51">
  <input type="text" name="form_fields[name]" placeholder="Name">
  <input type="email" name="form_fields[email]" placeholder="Email" required>
  <textarea name="form_fields[message]" placeholder="Message"></textarea>
</form>`;
const ef = extractForms(ELEMENTOR)[0];
const eFields = extractFields(ef.body);
const eLabels = extractLabels(ef.body);
ok("an Elementor contact form is submittable", classifyForm(ef, eFields, eLabels).ok);
const eMap = new Map(fillForm({ form: ef, fields: eFields, labels: eLabels, identity, message: MESSAGE }).body);
ok("bracketed field names are filled by placeholder",
   eMap.get("form_fields[email]") === "t@example.com");
ok("the Elementor name box gets the full name",
   eMap.get("form_fields[name]") === "Test Sender");
ok("Elementor's hidden form_id is echoed", eMap.get("form_id") === "36cf5d51");

// --- action URLs ------------------------------------------------------------------------

ok("a fragment-only action posts back to the page",
   actionUrl({ attrs: "action='/contact/#gf_1'" }, "https://x.com/contact/") === "https://x.com/contact/");
ok("an empty action posts back to the page",
   actionUrl({ attrs: "method='post'" }, "https://x.com/contact/?a=1") === "https://x.com/contact/?a=1");
ok("a bare '#' action posts back to the page",
   actionUrl({ attrs: "action='#'" }, "https://x.com/c/") === "https://x.com/c/");
ok("an absolute action is used as given",
   actionUrl({ attrs: "action='https://y.com/post'" }, "https://x.com/") === "https://y.com/post");
ok("an encoded ampersand in the action is decoded",
   actionUrl({ attrs: "action='/?a=1&#038;b=2'" }, "https://x.com/") === "https://x.com/?a=1&b=2");

// --- reading the response ---------------------------------------------------------------
//
// The status code carries no information: a form that re-renders with validation errors
// returns 200 exactly like one that succeeded.

ok("a thank-you page is a send", readResponse(200, "Thank you for contacting us!").outcome === "sent");
ok("a we-will-be-in-touch page is a send",
   readResponse(200, "Got it. We'll be in touch shortly.").outcome === "sent");
ok("a validation error is a rejection",
   readResponse(200, "There was a problem with your submission. Please check.").outcome === "rejected");
ok("a required-field complaint is a rejection",
   readResponse(200, "This field is required").outcome === "rejected");
// The specific signal wins: a themed page can carry "thanks for visiting" chrome and a
// real error at once.
ok("an error outranks incidental thank-you chrome",
   readResponse(200, "Thanks for visiting. There was a problem with your submission.").outcome === "rejected");
ok("silence is unknown, not success",
   readResponse(200, "<html><body>Contact</body></html>").outcome === "unknown");
ok("a 403 is blocked, not a failed send", readResponse(403, "").outcome === "blocked");
ok("a 429 is blocked", readResponse(429, "").outcome === "blocked");
ok("a 500 is blocked", readResponse(500, "").outcome === "blocked");
ok("no response at all is blocked", readResponse(0, null).outcome === "blocked");

// --- parseForm end to end ----------------------------------------------------------------

const parsed = parseForm(extractForms(GRAVITY)[0], "https://sitmeanssitforsyth.com/contact/");
ok("parseForm keys the form by its id", parsed.key === "gform_1");
ok("parseForm absolutises the action",
   parsed.action === "https://sitmeanssitforsyth.com/contact/");
ok("parseForm carries the enctype", parsed.enctype === "multipart/form-data");
ok("parseForm reports it submittable", parsed.ok);
ok("parseForm attaches select options",
   parsed.fields.find((f) => f.tag === "select").options.length === 3);

console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
