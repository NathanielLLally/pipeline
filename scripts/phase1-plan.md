# Phase 1 Execution Plan — dog_training Dog Service Prospects

**Goal:** Build a deduplicated, scored database of high-quality dog training prospects across 16 US metros using Google Maps as the data source.

**Status:** 2 of 16 metros completed (LA + partial NY); 14 pending.

---

## Current Database State

| Metric | Value |
|--------|-------|
| **Total Businesses** | 473 |
| **Geo-targets Covered** | 20 |
| **Qualified Prospects (Tier 1–3)** | 465 |
| **Tier 1 (Premium)** | 141 |
| **Tier 2 (Strong)** | 210 |
| **Tier 3 (Developing)** | 114 |
| **Rejected (Non-ICP)** | 8 |

**Coverage by Metro:**
- ✓ Los Angeles, CA (11 cities) — 263 businesses
- ✓ New York, NY (9 of 11 cities) — 210 businesses, 33-query gap pending rate-limit clearance
- ◌ Chicago, IL — 8 cities planned
- ◌ Houston, TX — 5 cities planned
- ◌ Phoenix, AZ — 5 cities planned
- ◌ Dallas, TX — 5 cities planned
- ◌ San Francisco Bay Area, CA — 6 cities planned
- ◌ Seattle, WA — 4 cities planned
- ◌ Denver, CO — 4 cities planned
- ◌ Boston, MA — 5 cities planned
- ◌ Miami, FL — 5 cities planned
- ◌ Atlanta, GA — 5 cities planned
- ◌ Washington, DC — 5 cities planned
- ◌ Austin, TX — 4 cities planned
- ◌ Philadelphia, PA — 4 cities planned
- ◌ San Diego, CA — 5 cities planned

**Total Planned Geo-targets:** ~85 (estimate)

---

## Execution Parameters

Each batch runs:
```bash
./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "METRO" --workers 3
```

**Configuration:**
- **Workers:** 3 (scraper concurrency per metro)
- **Poll Timeout:** 600s (auto-reschedule hung jobs)
- **Max Retries:** 3 (per keyword)
- **Estimated Runtime per Metro:** 60–90 min (varies with city count, result density, scraper latency)

**Total Estimated Time:** ~20–24 hours for all 14 pending metros (serial execution)

---

## Pipeline Stages (per metro)

1. **gen-queries.mjs** (2–5 min)
   - Cross-reference `search_log` for coverage by city
   - Generate `SERVICE × CITY` keywords (e.g., "dog trainer in Chicago, IL", "board and train in Lincoln Park, Chicago, IL")
   - Skip already-covered cities

2. **create_search_job.py** (40–60 min)
   - Submit keywords to REST API in batches (3 workers, 5s poll interval)
   - Reschedule timed-out jobs (600s deadline per keyword)
   - Save JSON result arrays to `map-outputs/{batch}/results/`

3. **transform-and-score.mjs** (5–10 min)
   - Read result JSON files
   - Normalize fields: `web_site` → domain, phone digits, address → city/state/zip
   - Compute ICP score + tier (Tier 1–4 based on training sub-type + rating + reviews + website + categories)
   - Assign QC status: VALID / NEEDS_ENRICHMENT / LOW_PRIORITY / REJECTED
   - Write staging CSV

4. **upsert.sql** (1–5 min)
   - Load staging CSV via `\copy`
   - Dedup: match by place_id → domain → phone → name+city+state
   - Merge: insert new / update existing (sources array grows, never shrinks)
   - Log one `search_log` row per (geo_target, service_category) summarizing coverage

---

## How to Execute

### Option A: Run All Pending Metros (Automated)
```bash
./scripts/enumerate-phase1-calls.sh --execute
```
Runs Chicago → Houston → Phoenix → ... → San Diego sequentially. Each metro blocks until complete.

### Option B: Run One Metro at a Time (Manual)
```bash
cd /home/nathaniel/leads/.claude/worktrees/you-are-a-lead-sourcing-glittery-curry
set -a && source .env && set +a
./scripts/pipeline.sh --phase prompt1 --service-category dog_training --metro "Chicago, IL" --workers 3
```

### Option C: Check Current Coverage
```bash
./scripts/enumerate-phase1-calls.sh --all
```
Shows which metros are covered and which are pending.

---

## Known Constraints

1. **Rate-limiting:** Google Maps scraper enforces per-minute query budgets. Observed pattern: ~60–80 jobs/hour sustained, then 24–72h zero-result blocks. Mitigation: 3 workers per metro (lower throughput per batch, easier to absorb blocks); VPS routing + proxy exploration pending.

2. **Geo-target Coverage Tracking:** `search_log` logs coverage per city, not per search-term. Ingesting a partial batch marks all cities "covered" even if some term-queries never ran. Solution: either complete the batch cleanly (wait for rate-limit to clear) or accept the gap and re-run the gaps later with a different phase marker.

3. **Poll Timeout:** Jobs stuck server-side in `"running"` state now auto-timeout at 600s and reschedule (up to 3 attempts). Without this, a single hung job would park a worker thread forever, breaking a 3-worker batch.

4. **Worker Pool:** Bottleneck is server-side concurrency, not local workers. Increasing from 3 to 10 workers doesn't meaningfully improve throughput per metro.

---

## Quality Benchmarks

**Los Angeles:**
- 263 businesses discovered
- 247 VALID (93.9%)
- 9 LOW_PRIORITY (3.4%)
- 5 REJECTED (1.9%)
- Tier 1: 74, Tier 2: 111, Tier 3: 62, Tier 4: 16

**Expected per metro (rough):**
- Small metro (5 cities): 150–200 businesses
- Large metro (10+ cities): 300–500 businesses

---

## Next Steps After Phase 1

1. **Prompt 2:** Website qualification — fetch each Tier 1–2 business's website, look for high-ticket program signals (multi-week training, custom pricing, private coaching, board-and-train details).

2. **Prompt 3:** Geographic expansion — retarget metro-pairs (e.g., Dallas + Austin, Boston + Philadelphia) to check for density underperformance; expand into secondary metros if qualified-prospect yield is high.

3. **Prompt 4+:** Additional service categories — daycare_boarding, grooming, dog_walking_petsitting (separate ICP, lower base scores, different messaging).

---

## Log & Monitoring

Track progress in:
- Database: `SELECT geo_target, count(*) FROM leads.businesses GROUP BY geo_target ORDER BY 1;`
- Coverage: `SELECT geo_target, new_businesses, duplicates, qualified FROM leads.search_log ORDER BY geo_target;`
- Health: Scraper API health at `https://worker.accurateleadinfo.com/api/v1/health`

---

## Estimated ROI

- **Current:** 473 qualified prospects across 20 cities (23.7 prospects/city)
- **After all metros:** ~1,600–2,000 qualified prospects (19–24 prospects/city average)
- **Target:** 500+ Tier 1 + Tier 2 prospects (high-quality leads for downstream outreach)
