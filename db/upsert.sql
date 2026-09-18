-- Merges leads.staging_businesses (freshly loaded via \copy) into leads.businesses,
-- then logs one leads.search_log row per (geo_target, service_category) touched.
--
-- Expected psql variables (pass via -v):
--   batch_label  -- e.g. 'prompt1-la-dog_training-2026-09-11'
--   phase        -- e.g. 'prompt1'
--
-- Run AFTER: TRUNCATE leads.staging_businesses; \copy leads.staging_businesses FROM '<csv>' CSV HEADER;

\set ON_ERROR_STOP on
BEGIN;

-- Work on a temp copy so leads.staging_businesses's declared schema (and the explicit
-- \copy column list pipeline.sh uses to load it) never drifts between runs.
CREATE TEMP TABLE staging_work AS SELECT * FROM leads.staging_businesses;
ALTER TABLE staging_work ADD COLUMN dedup_key text;
ALTER TABLE staging_work ADD COLUMN matched_id uuid;

UPDATE staging_work
SET dedup_key = COALESCE(place_id, domain, phone_normalized, name_city_state_key);

-- Rows with no usable identifier at all can't be deduped or matched; drop them rather
-- than risk merging unrelated businesses together under a NULL key.
DELETE FROM staging_work WHERE dedup_key IS NULL;

-- Collapse intra-batch duplicates (same business surfaced by >1 query in this batch)
-- into one representative row per dedup_key, aggregating all their (query, geo_target,
-- service_category, phase) combos into a single sources[] payload for that business.
CREATE TEMP TABLE staging_sources AS
SELECT dedup_key,
       jsonb_agg(DISTINCT jsonb_build_object(
         'query', query,
         'geo_target', geo_target,
         'service_category', service_category,
         'phase', phase,
         'discovered_at', now()
       )) AS new_sources
FROM staging_work
GROUP BY dedup_key;

CREATE TEMP TABLE staging_dedup AS
SELECT DISTINCT ON (dedup_key) s.*
FROM staging_work s
ORDER BY dedup_key, (review_count IS NULL), review_count DESC NULLS LAST;

ALTER TABLE staging_dedup ADD PRIMARY KEY (dedup_key);

-- Priority waterfall match against existing businesses: place_id > domain > phone > name+city+state.
UPDATE staging_dedup s SET matched_id = b.id
FROM leads.businesses b
WHERE s.matched_id IS NULL AND s.place_id IS NOT NULL AND b.place_id = s.place_id;

UPDATE staging_dedup s SET matched_id = b.id
FROM leads.businesses b
WHERE s.matched_id IS NULL AND s.domain IS NOT NULL AND b.domain = s.domain;

UPDATE staging_dedup s SET matched_id = b.id
FROM leads.businesses b
WHERE s.matched_id IS NULL AND s.phone_normalized IS NOT NULL AND b.phone_normalized = s.phone_normalized;

UPDATE staging_dedup s SET matched_id = b.id
FROM leads.businesses b
WHERE s.matched_id IS NULL AND s.name_city_state_key IS NOT NULL AND b.name_city_state_key = s.name_city_state_key;

-- Update existing businesses: fresh scrape fields win when present, never clobber an
-- existing non-null value with a new null; sources[] grows, never shrinks or resets.
UPDATE leads.businesses b
SET
  place_id              = COALESCE(s.place_id, b.place_id),
  domain                = COALESCE(s.domain, b.domain),
  phone_normalized      = COALESCE(s.phone_normalized, b.phone_normalized),
  name_city_state_key   = COALESCE(s.name_city_state_key, b.name_city_state_key),
  name                  = COALESCE(s.name, b.name),
  maps_url              = COALESCE(s.maps_url, b.maps_url),
  website               = COALESCE(s.website, b.website),
  phone                 = COALESCE(s.phone, b.phone),
  address               = COALESCE(s.address, b.address),
  city                  = COALESCE(s.city, b.city),
  state                 = COALESCE(s.state, b.state),
  zip                   = COALESCE(s.zip, b.zip),
  latitude              = COALESCE(s.latitude, b.latitude),
  longitude             = COALESCE(s.longitude, b.longitude),
  rating                = COALESCE(s.rating, b.rating),
  review_count          = COALESCE(s.review_count, b.review_count),
  primary_category      = COALESCE(s.primary_category, b.primary_category),
  additional_categories = COALESCE(string_to_array(s.additional_categories, '|'), b.additional_categories),
  description           = COALESCE(NULLIF(s.description, ''), b.description),
  hours                 = COALESCE(s.hours::jsonb, b.hours),
  price_range           = COALESCE(NULLIF(s.price_range, ''), b.price_range),
  status                = COALESCE(NULLIF(s.status, ''), b.status),
  icp_score             = s.icp_score,
  icp_tier              = s.icp_tier,
  qc_status             = s.qc_status::leads.qc_status,
  raw_scrape            = s.raw_scrape::jsonb,
  sources               = b.sources || ss.new_sources,
  date_updated          = now()
FROM staging_dedup s
JOIN staging_sources ss ON ss.dedup_key = s.dedup_key
WHERE b.id = s.matched_id;

-- Insert brand-new businesses.
INSERT INTO leads.businesses (
  place_id, domain, phone_normalized, name_city_state_key,
  name, maps_url, website, phone, address, city, state, zip,
  latitude, longitude, rating, review_count, primary_category, additional_categories,
  description, hours, price_range, status,
  service_category, icp_score, icp_tier, qc_status,
  sources, raw_scrape
)
SELECT
  s.place_id, s.domain, s.phone_normalized, s.name_city_state_key,
  s.name, s.maps_url, s.website, s.phone, s.address, s.city, s.state, s.zip,
  s.latitude, s.longitude, s.rating, s.review_count, s.primary_category,
  string_to_array(s.additional_categories, '|'),
  NULLIF(s.description, ''), s.hours::jsonb, NULLIF(s.price_range, ''), NULLIF(s.status, ''),
  s.service_category, s.icp_score, s.icp_tier, s.qc_status::leads.qc_status,
  ss.new_sources, s.raw_scrape::jsonb
FROM staging_dedup s
JOIN staging_sources ss ON ss.dedup_key = s.dedup_key
WHERE s.matched_id IS NULL;

-- Log this batch: one row per (geo_target, service_category) touched. results_discovered
-- counts raw (pre-dedup) staging rows for that geo/category; new/duplicates/qualified/
-- rejected count the deduped representative rows attributed to it via staging_sources.
INSERT INTO leads.search_log (query_batch_label, query, geo_target, service_category, phase, searched_at,
                               results_discovered, new_businesses, duplicates, qualified, rejected)
SELECT
  :'batch_label',
  raw.query,
  raw.geo_target,
  raw.service_category,
  :'phase',
  now(),
  raw.results_discovered,
  COALESCE(dd.new_businesses, 0),
  COALESCE(dd.duplicates, 0),
  COALESCE(dd.qualified, 0),
  COALESCE(dd.rejected, 0)
FROM (
  SELECT query, geo_target, service_category, count(*) AS results_discovered
  FROM staging_work
  GROUP BY query, geo_target, service_category
) raw
LEFT JOIN (
  SELECT
    src.value->>'query' AS query,
    src.value->>'geo_target' AS geo_target,
    src.value->>'service_category' AS service_category,
    count(DISTINCT sd.dedup_key) FILTER (WHERE sd.matched_id IS NULL) AS new_businesses,
    count(DISTINCT sd.dedup_key) FILTER (WHERE sd.matched_id IS NOT NULL) AS duplicates,
    count(DISTINCT sd.dedup_key) FILTER (WHERE sd.qc_status <> 'REJECTED') AS qualified,
    count(DISTINCT sd.dedup_key) FILTER (WHERE sd.qc_status = 'REJECTED') AS rejected
  FROM staging_dedup sd
  JOIN staging_sources ss ON ss.dedup_key = sd.dedup_key
  CROSS JOIN LATERAL jsonb_array_elements(ss.new_sources) AS src(value)
  GROUP BY 1, 2, 3
) dd ON dd.query IS NOT DISTINCT FROM raw.query
    AND dd.geo_target = raw.geo_target
    AND dd.service_category = raw.service_category;

COMMIT;
