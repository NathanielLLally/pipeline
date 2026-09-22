-- Contact emails: the deliverable the rest of the pipeline exists to produce.
--
-- Its own table rather than a column on businesses, for the same reasons as
-- website_crawl and ads_transparency: a business can have several addresses, each one
-- needs the page it came from so a claim is auditable, and a re-run has to know what
-- was already extracted. A single `email` column would force a choice between
-- `info@` and the owner's personal address at extraction time, before there is enough
-- evidence to make it well.
--
-- Nothing here is invented. Every row records an address that appeared verbatim in
-- text fetched from that business's own site, with the source URL attached. Guessing
-- `firstname@domain` from a decision-maker name is a different and much weaker
-- operation; if that is ever added it must be marked with a distinct `source` value
-- so inferred addresses can be excluded from sends, never blended in with observed
-- ones.

CREATE TABLE IF NOT EXISTS leads.business_email (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  email          text NOT NULL,        -- normalized: lowercased, trimmed
  local_part     text NOT NULL,        -- before the @, kept for role/personal analysis
  domain         text NOT NULL,        -- after the @

  -- How the address was obtained. 'crawl' = seen verbatim in fetched page text.
  -- Any future inferred/purchased source gets its own value here and must stay
  -- distinguishable, because confidence in an address is mostly confidence in how it
  -- was acquired.
  source         text NOT NULL DEFAULT 'crawl',
  source_url     text,                 -- the page it appeared on
  page_kind      text,                 -- home | about | contact | pricing

  -- Classification, derived at extraction time from the address itself.
  -- is_role: info@, hello@, admin@ -- reaches the business but not a named person.
  -- is_free_mail: gmail/yahoo/etc -- common and legitimate for small businesses here,
  --   but it means the address cannot be verified against the site's own domain.
  -- matches_site_domain: the address is on the same domain as the business website,
  --   which is the strongest available evidence that it genuinely belongs to them.
  is_role             boolean NOT NULL DEFAULT false,
  is_free_mail        boolean NOT NULL DEFAULT false,
  matches_site_domain boolean NOT NULL DEFAULT false,

  -- 0-100, from the signals above plus where on the site it was found. Used to pick
  -- ONE address per business for outreach without discarding the others.
  confidence     integer,

  extracted_at   timestamptz NOT NULL DEFAULT now()
);

-- One row per (business, address). A re-extraction updates in place, so the pass is
-- idempotent and can be re-run after every new crawl without accumulating duplicates.
CREATE UNIQUE INDEX IF NOT EXISTS business_email_business_email_uidx
  ON leads.business_email (business_id, email);

CREATE INDEX IF NOT EXISTS business_email_business_idx ON leads.business_email (business_id);
CREATE INDEX IF NOT EXISTS business_email_email_idx    ON leads.business_email (email);
-- Supports "best address per business", the query every export will run.
CREATE INDEX IF NOT EXISTS business_email_best_idx
  ON leads.business_email (business_id, confidence DESC NULLS LAST);

COMMENT ON TABLE leads.business_email IS
  'Contact email evidence, one row per (business, address), each traceable to the page '
  'it was observed on. source=crawl means the address appeared verbatim in fetched '
  'page text; inferred addresses must use a different source value and never be mixed '
  'with observed ones in a send list.';

-- Rolled up onto businesses so exports and scoring do not need the join. Mirrors the
-- ads_transparency pattern: the evidence table is authoritative, these are a cache.
ALTER TABLE leads.businesses
  ADD COLUMN IF NOT EXISTS contact_email        text,
  ADD COLUMN IF NOT EXISTS contact_email_count  integer,
  ADD COLUMN IF NOT EXISTS contact_email_is_role boolean;

COMMENT ON COLUMN leads.businesses.contact_email IS
  'Highest-confidence address from leads.business_email. A cache of that table, '
  'rewritten by scripts/extract-emails.mjs; never edited directly.';
