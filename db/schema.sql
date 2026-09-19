-- Lead-sourcing campaign schema. Lives in its own schema so it never collides
-- with whatever tables the gosom/google-maps-scraper job-queue mode creates
-- in `public` when run with -dsn.

CREATE SCHEMA IF NOT EXISTS leads;

CREATE TYPE leads.qc_status AS ENUM (
  'VALID',
  'NEEDS_ENRICHMENT',
  'LOW_PRIORITY',
  'REJECTED',
  'CLOSED'
);

CREATE TABLE IF NOT EXISTS leads.businesses (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- dedup identity
  place_id                  text,
  domain                    text,
  phone_normalized          text,
  name_city_state_key       text,

  -- Google Maps fields (never fabricated; null when the scraper didn't capture it)
  name                      text NOT NULL,
  maps_url                  text,
  website                   text,
  phone                     text,
  address                   text,
  city                      text,
  state                     text,
  zip                       text,
  latitude                  double precision,
  longitude                 double precision,
  rating                    numeric,
  review_count              integer,
  primary_category          text,
  additional_categories     text[],
  description               text,
  hours                     jsonb,
  price_range               text,
  -- As scraped. Note this is NOT an open/closed flag despite the name: Google returns
  -- a one-line editorial blurb here ("Casual, kid-friendly American brewpub", "$227").
  -- Exactly 1 of 3,686 rows contains the word "closed", so there is no closure signal
  -- in this feed and qc_status = 'CLOSED' has nothing to key off.
  status                    text,
  state_source              text,               -- 'scraped' | 'inferred_from_geo_target' (see 002_qc_pass.sql)

  -- campaign fields
  service_category          text NOT NULL,       -- dog_training | daycare_boarding | grooming | dog_walking_petsitting
  icp_score                 integer NOT NULL DEFAULT 0,
  icp_tier                  text,                -- Tier 1..4
  qc_status                 leads.qc_status NOT NULL DEFAULT 'NEEDS_ENRICHMENT',

  -- Prompt 6: growth signals
  growth_signal             text,
  growth_score              integer,

  -- Prompt 7: marketing signals
  marketing_active          boolean,
  google_ads_signal         boolean,
  meta_ads_signal           boolean,
  lead_form_present         boolean,
  booking_present           boolean,
  marketing_evidence        text,
  marketing_score           integer,

  -- Prompt 9: decision maker enrichment
  decision_maker_name       text,
  decision_maker_title      text,
  decision_maker_source     text,
  decision_maker_confidence text,

  -- provenance (append-only; never erase discovery history per Prompt 8)
  sources                   jsonb NOT NULL DEFAULT '[]'::jsonb,
  raw_scrape                jsonb,

  date_discovered           timestamptz NOT NULL DEFAULT now(),
  date_updated              timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS businesses_place_id_uidx
  ON leads.businesses (place_id) WHERE place_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS businesses_domain_idx ON leads.businesses (domain) WHERE domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS businesses_phone_idx ON leads.businesses (phone_normalized) WHERE phone_normalized IS NOT NULL;
CREATE INDEX IF NOT EXISTS businesses_namekey_idx ON leads.businesses (name_city_state_key) WHERE name_city_state_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS businesses_service_category_idx ON leads.businesses (service_category);
CREATE INDEX IF NOT EXISTS businesses_qc_status_idx ON leads.businesses (qc_status);
CREATE INDEX IF NOT EXISTS businesses_icp_score_idx ON leads.businesses (icp_score DESC);

CREATE TABLE IF NOT EXISTS leads.search_log (
  id                         bigserial PRIMARY KEY,
  query_batch_label          text NOT NULL,
  query                      text,   -- the exact keyword searched; NULL only on pre-per-term rows
  geo_target                 text NOT NULL,
  service_category           text NOT NULL,
  phase                      text,
  searched_at                timestamptz NOT NULL DEFAULT now(),
  results_discovered         integer NOT NULL DEFAULT 0,
  new_businesses             integer NOT NULL DEFAULT 0,
  duplicates                 integer NOT NULL DEFAULT 0,
  qualified                  integer NOT NULL DEFAULT 0,
  rejected                   integer NOT NULL DEFAULT 0,
  results_watermark_start    text,   -- public.results id/timestamp range ingested, for traceability
  results_watermark_end      text
);

CREATE INDEX IF NOT EXISTS search_log_service_geo_idx ON leads.search_log (service_category, geo_target);

-- Coverage lookups in gen-queries.mjs are keyed on (service_category, geo_target, query):
-- a geo is only "covered" for the specific terms already run against it.
CREATE INDEX IF NOT EXISTS search_log_coverage_idx
  ON leads.search_log (service_category, geo_target, query);

-- Scratch table for the load step. Truncated and reloaded every batch via \copy.
-- Same shape as the ingest-relevant subset of leads.businesses (no merge-only
-- provenance/timestamp columns -- those are computed during the upsert).
CREATE TABLE IF NOT EXISTS leads.staging_businesses (
  place_id                  text,
  domain                    text,
  phone_normalized          text,
  name_city_state_key       text,
  name                      text,
  maps_url                  text,
  website                   text,
  phone                     text,
  address                   text,
  city                      text,
  state                     text,
  zip                       text,
  latitude                  double precision,
  longitude                 double precision,
  rating                    numeric,
  review_count              integer,
  primary_category          text,
  additional_categories     text,   -- pipe-delimited in CSV, split on load
  description               text,
  hours                     text,   -- raw JSON text in CSV, cast to jsonb on load
  price_range               text,
  status                    text,
  service_category          text,
  icp_score                 integer,
  icp_tier                  text,
  qc_status                 text,
  query                     text,   -- this batch's query metadata, used to build the sources[] entry
  geo_target                text,
  phase                     text,
  raw_scrape                text    -- raw JSON text in CSV, cast to jsonb on load
);

-- Worker health monitoring: audit trail, liveness signals, and action history.
-- One row per tick (~60s). Hysteresis and cooldown state derived from this table
-- (no local state file; survives reinstall of the timer).
CREATE TABLE IF NOT EXISTS leads.worker_health_log (
  id           bigserial PRIMARY KEY,
  checked_at   timestamptz NOT NULL DEFAULT now(),
  -- IDLE|HEALTHY|PROXY_DEGRADED|WEDGED|STALLED|HOST_UNREACHABLE|WORKER_DEGRADED|FLEET_DOWN
  -- WORKER_DEGRADED/FLEET_DOWN come from per-host container probes. The queue-level
  -- metrics cannot see a dead worker: the survivors keep draining the backlog, so a
  -- partial outage reads as HEALTHY until someone notices jobs timing out.
  verdict      text NOT NULL,
  signals      jsonb NOT NULL,         -- all metrics: backlog, last_progress, longest_running, zero_yield_ratio, recent_timeouts
  action       text,                   -- null|restart|proxy_refresh|alert
  action_ok    boolean,
  action_note  text
);

CREATE INDEX IF NOT EXISTS worker_health_log_checked_at_idx
  ON leads.worker_health_log (checked_at DESC);

-- Duplicate candidates and other QC flags awaiting human adjudication.
-- Nothing here is resolved by machine: see the rationale in
-- db/migrations/002_qc_pass.sql for why matching domains/phones are NOT merged
-- automatically (franchise branches share both, and they are separate prospects).
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

CREATE UNIQUE INDEX IF NOT EXISTS qc_review_open_uidx
  ON leads.qc_review (kind, business_ids) WHERE resolution IS NULL;
