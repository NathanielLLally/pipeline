-- Phase B (Prompt 7): website crawl evidence.
--
-- Kept in its own table rather than as more columns on businesses for three reasons:
-- a crawl is per-URL while a business may have several pages worth fetching; a re-run
-- needs to know what was already fetched and when, so the pass can be incremental; and
-- the extracted page text has to be readable later without re-fetching the site.
--
-- Nothing here is authoritative about a business. It is raw evidence with a timestamp
-- and an HTTP status attached, so any claim made from it can be traced back to the
-- page and the moment it was observed.

CREATE TABLE IF NOT EXISTS leads.website_crawl (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  url            text NOT NULL,        -- the URL actually requested
  final_url      text,                 -- after redirects; differs when a site moved
  page_kind      text NOT NULL,        -- home | about | team | contact | pricing | services
  http_status    integer,              -- 0 when the request never completed
  fetch_error    text,                 -- timeout, DNS failure, TLS error...
  content_bytes  integer,

  -- Extracted evidence. signals is the matched-token detail behind the booleans, so a
  -- detection can be audited instead of trusted.
  signals        jsonb NOT NULL DEFAULT '{}'::jsonb,
  text_excerpt   text,                 -- stripped page text, capped; feeds name extraction

  fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per (business, url): a re-crawl updates in place rather than accumulating
-- duplicates, which is what makes the pass resumable.
CREATE UNIQUE INDEX IF NOT EXISTS website_crawl_business_url_uidx
  ON leads.website_crawl (business_id, url);

CREATE INDEX IF NOT EXISTS website_crawl_business_idx ON leads.website_crawl (business_id);
CREATE INDEX IF NOT EXISTS website_crawl_fetched_idx  ON leads.website_crawl (fetched_at DESC);

COMMENT ON TABLE leads.website_crawl IS
  'Per-URL crawl evidence for Prompt 7 marketing signals and Prompt 9 decision-maker '
  'extraction. Append/update per (business_id, url); never treated as authoritative '
  'without the http_status and fetched_at that accompany it.';
