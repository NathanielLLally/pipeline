-- Deduped businesses from yellow_pages.yellow_pages_loading.
--
-- yellow_pages_loading holds one row per (business, matched category) pair, because
-- the scraper writes a fresh row every time a business shows up under a different
-- category search. As of this migration that's 501,684 rows over only ~85,701 distinct
-- businesses identified by (name, address, city, state, zip) -- one listing repeated up
-- to ~199 times. Within a duplicate group, phone/website almost always agree (they
-- disagree in <1% of groups; we keep the first non-blank value seen, ordered by id),
-- but category legitimately varies -- 26,908 groups carry more than one category, which
-- is itself an ICP signal (e.g. a business tagged both dog-training and
-- pet-boarding-kennels), so categories are collected into an array rather than
-- collapsed to one.
--
-- zip is blank on 9,565 source rows (blank, not NULL -- the source columns are all
-- varchar with no NULL zips, but if that ever changes, NULL zip would break the
-- uniqueness of the natural key below since NULL <> NULL in a unique constraint). This
-- table defaults every key column to '' instead of NULL so the unique constraint holds
-- for every row, including ones with a missing zip.

CREATE TABLE IF NOT EXISTS yellow_pages.business (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  name        text NOT NULL,
  address     text NOT NULL DEFAULT '',
  city        text NOT NULL DEFAULT '',
  state       text NOT NULL DEFAULT '',
  zip         text NOT NULL DEFAULT '',

  phone       text NOT NULL DEFAULT '',
  website     text NOT NULL DEFAULT '',
  categories  text[] NOT NULL DEFAULT '{}',

  source_row_count integer NOT NULL DEFAULT 1,  -- how many yellow_pages_loading rows collapsed into this one
  min_source_id    integer,                     -- lowest yellow_pages_loading.id in the group, for tracing back

  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS business_natural_key_uidx
  ON yellow_pages.business (name, address, city, state, zip);

CREATE INDEX IF NOT EXISTS business_categories_gin_idx
  ON yellow_pages.business USING gin (categories);

COMMENT ON TABLE yellow_pages.business IS
  'One row per distinct business from yellow_pages.yellow_pages_loading, deduped on '
  '(name, address, city, state, zip). yellow_pages_loading has one row per '
  '(business, category) match, so the same business appears repeatedly there; this '
  'table collapses those into one row per business with all its categories collected '
  'into the categories array.';

-- Migrate: group the raw rows on the natural key, pick the first non-blank
-- phone/website in each group (ordered by id, so the choice is deterministic and
-- reproducible), and collect the distinct categories.
INSERT INTO yellow_pages.business
  (name, address, city, state, zip, phone, website, categories, source_row_count, min_source_id)
SELECT
  name,
  coalesce(address, ''),
  coalesce(city, ''),
  coalesce(state, ''),
  coalesce(zip, ''),
  coalesce((array_agg(phone ORDER BY id) FILTER (WHERE phone IS NOT NULL AND phone <> ''))[1], ''),
  coalesce((array_agg(website ORDER BY id) FILTER (WHERE website IS NOT NULL AND website <> ''))[1], ''),
  array_agg(DISTINCT category ORDER BY category) FILTER (WHERE category IS NOT NULL AND category <> ''),
  count(*),
  min(id)
FROM yellow_pages.yellow_pages_loading
GROUP BY name, coalesce(address, ''), coalesce(city, ''), coalesce(state, ''), coalesce(zip, '')
ON CONFLICT (name, address, city, state, zip) DO NOTHING;
