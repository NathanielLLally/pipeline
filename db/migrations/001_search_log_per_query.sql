-- Per-term search coverage.
--
-- Before this migration leads.search_log recorded one row per (geo_target,
-- service_category) batch, so gen-queries.mjs could only ask "has this geo been
-- searched at all?". That made every already-touched geo permanently ineligible:
-- prompt2/prompt3 for dog_training generated zero queries even though only the
-- prompt1 terms had ever run there.
--
-- After: one row per (query, geo_target, service_category), and coverage is keyed
-- on the exact keyword.
--
-- Idempotent -- safe to re-run.

\set ON_ERROR_STOP on
BEGIN;

ALTER TABLE leads.search_log ADD COLUMN IF NOT EXISTS query text;

CREATE INDEX IF NOT EXISTS search_log_coverage_idx
  ON leads.search_log (service_category, geo_target, query);

-- Reconstruct per-query history from businesses.sources, which has carried
-- {query, geo_target, service_category, phase} per discovery since day one.
--
-- These rows are provenance, not measurement: results_discovered is the number of
-- surviving deduplicated businesses still attributable to that query today, which
-- is a lower bound on what the query actually returned. new/duplicates/qualified/
-- rejected stay 0 -- those were only ever computed at batch time and cannot be
-- recovered after the fact. The 'backfill-from-sources' label marks them as such.
--
-- Caveat: a query that returned zero usable results left no source entry, so it is
-- not represented here and will be re-run. That is the safe direction to err.
INSERT INTO leads.search_log
  (query_batch_label, query, geo_target, service_category, phase, searched_at,
   results_discovered, new_businesses, duplicates, qualified, rejected)
SELECT
  'backfill-from-sources',
  s.query,
  s.geo_target,
  s.service_category,
  s.phase,
  s.first_seen,
  s.n,
  0, 0, 0, 0
FROM (
  SELECT
    src.value->>'query'            AS query,
    src.value->>'geo_target'       AS geo_target,
    src.value->>'service_category' AS service_category,
    src.value->>'phase'            AS phase,
    min((src.value->>'discovered_at')::timestamptz) AS first_seen,
    count(DISTINCT b.id)           AS n
  FROM leads.businesses b
  CROSS JOIN LATERAL jsonb_array_elements(b.sources) AS src(value)
  WHERE src.value->>'query' IS NOT NULL
    AND src.value->>'geo_target' IS NOT NULL
    AND src.value->>'service_category' IS NOT NULL
  GROUP BY 1, 2, 3, 4
) s
WHERE NOT EXISTS (
  SELECT 1 FROM leads.search_log sl
  WHERE sl.query = s.query
    AND sl.geo_target = s.geo_target
    AND sl.service_category = s.service_category
);

COMMIT;

-- The pre-existing per-geo rows keep query IS NULL. They stay for their batch
-- statistics, but gen-queries.mjs ignores NULL-query rows when computing coverage
-- -- otherwise they would re-impose exactly the blanket skip this migration removes.
