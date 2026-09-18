-- QC pass support (Prompt 8).
--
-- Two additions, both additive -- no column is dropped and no row is deleted here.
--
-- 1. businesses.state_source records HOW a state value was arrived at, because this
--    pass fills 520 empty states by inference rather than observation. CLAUDE.md says
--    not to fabricate missing fields; recording provenance is what makes filling them
--    legitimate instead of fabrication. Downstream consumers that need only
--    ground-truth geography can filter on state_source = 'scraped'.
--
-- 2. leads.qc_review holds flagged duplicate candidates for human adjudication.
--    Deliberately NOT an auto-merge: see the comment on the table.
--
-- Idempotent -- safe to re-run.

\set ON_ERROR_STOP on
BEGIN;

ALTER TABLE leads.businesses
  ADD COLUMN IF NOT EXISTS state_source text;

COMMENT ON COLUMN leads.businesses.state_source IS
  'How state was determined: ''scraped'' (from the Google record) or '
  '''inferred_from_geo_target'' (service-area business with no street address; the '
  'state comes from the search geo that found it, which agrees with the scraped state '
  '97.3% of the time on rows where both exist). NULL means state is still unknown.';

-- Duplicate candidates, for review rather than automatic merging.
--
-- An earlier plan would have merged on matching domain or phone. That is wrong for
-- this dataset: of 380 duplicate-domain pairs only 30 share a name key and 246 are in
-- different cities outright. stores.petco.com covers 12 stores in 11 cities;
-- puppyhaven.com is 5 branches with 5 distinct place_ids. Those are franchise
-- locations -- each a separate prospect with its own owner, phone and lead value --
-- and merging by domain would have destroyed 230 legitimate businesses. Phone is no
-- better: 176 pairs share a number, only 17 share a name key.
--
-- Even the narrow name-key matches are not reliably duplicates ("Rapawzel Dog
-- Grooming" has a Brooklyn and a Manhattan location; "Abbie's Dog House on Maple" and
-- "on Belmont" are two real Dallas shops), so nothing here is resolved by machine.
CREATE TABLE IF NOT EXISTS leads.qc_review (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL,          -- what rule flagged it, e.g. 'dup_candidate'
  reason        text NOT NULL,          -- human-readable evidence for the flag
  business_ids  uuid[] NOT NULL,        -- the rows involved
  details       jsonb NOT NULL DEFAULT '{}'::jsonb,
  resolution    text,                   -- NULL = unreviewed; else 'merged'/'distinct'/'ignored'
  resolved_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- One open flag per rule per set of rows; re-running the pass must not pile up copies.
CREATE UNIQUE INDEX IF NOT EXISTS qc_review_open_uidx
  ON leads.qc_review (kind, business_ids) WHERE resolution IS NULL;

COMMIT;
