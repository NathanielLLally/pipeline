-- Flag crawls whose stored text was truncated, so they can be fetched again.
--
-- scripts/enrich-websites.mjs stored page text as
-- `toText(body).slice(0, EXCERPT_CHARS)` with EXCERPT_CHARS = 20000. Any row
-- whose text_excerpt is exactly 20000 characters long therefore hit that
-- ceiling and is missing everything past it. Measured 2026-10-03: 2030 rows
-- across 1693 businesses, out of 60124 crawls carrying text.
--
-- The ceiling has been removed from the crawler, so new crawls store the whole
-- page. This migration marks the already-damaged rows. It cannot repair them:
-- the discarded characters were never stored, so the pages must be refetched.
--
-- Detection is by exact length rather than by a stored flag because no flag
-- existed when the truncation happened. A page whose genuine length is exactly
-- 20000 characters would be flagged as a false positive; refetching it is
-- harmless, so that trade is deliberate.
--
-- needs_recrawl stays in the schema after this backfill: it is the general
-- mechanism for asking the crawler to revisit a URL, whatever the reason.

alter table leads.website_crawl
    add column if not exists needs_recrawl boolean not null default false;

alter table leads.website_crawl
    add column if not exists recrawl_reason text;

comment on column leads.website_crawl.needs_recrawl is
    'Stored text is known-incomplete or stale; the crawler should refetch this url.';
comment on column leads.website_crawl.recrawl_reason is
    'Why this row was flagged, so a later reader need not guess.';

update leads.website_crawl
   set needs_recrawl = true,
       recrawl_reason = 'text_excerpt truncated at the former 20000-char EXCERPT_CHARS ceiling'
 where length(text_excerpt) = 20000
   and needs_recrawl = false;

-- Partial index: the flagged set is small and is always queried as "what still
-- needs recrawling", so indexing only the true rows keeps it tiny.
create index if not exists website_crawl_needs_recrawl_idx
    on leads.website_crawl (business_id)
 where needs_recrawl;
