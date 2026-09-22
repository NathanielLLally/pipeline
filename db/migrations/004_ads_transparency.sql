-- Phase D (Prompt 7, second source): Google Ads Transparency evidence.
--
-- This is the database's first CONFIRMED-CURRENT-SPEND signal. `marketing_active` says
-- only that paid acquisition infrastructure is present -- a pixel outlives the campaign
-- that installed it, so one left from a six-week push two years ago looks identical to
-- one backing $8k/month. A creative returned here is an ad Google is serving, with the
-- date it was last shown attached, so recency is a fact rather than an assumption.
--
-- Kept in its own table for the same reasons as website_crawl: it is raw evidence with a
-- timestamp, a re-run needs to know what was already checked so the pass is incremental,
-- and any claim made from it should be traceable to the moment it was observed. The
-- rolled-up booleans on leads.businesses are derived from this, never the reverse.

CREATE TABLE IF NOT EXISTS leads.ads_transparency (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  domain          text NOT NULL,        -- the domain actually queried, www stripped
  advertiser_id   text,                 -- Google's AR... id; stable across creatives
  advertiser_name text,                 -- display name, often the legal entity

  creative_count  integer NOT NULL DEFAULT 0,  -- capped at the 40-per-page request size
  first_shown     timestamptz,          -- earliest first-shown across returned creatives
  last_shown      timestamptz,          -- latest last-shown; THE recency signal
  formats         text[],               -- distinct creative format codes seen

  -- Raw evidence: the creative ids and their date ranges, so a claim can be audited
  -- without re-querying. Deliberately not the full creative HTML, which is large and
  -- mostly ad-server markup.
  creatives       jsonb NOT NULL DEFAULT '[]'::jsonb,

  http_status     integer,              -- 0 when the request never completed
  fetch_error     text,
  checked_at      timestamptz NOT NULL DEFAULT now()
);

-- One row per (business, domain): a re-check updates in place, which is what makes the
-- pass resumable and lets `checked_at` drive a staleness-based refresh.
CREATE UNIQUE INDEX IF NOT EXISTS ads_transparency_business_domain_uidx
  ON leads.ads_transparency (business_id, domain);

CREATE INDEX IF NOT EXISTS ads_transparency_last_shown_idx
  ON leads.ads_transparency (last_shown DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ads_transparency_checked_idx
  ON leads.ads_transparency (checked_at DESC);

-- Rolled-up columns on businesses, derived from the table above.
--
-- ads_confirmed_active is deliberately NOT the same question as marketing_active. It is
-- true only when a creative was shown recently enough to mean the campaign is live (see
-- RECENT_DAYS in scripts/enrich-ads-transparency.mjs). A business with a pixel and no
-- creatives is running infrastructure, not ads; a business with creatives and no pixel
-- is advertising through a channel the crawler cannot see, which a 25-row sample showed
-- is common -- 9 of 19 confirmed advertisers had marketing_active false or null.
ALTER TABLE leads.businesses
  ADD COLUMN IF NOT EXISTS ads_confirmed_active boolean,
  ADD COLUMN IF NOT EXISTS ads_last_shown       timestamptz,
  ADD COLUMN IF NOT EXISTS ads_creative_count   integer;

COMMENT ON TABLE leads.ads_transparency IS
  'Google Ads Transparency evidence per business domain. The only confirmed-current-spend '
  'signal in this database: a returned creative is an ad Google is serving, with a '
  'last-shown date, unlike marketing_active which only proves a pixel is installed.';

COMMENT ON COLUMN leads.businesses.ads_confirmed_active IS
  'True when Ads Transparency returned a creative shown within the recency window. '
  'Distinct from marketing_active, which means only that a paid-media pixel is present.';
