# Phase 3+ TODO

## Email Extraction Channels

### Form Submission (HOLD)
- **Status:** Code exists (`scripts/form-submission.mjs`), schema in place (`leads.form_submission`)
- **Issue:** Risky without proper bot detection handling — high honeypot/CAPTCHA trigger risk using owner credentials
- **Plan:** Refactor using Playwright for proper browser automation + bot handling
- **Alternative:** Evaluate scrapemate/Go extractor that can utilize current infrastructure
- **Decision pending:** Do not execute until refactor is complete
- **Affected:** ~242 businesses with contact form only (no published email)
- **Coverage impact:** +242 emails if activated, bringing email coverage from 56% to ~58%

### Completed Email Channels
- ✓ Website crawl + mailto: links → 3,018 emails (2,145 businesses)
- ✓ RDAP domain registrant → 45 emails
- ✓ Decision maker extraction → 425 names (complementary, not email)

---

## Phase 3 Part 2 Options

### Option A: Extend Secondary Service Categories
- Grooming: 11 cities → 105 metros (estimated +500–1,000 businesses)
- Dog walking/pet sitting: 5 cities → 105 metros (estimated +200–400 businesses)
- Rationale: Broaden addressable market; high email extraction rate expected (~56% baseline)

### Option B: Geographic Expansion
- Secondary metros (population >500k, not in original 16)
- Identify underperforming metros (existing but low yield)
- Rationale: Dog training already at 105 cities; may have saturated

### Option C: Accept Current State & Export
- 3,888 businesses, 2,190 with email (56%), 425 with decision maker (11%)
- 1,417 Tier 1+2 prospects with email
- 180 Tier 1+2 with both email + name
- Rationale: Sufficient for initial cold email campaign; refine incrementally

---

## Known Limitations
- Email coverage plateaued at 56% (3,018 emails from crawl + mailto:, 45 from RDAP)
- Decision maker extraction hit 11% (425 names) — high precision, conservative recall
- Dog training coverage complete (105 metros); grooming minimally covered (11 metros)
- Form submission blocked pending Playwright refactor

