# Phase 3+ TODO

## Email Extraction Channels

### Form Submission (HOLD)
- **Status:** Code exists (`scripts/submit-forms.mjs` + `scripts/lib/forms.mjs`), schema in place (`leads.form_submission`)
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

### Address Verification (`scripts/mxCheck.pl`)
- **Status:** In repo, instrumented with `--debug`, `--rate-limit`, `--socks5-proxy`.
  Schema landed (`db/migrations/008_email_verification.sql`, applied). All paths
  verified working end-to-end (direct SMTP, catch-all probe, rate-limited dispatch,
  SOCKS5-tunneled SMTP against smtp.google.com).
- **Not yet done:** bulk run across the 3,063 addresses in `leads.business_email`,
  and no script writes `mxCheck.pl`'s JSON output into `leads.email_verification` yet
  — that loader (read business_email, shell out to mxCheck.pl, upsert results) still
  needs to be written.
- **Proxy reality check (measured 2026-09-21):** the project's Webshare pool
  (`PROXY_LIST_URL`) refuses CONNECT to ports 25/465/587 outright — cannot be used
  for SMTP verification as configured. All three scraper hosts also cannot reach
  port 25 outbound at all (provider-level anti-spam policy), so an `ssh -D` tunnel to
  any of them doesn't help either. Only this workstation's own IP is confirmed to
  reach port 25 today. `--socks5-proxy` was validated against a local `ssh -D`
  loopback tunnel, not against anything already in this project's inventory — a bulk
  run today would go out this workstation's IP unproxied, or `--rate-limit` alone.
- **Caution:** a full pass makes ~3,000 outbound SMTP connections from one IP; pace it
  with `--rate-limit`, or risk being rate-limited/blocklisted.
- **Expected effect:** catch-all domains report as unverifiable, not valid, so the
  verified count will be conservative — treat "not verified" as unknown, not as dead

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

