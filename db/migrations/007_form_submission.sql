-- Contact-form submissions, one row per (business, form) we have attempted.
--
-- The fourth contact channel. 242 businesses in the corpus publish a contact *form* and
-- no address anywhere on their site, which is not an accident: they chose the form
-- instead. For those, the form IS the address, and a submission is the only way to
-- reach them short of the phone.
--
-- This table is deliberately NOT shaped like leads.domain_rdap. RDAP asks a question and
-- records the answer, so a failed request is safely retried. A form submission is an
-- outbound message to a real business, so the hazard runs the other way: a POST that
-- times out may well have been delivered, and a blind retry sends a stranger a second
-- copy of the same inquiry. So:
--
--   * The attempt row is written BEFORE the request goes out, not after. If the process
--     dies mid-send, the record of having tried survives.
--   * `outcome='unknown'` -- we sent bytes and never learned what happened -- is a
--     terminal state by default. Re-running skips it. Only an explicit
--     --retry-unknown, chosen by a human who has decided a second copy is acceptable,
--     will try it again.
--
-- Nothing here is a `leads.business_email` row, because no address was learned. This is
-- a record of contact having been made by another route, and downstream campaign
-- tooling should read it as "already approached, do not also cold-email".
CREATE TABLE IF NOT EXISTS leads.form_submission (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  page_url     text NOT NULL,        -- the crawled page the form was found on
  action_url   text NOT NULL,        -- where the POST actually went, absolutised
  form_key     text NOT NULL,        -- the form's id attribute, or its index on the page

  -- sent      -- posted, and the response looked like a confirmation
  -- rejected  -- posted, and the response came back with validation errors
  -- unknown   -- posted, and the response said nothing either way (see above: terminal)
  -- blocked   -- the request never reached the site (403/429/transport): safe to retry
  -- skipped   -- parsed but deliberately not sent; `note` says why
  outcome      text NOT NULL,

  http_status  integer,
  note         text,                 -- the confirmation/error text, or the skip reason
  fields_sent  jsonb,                -- exactly what was posted, for auditing a complaint
  submitted_at timestamptz NOT NULL DEFAULT now()
);

-- One attempt per form. The form_key keeps a site's contact form distinct from its
-- newsletter box without depending on the order they appear in the markup.
CREATE UNIQUE INDEX IF NOT EXISTS form_submission_unique_idx
  ON leads.form_submission (business_id, action_url, form_key);
CREATE INDEX IF NOT EXISTS form_submission_outcome_idx
  ON leads.form_submission (outcome);
