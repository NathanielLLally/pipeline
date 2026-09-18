# Phase 2 Execution Guide

## Quick Start

```bash
cd /home/nathaniel/leads

# Run all metros in parallel (recommended)
./scripts/phase2-all-metros.sh

# Or run sequentially
./scripts/phase2-all-metros.sh --sequential

# With specific worker count
./scripts/phase2-all-metros.sh --workers 5
```

## What It Does

**Track A: Dog Training Phase 2** (Refined searches)
- 16 metros × 24 specialized queries each
- board & train, behavior/aggression specialists, puppy programs, in-home training
- Expect high dedup rate (hitting same Tier 1 prospects with refined terms)

**Track B: Daycare + Boarding Phase 1** (New category)
- 16 metros × 28-56 queries each
- dog daycare, luxury boarding, dog resort, pet resort
- Expect 40-50% Tier 1 conversion

## Execution Modes

### Parallel (Default)
- Both tracks run simultaneously
- Metros processed in priority order
- Faster completion (~3-5 days vs 7-10 days sequential)
- Better load distribution across scraper workers

### Sequential
- Track A all 16 metros first, then Track B
- Lower resource utilization
- Useful for testing or conservative approach

## Monitoring

```bash
# Watch master log
tail -f phase2-logs/phase2-all.log

# Watch individual track logs
tail -f phase2-logs/track-a-*.log
tail -f phase2-logs/track-b-*.log

# Check worker health on remote
ssh -p 2222 nathaniel@accurateleadinfo.com \
  'tail -f /var/log/journal | grep -i watchdog'

# Check database progress
psql $LEADS_DB_URL -c "
  SELECT service_category, COUNT(*), COUNT(*) FILTER (WHERE icp_tier='Tier 1')
  FROM leads.businesses
  GROUP BY service_category;
"
```

## Expected Outcomes

### Track A: Dog Training Phase 2
- ~3,000-5,000 results per metro
- 0-10% new (mostly duplicates = confirmed ICP)
- Total new: ~100-300 businesses
- Avg Tier 1: 55-65%

### Track B: Daycare + Boarding Phase 1
- ~150-200 results per metro
- 30-50% new businesses
- Total new: ~400-600 businesses
- Avg Tier 1: 40-50%

## Database Impact

Starting counts:
- Dog Training: 1,962 total (1,080 Tier 1)
- Daycare/Boarding: 49 total (21 Tier 1)

Projected after Phase 2:
- Dog Training: 2,100-2,300 total (55-60% Tier 1)
- Daycare/Boarding: 450-650 total (40-50% Tier 1)

## Troubleshooting

**Batch fails mid-run:**
- Check individual log: `cat phase2-logs/track-{a,b}-*.log`
- Watchdog status on remote: `ssh -p 2222 nathaniel@accurateleadinfo.com 'systemctl --user status leads-watchdog.service'`
- Restart watchdog if needed: `ssh -p 2222 nathaniel@accurateleadinfo.com 'systemctl --user restart leads-watchdog.service'`

**High failure rate:**
- Check remote worker logs: `ssh -p 2222 nathaniel@accurateleadinfo.com 'docker logs gms-worker-worker-1 | tail -50'`
- May indicate proxy exhaustion; watchdog should auto-refresh (when action logic is enabled)

**Duplicate rate higher than expected:**
- Normal! Refined searches hit existing high-quality prospects
- Indicates good dedup logic working correctly

## Resume from Interruption

If interrupted, use `--skip-completed`:
```bash
./scripts/phase2-all-metros.sh --skip-completed
```

This will only run metros that haven't completed yet.

## Next Steps (Phase 3+)

After Phase 2 completes:
1. Review Tier 1 businesses for enrichment (websites, pricing, booking signals)
2. Run Phase 3: Expand grooming & dog walking/petsitting
3. Phase 4: Database quality control & dedup across all categories
4. Phase 5+: Decision maker enrichment, marketing signal detection

