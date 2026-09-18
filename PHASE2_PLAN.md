# Phase 2 Execution Plan

## Track A: Dog Training Phase 2 (Refined Searches)
Deepen existing metros with high-value specialist searches:
- board and train
- aggressive dog training
- reactive dog training
- dog behavior modification
- private dog trainer
- in-home dog training
- puppy training specialist

**Priority order** (metro → backlog size):
1. New York, NY (largest market)
2. Los Angeles, CA
3. Chicago, IL
4. Houston, TX
5. Phoenix, AZ
6. Philadelphia, PA
7. San Antonio, TX (if available)
... (remaining metros)

**Execution**: `./scripts/pipeline.sh --phase prompt2 --service-category dog_training --metro "City, ST" --workers 3`

## Track B: Dog Daycare + Boarding Phase 1 (New Category)
Launch across all 16 metros with primary search terms:
- dog daycare
- dog boarding
- luxury dog boarding
- dog resort
- pet resort
- dog daycare and boarding

**Priority order** (markets with highest concentration of affluent households):
1. San Francisco Bay Area, CA (premium market)
2. Los Angeles, CA
3. New York, NY
4. Boston, MA
5. Washington, DC
6. Seattle, WA
... (remaining metros)

**Execution**: `./scripts/pipeline.sh --phase prompt1 --service-category daycare_boarding --metro "City, ST" --workers 3`

## Watchdog Coverage
Both tracks are monitored by the remote watchdog timer on `mail.accurateleadinfo.com`. 
View live health: `ssh -p 2222 nathaniel@accurateleadinfo.com 'journalctl --user -u leads-watchdog.service -f'`

## Success Metrics
- Track A: 20%+ Tier 1 prospects (high-ticket, behavior specialist signals)
- Track B: 15%+ Tier 1 prospects (recurring revenue, premium positioning)

## Estimated Timeline
- Track A: ~2-3 days (87 base + 50%+ refinement queries)
- Track B: ~3-4 days (16 metros × ~30 searches)
