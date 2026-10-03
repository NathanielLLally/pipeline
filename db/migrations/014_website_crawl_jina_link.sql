-- Links extracted from Jina's response `data.links` map.
--
-- One row per link text/href pair from a crawled page. Keyed on
-- (business_id, url, link_href) so a re-crawl of the same page does not
-- duplicate links already recorded. The conflict strategy is DO NOTHING
-- rather than DO UPDATE because links are immutable: if the same link appears
-- on the same page in two crawls, the second crawl adds nothing new.

CREATE TABLE IF NOT EXISTS leads.website_crawl_jina_link (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  url            text NOT NULL,        -- the crawled page's URL
  link_text      text,                 -- display text from data.links key (may be empty string)
  link_href      text NOT NULL,        -- target from data.links value (may be mailto, http, tel, etc.)

  fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per (business, page, link destination): a re-crawl of the same page
-- with the same link does not duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS website_crawl_jina_link_business_url_href_uidx
  ON leads.website_crawl_jina_link (business_id, url, link_href);

CREATE INDEX IF NOT EXISTS website_crawl_jina_link_business_idx
  ON leads.website_crawl_jina_link (business_id);
CREATE INDEX IF NOT EXISTS website_crawl_jina_link_href_idx
  ON leads.website_crawl_jina_link (link_href);

COMMENT ON TABLE leads.website_crawl_jina_link IS
  'Links from Jina crawl responses, keyed on (business_id, url, link_href). '
  'Supports discovery of email, phone, and social media links from rendered pages.';
