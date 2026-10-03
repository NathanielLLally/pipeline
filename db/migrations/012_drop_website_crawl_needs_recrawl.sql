-- Revert 011. The needs_recrawl/recrawl_reason columns were the wrong mechanism.
--
-- 011 recorded "this row needs refetching" as stored state. It did not need to
-- be stored: scripts/enrich-websites.mjs already decides what to skip with
--
--   SELECT business_id, url FROM leads.website_crawl WHERE text_excerpt IS NOT NULL
--
-- keyed, as its own comment says, on having usable content rather than on HTTP
-- status. Text cut at the former 20000-char ceiling is not complete content, so
-- the fix belongs in that predicate -- one clause, derived from data already
-- present, with nothing to backfill, index, keep accurate or clean up.
--
-- The predicate is bounded by fetched_at so that it cannot match crawls made
-- after the ceiling was removed; otherwise a page whose genuine length is
-- exactly 20000 characters would be refetched on every pass forever.

drop index if exists leads.website_crawl_needs_recrawl_idx;

alter table leads.website_crawl drop column if exists needs_recrawl;
alter table leads.website_crawl drop column if exists recrawl_reason;
