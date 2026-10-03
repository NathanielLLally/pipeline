-- A development-only mirror of leads.website_crawl for the Jina crawler.
--
-- flow/jina_crawl.py fetches the same business websites through r.jina.ai
-- instead of curl-through-SOCKS5, so its output is not comparable row-for-row
-- with what scripts/enrich-websites.mjs stored: Jina returns rendered markdown
-- from a headless browser, where the mjs crawler returns raw HTML stripped of
-- tags. Writing both into one table would make text_excerpt mean two different
-- things and silently change what every downstream reader
-- (extract-emails.mjs, enrich-decision-makers.mjs, flow/agents/selector.py)
-- is quoting from. Hence a separate table while the approach is evaluated.
--
-- Differences from leads.website_crawl, both deliberate:
--
--   no content_bytes -- Jina answers with JSON, not the page's own bytes, so
--     the number would measure Jina's envelope and not the site.
--   signals is Jina's own response object (title, description, links,
--     metadata, usage) minus `content`, which is stored once in text_excerpt.
--     It is kept whole because `links` carries mailto: addresses that the
--     markdown body does not, and usage.tokens is how the API budget is
--     accounted for.

CREATE TABLE IF NOT EXISTS leads.website_crawl_jina (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id    uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  url            text NOT NULL,        -- the URL actually requested
  final_url      text,                 -- data.url, after Jina followed redirects
  page_kind      text NOT NULL,        -- home | about | contact | pricing
  http_status    integer,              -- data.httpStatus: the SITE's status, 0 when unknown
  fetch_error    text,                 -- Jina error envelope, empty content, transport failure

  signals        jsonb NOT NULL DEFAULT '{}'::jsonb,
  text_excerpt   text,                 -- data.content: rendered markdown, uncapped

  fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- One row per (business, url), exactly as in website_crawl: this is what makes
-- a re-run update in place rather than accumulate, and so resumable.
CREATE UNIQUE INDEX IF NOT EXISTS website_crawl_jina_business_url_uidx
  ON leads.website_crawl_jina (business_id, url);

CREATE INDEX IF NOT EXISTS website_crawl_jina_business_idx
  ON leads.website_crawl_jina (business_id);
CREATE INDEX IF NOT EXISTS website_crawl_jina_fetched_idx
  ON leads.website_crawl_jina (fetched_at DESC);

COMMENT ON TABLE leads.website_crawl_jina IS
  'Development mirror of leads.website_crawl written by flow/jina_crawl.py via '
  'r.jina.ai. Rendered markdown, not stripped HTML -- kept separate so '
  'text_excerpt keeps one meaning per table. No content_bytes: the response is '
  'JSON, so a byte count would measure the API envelope, not the site.';
