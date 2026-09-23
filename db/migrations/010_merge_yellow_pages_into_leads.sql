-- Merge Yellow Pages businesses into leads.businesses with paid-marketing signals.
--
-- Yellow Pages presence signals commercial paid listing and marketing investment.
-- This migration:
-- 1. Normalizes YP phone/name+city+state keys for dedup matching
-- 2. Maps YP's 12 dog/pet categories to ICP's 4 service_category buckets
-- 3. Deduplicates against existing leads.businesses using waterfall: phone > name+city+state
-- 4. Inserts new YP businesses with YP marketing signal baked into initial icp_score
-- 5. Updates matched businesses to mark YP presence in sources + marketing_evidence

-- Step 1: Add normalized keys to yellow_pages.business if not present.
ALTER TABLE yellow_pages.business
  ADD COLUMN IF NOT EXISTS phone_normalized text,
  ADD COLUMN IF NOT EXISTS name_city_state_key text;

-- Normalize phone: extract digits, strip leading 1 (US country code), keep if >= 7 digits.
UPDATE yellow_pages.business
SET phone_normalized = (
  SELECT CASE
    WHEN d IS NULL OR d = '' THEN NULL
    WHEN length(d) = 11 AND d LIKE '1%' THEN
      CASE WHEN length(substring(d, 2)) >= 7 THEN substring(d, 2) ELSE NULL END
    WHEN length(d) >= 7 THEN d
    ELSE NULL
  END
  FROM (SELECT regexp_replace(phone, '[^\d]', '', 'g') as d) _
)
WHERE phone_normalized IS NULL AND phone IS NOT NULL AND phone <> '';

-- Normalize name+city+state key: lowercase, spaces to underscores, pipe-separated.
UPDATE yellow_pages.business
SET name_city_state_key = lower(
  regexp_replace(name, '\s+', '_', 'g')
  || '|'
  || regexp_replace(coalesce(city, ''), '\s+', '_', 'g')
  || '|'
  || regexp_replace(coalesce(state, ''), '\s+', '_', 'g')
)
WHERE name_city_state_key IS NULL AND name IS NOT NULL AND name <> '';

-- Step 2: Map YP categories to ICP service_category.
-- Priority: dog_training > daycare_boarding > grooming > dog_walking_petsitting > off-ICP.
-- Store the mapping in a temp view for later use.
CREATE TEMPORARY TABLE yp_mapped_categories AS
SELECT
  id,
  name,
  categories,
  CASE
    WHEN categories @> ARRAY['dog-training']::text[] THEN 'dog_training'
    WHEN categories @> ARRAY['dog-day-care']::text[] THEN 'daycare_boarding'
    WHEN categories @> ARRAY['pet-boarding-kennels']::text[] THEN 'daycare_boarding'
    WHEN categories @> ARRAY['kennels']::text[] THEN 'daycare_boarding'
    WHEN categories @> ARRAY['mobile-pet-grooming']::text[] THEN 'grooming'
    WHEN categories @> ARRAY['pet-grooming']::text[] THEN 'grooming'
    ELSE NULL
  END AS service_category
FROM yellow_pages.business
WHERE id IS NOT NULL;

-- Step 3: Build a dedup match table: for each YP business, find if it already exists in leads.businesses.
-- Use phone_normalized first, then name_city_state_key as fallback.
CREATE TEMPORARY TABLE yp_dedup_candidates AS
SELECT
  yp.id as yp_id,
  -- Try phone match first (most reliable)
  CASE
    WHEN yp.phone_normalized IS NOT NULL THEN
      (SELECT lb.id FROM leads.businesses lb
       WHERE lb.phone_normalized = yp.phone_normalized
       ORDER BY lb.date_discovered DESC
       LIMIT 1)
    ELSE NULL
  END as matched_by_phone,
  -- Fall back to name+city+state if no phone match
  CASE
    WHEN yp.phone_normalized IS NULL AND yp.name_city_state_key IS NOT NULL THEN
      (SELECT lb.id FROM leads.businesses lb
       WHERE lb.name_city_state_key = yp.name_city_state_key
       ORDER BY lb.date_discovered DESC
       LIMIT 1)
    ELSE NULL
  END as matched_by_name_key,
  COALESCE(
    CASE WHEN yp.phone_normalized IS NOT NULL THEN
      (SELECT lb.id FROM leads.businesses lb
       WHERE lb.phone_normalized = yp.phone_normalized
       ORDER BY lb.date_discovered DESC
       LIMIT 1)
    ELSE NULL
    END,
    CASE WHEN yp.phone_normalized IS NULL AND yp.name_city_state_key IS NOT NULL THEN
      (SELECT lb.id FROM leads.businesses lb
       WHERE lb.name_city_state_key = yp.name_city_state_key
       ORDER BY lb.date_discovered DESC
       LIMIT 1)
    ELSE NULL
    END
  ) as matched_leads_id
FROM yellow_pages.business yp;

-- Step 4: Insert new YP businesses (those without a match in leads.businesses).
-- Initial score computation: base score from service_category + YP signal (+7) + website bonus (+4 if has website, -20 if no website and no reviews).
INSERT INTO leads.businesses (
  name, address, city, state, zip,
  website, phone, phone_normalized, name_city_state_key,
  primary_category, additional_categories,
  service_category, icp_score, icp_tier, qc_status,
  marketing_evidence, sources
)
SELECT
  yp.name,
  yp.address,
  yp.city,
  yp.state,
  yp.zip,
  NULLIF(yp.website, ''),
  NULLIF(yp.phone, ''),
  yp.phone_normalized,
  yp.name_city_state_key,
  -- Primary category: best guess from YP categories
  CASE
    WHEN yp.categories @> ARRAY['dog-training']::text[] THEN 'dog trainer'
    WHEN yp.categories @> ARRAY['dog-day-care','pet-boarding-kennels','kennels']::text[] THEN 'pet boarding service'
    WHEN yp.categories @> ARRAY['mobile-pet-grooming','pet-grooming']::text[] THEN 'pet groomer'
    ELSE 'pet care service'
  END,
  yp.categories,
  -- Service category (from the mapped table)
  m.service_category,
  -- ICP score: base by category + YP signal (+7) + website bonus/penalty
  LEAST(100, GREATEST(0,
    COALESCE(
      CASE m.service_category
        WHEN 'dog_training' THEN 55
        WHEN 'daycare_boarding' THEN 45
        WHEN 'grooming' THEN 35
        WHEN 'dog_walking_petsitting' THEN 25
        ELSE 0
      END,
      0
    )
    + 7  -- YP paid marketing signal
    + CASE WHEN yp.website <> '' THEN 4 ELSE -20 END  -- Website bonus or no-listings penalty
  )),
  -- Tier based on score
  CASE
    WHEN m.service_category IS NULL THEN 'Tier 4'
    WHEN LEAST(100, GREATEST(0,
      COALESCE(
        CASE m.service_category
          WHEN 'dog_training' THEN 55
          WHEN 'daycare_boarding' THEN 45
          WHEN 'grooming' THEN 35
          WHEN 'dog_walking_petsitting' THEN 25
          ELSE 0
        END,
        0
      )
      + 7
      + CASE WHEN yp.website <> '' THEN 4 ELSE -20 END
    )) >= 70 THEN 'Tier 1'
    WHEN LEAST(100, GREATEST(0,
      COALESCE(
        CASE m.service_category
          WHEN 'dog_training' THEN 55
          WHEN 'daycare_boarding' THEN 45
          WHEN 'grooming' THEN 35
          WHEN 'dog_walking_petsitting' THEN 25
          ELSE 0
        END,
        0
      )
      + 7
      + CASE WHEN yp.website <> '' THEN 4 ELSE -20 END
    )) >= 50 THEN 'Tier 2'
    WHEN LEAST(100, GREATEST(0,
      COALESCE(
        CASE m.service_category
          WHEN 'dog_training' THEN 55
          WHEN 'daycare_boarding' THEN 45
          WHEN 'grooming' THEN 35
          WHEN 'dog_walking_petsitting' THEN 25
          ELSE 0
        END,
        0
      )
      + 7
      + CASE WHEN yp.website <> '' THEN 4 ELSE -20 END
    )) >= 30 THEN 'Tier 3'
    ELSE 'Tier 4'
  END,
  -- QC status (cast to enum)
  CASE
    WHEN m.service_category IS NULL THEN 'REJECTED'::leads.qc_status
    WHEN yp.website = '' THEN 'NEEDS_ENRICHMENT'::leads.qc_status
    ELSE 'VALID'::leads.qc_status
  END,
  -- Marketing evidence
  'yellow_pages_paid_listing: ' || array_length(yp.categories, 1) || ' categories',
  -- Sources
  jsonb_build_array(jsonb_build_object(
    'source', 'yellow_pages',
    'name', yp.name,
    'city', yp.city,
    'state', yp.state,
    'categories', yp.categories,
    'date_discovered', now()
  ))
FROM yellow_pages.business yp
JOIN yp_mapped_categories m ON m.id = yp.id
LEFT JOIN yp_dedup_candidates dc ON dc.yp_id = yp.id
WHERE dc.matched_leads_id IS NULL  -- Only new businesses
  AND m.service_category IS NOT NULL  -- Only on-ICP
;

-- Step 5: Update matched YP businesses in leads.businesses.
-- Add YP to sources, mark marketing_evidence, merge categories, and rescore (+7).
UPDATE leads.businesses lb
SET
  sources = CASE
    WHEN NOT (sources @> jsonb_build_array(jsonb_build_object('source', 'yellow_pages'))) THEN
      sources || jsonb_build_array(jsonb_build_object(
        'source', 'yellow_pages',
        'name', yp.name,
        'city', yp.city,
        'state', yp.state,
        'categories', yp.categories,
        'date_discovered', now()
      ))
    ELSE sources
  END,
  marketing_evidence = CASE
    WHEN marketing_evidence IS NULL THEN 'yellow_pages_paid_listing'
    WHEN NOT marketing_evidence LIKE '%yellow_pages%' THEN marketing_evidence || '; yellow_pages_paid_listing'
    ELSE marketing_evidence
  END,
  additional_categories = CASE
    WHEN additional_categories IS NULL THEN yp.categories
    ELSE (
      SELECT array_agg(DISTINCT cat ORDER BY cat)
      FROM (
        SELECT unnest(additional_categories) as cat
        UNION ALL
        SELECT unnest(yp.categories)
      ) _
    )
  END,
  icp_score = LEAST(100, icp_score + 7),
  icp_tier = CASE
    WHEN (icp_score + 7) >= 70 THEN 'Tier 1'
    WHEN (icp_score + 7) >= 50 THEN 'Tier 2'
    WHEN (icp_score + 7) >= 30 THEN 'Tier 3'
    ELSE 'Tier 4'
  END,
  date_updated = now()
FROM yellow_pages.business yp
JOIN yp_dedup_candidates dc ON dc.yp_id = yp.id
WHERE lb.id = dc.matched_leads_id
  AND dc.matched_leads_id IS NOT NULL;

-- Cleanup.
DROP TABLE IF EXISTS yp_dedup_candidates;
DROP TABLE IF EXISTS yp_mapped_categories;
