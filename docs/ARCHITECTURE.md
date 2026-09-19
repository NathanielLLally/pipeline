# Architecture

Operational reference for the lead-sourcing pipeline: what runs where, what writes to
what, and which facts have actually been verified. Written so that infrastructure
details do not have to be re-derived from scratch each session.

**How to use this document:** when you discover or correct a non-obvious fact about the
deployment, record it here in the same session. Mark anything you did not personally
verify as unverified rather than stating it as settled — the value of this file depends
on being trustworthy without re-checking.

Last verified: 2026-09-18.

---

## Hosts

All hosts are under `accurateleadinfo.com`. Their addresses come from `.env`, which is
gitignored and symlinked into each git worktree from the main checkout.

| Role | Host | SSH | Notes |
|---|---|---|---|
| Database + watchdog | `accurateleadinfo.com` = `mail.accurateleadinfo.com` (144.91.96.230) | **port 2222** | Postgres (`leads` schema) and the canonical watchdog deployment. |
| Queue API + scraper #1 | `worker.accurateleadinfo.com` (136.119.65.9) | port 22 | Runs `gms-server-server-1` (REST API + River queue) **and** `gms-worker-worker-1`. |
| Scraper #2 | `worker2.accurateleadinfo.com` | port 22 | Worker container only. |
| Scraper #3 | `worker3.accurateleadinfo.com` | port 22 | Worker container only. |
| Workstation | `hawkeye` (local) | — | Runs the pipeline scripts and a second copy of the watchdog timer. |

### SSH: the database host uses a non-standard port

`REST_SSH_PORT` in `.env` is `22`, which is correct for the worker hosts but **wrong for
the database host**, which listens on **2222**. Connecting to it on port 22 returns
"Connection refused," which reads like "no access to this machine" but only means the
port is wrong. If one host in the fleet refuses SSH while the others accept it, probe
for an alternate port before concluding access does not exist.

The `REST_SSH_USER` account has passwordless sudo on these hosts, and the database host
can SSH to all three workers (verified).

### `REST_SSH_HOST` overlaps `SCRAPER_SSH_HOSTS`

`REST_SSH_HOST` is `worker.accurateleadinfo.com`, which is also the first element of the
`SCRAPER_SSH_HOSTS` array. The queue and a scraper share one machine. These are
overlapping *roles*, not distinct hosts, so an action aimed at "the fleet" also hits the
queue host. An earlier comment in `scripts/worker-watchdog.sh` asserted the opposite;
it has been corrected.

`internal` vs external DB URLs exist as `LEADS_DB_URL` and `LEADS_DB_URL_INTERNAL` for
connections originating on the workstation versus inside the cluster.

---

## The watchdog

`scripts/worker-watchdog.sh` runs one tick per invocation: it collects metrics from the
database, probes container state over SSH, decides a verdict, optionally acts, and
appends a row to `leads.worker_health_log`.

### Verdicts

`IDLE`, `HEALTHY`, `PROXY_DEGRADED`, `WEDGED`, `STALLED`, `HOST_UNREACHABLE`, plus two
from per-host container probes:

- `WORKER_DEGRADED` — some but not all workers are down. Restarts **only** the dead
  hosts, leaving healthy workers mid-job alone.
- `FLEET_DOWN` — every worker is down. Suppressed rather than acted on: a restart loop
  across the whole fleet is unlikely to help and usually indicates a network or
  provider problem.

**Why per-host probing is necessary.** The queue-level metrics cannot detect a dead
worker. When a container dies it silently stops drawing jobs, and the surviving workers
keep draining the backlog — so backlog, progress and yield all still look fine. This was
observed on 2026-09-18: two of three workers sat dead for roughly an hour while every
tick recorded `IDLE` or `HEALTHY`, and the LA grooming run timed out job after job until
the workers were restarted by hand. Aggregate health is not fleet health.

### Two schedulers write to one table

The canonical deployment is on the database host (`accurateleadinfo.com`), as a systemd
**user** timer: `~/.config/systemd/user/leads-watchdog.{timer,service}`, with
`OnBootSec=30s`, `OnUnitActiveSec=60s`, `AccuracySec=10s`. Its `ExecStart` is
`/home/nathaniel/leads/scripts/worker-watchdog.sh`, where `~/leads` is a **symlink** to
`/home/nathaniel/src/git/pipeline` — one checkout on `main`, not two. A full-filesystem
`find` confirmed no watchdog exists on any of the three worker hosts.

The local workstation *also* has an identically named `leads-watchdog.timer` user unit
running its own copy from the main checkout.

Both write to the same `leads.worker_health_log`. This is why the tick log shows rows in
phase-locked pairs roughly 13 seconds apart: two independent schedulers, not a bug. Two
consequences follow:

- Tick frequency in the table is **not** a measure of any one scheduler's period. The
  local timer's real period is about 70s (`OnUnitActiveSec=60s` measured from
  *completion*, plus `AccuracySec=10s` of jitter), not 60s.
- Hysteresis logic that counts consecutive verdicts is counting the interleaving of both
  writers. Anything depending on consecutive-tick counts must tolerate that.

### Installing the watchdog

The unit files are checked in at `deploy/systemd/leads-watchdog.{service,timer}` with the
installation path as a `@LEADS_ROOT@` placeholder, and `deploy/install-watchdog.sh`
renders them into `~/.config/systemd/user/` and enables the timer. Previously the units
existed only on the two hosts and in no repository, so a rebuild meant reconstructing
them from memory.

Run the installer **on** the host that should carry the watchdog, from the main checkout:

```bash
./deploy/install-watchdog.sh --dry-run    # show what would change
./deploy/install-watchdog.sh              # install, enable, start
./deploy/install-watchdog.sh --uninstall  # stop, disable, remove units
```

It is idempotent, so re-running it is also the normal way to deploy a change to the
watchdog script. Its preflight checks each encode a way this deployment has gone wrong:

- **Refuses to install from a git worktree.** A worktree path baked into `ExecStart`
  breaks as soon as the worktree is removed, leaving a timer that fails every 60s.
- **Requires `SCRAPER_SSH_HOSTS`** in `.env`, warning loudly if only the legacy singular
  `SCRAPER_SSH_HOST` is present. The fallback is worse than a failure: it yields a
  watchdog that supervises one worker of three and reports `HEALTHY` while the other two
  are dead. Note the matcher must accept an `export` prefix and ignore commented lines —
  the real `.env` contains both an active and a commented form of this array.
- **Runs one `--dry-run` tick before installing**, so a broken `.env` or unreachable
  database surfaces immediately instead of as a silent stream of failed ticks.
- **Warns when lingering is disabled.** Without `loginctl enable-linger`, a user timer
  stops at logout, which presents days later as "the watchdog silently stopped".

Two details in the units themselves are deliberate. `.env` is **not** loaded via
`EnvironmentFile`, because `systemctl show` would then print the database and SSH
credentials to anyone able to query the unit; the script sources it itself. And
`ProtectHome=read-only` must **not** be set: the watchdog SSHes with
`StrictHostKeyChecking=accept-new`, and accepting a new host key writes to
`~/.ssh/known_hosts`, so `ProtectSystem=strict` is paired with
`ReadWritePaths=@LEADS_ROOT@ %h/.ssh` instead. A read-only home would break the first
connection to any rebuilt worker — exactly when the watchdog matters most.

### Editing and deploying the watchdog

Editing the file inside a git worktree changes nothing that is running. Both timers
execute a main-checkout copy; the canonical one is remote. A change is live only after
it is committed, merged to `main`, and pulled on the database host.

**Deployment prerequisite:** the fleet-aware watchdog reads `SCRAPER_SSH_HOSTS` (a bash
array) from `.env`. The deployed `.env` on the database host currently has only the
legacy singular `SCRAPER_SSH_HOST`. The host-derivation block falls back cleanly to that
single host rather than failing, but the result is a watchdog that supervises one worker
out of three. Add the array line to the deployed `.env` before or alongside deploying
the new script. All four derivation paths (array / comma-string / singular / neither)
were tested under `set -u`; the empty case yields a `suppressed:` reason rather than a
crash.

Note the deployed checkout carries uncommitted local modifications
(`scripts/phase1.sh`) and untracked files, so a deploy should not assume a clean pull.

### Never lock the health-log table for testing

`leads.worker_health_log` is a live table with active writers on a ~60s cadence. Taking
`LOCK TABLE ... IN EXCLUSIVE MODE` on it — even inside a transaction intended to be
rolled back — stalls the real watchdog, queues blocked writers behind you, and has
resulted in the locking backend being terminated mid-statement ("server closed the
connection unexpectedly"). Postgres itself did not restart; only that backend was
killed. Test hysteresis SQL against a scratch table or a temp table instead.

---

## Database

Postgres, schema `leads`, on the mail host. Core tables:

| Table | Purpose |
|---|---|
| `businesses` | The prospect database. ~3,686 deduplicated rows. |
| `search_log` | One row per (query, geo_target, service_category) executed. Drives per-term coverage. |
| `staging_businesses` | Scratch table, truncated and reloaded each batch via `\copy`. |
| `worker_health_log` | One row per watchdog tick; hysteresis/cooldown state derives from it. |
| `qc_review` | Duplicate candidates and QC flags awaiting human adjudication. Nothing here is auto-resolved. |
| `scoring_snapshot_pre_rescore` | Rollback source retaining pre-rescore scoring columns. |

The scraper's own job-queue tables (`river_job`, `scrape_results`, `results`) live in
`public`, deliberately separated from `leads` so the two never collide.

### Deduplication

A priority waterfall: `place_id` > `domain` > `phone_normalized` >
`name_city_state_key`. Matching domain or phone alone is **not** treated as duplication
— franchise branches legitimately share both (one pet-supply domain covers 12 stores in
11 cities), and auto-merging on it would have destroyed roughly 230 legitimate rows.
Such cases are flagged into `qc_review` for a human instead.

`sources` is append-only jsonb provenance. Discovery history is never erased.

### Postgres gotchas learned here

- **Array equality is order-sensitive.** `qc_review_open_uidx` is a unique index over a
  `uuid[]`. Unsorted arrays let the same logical finding slip past `ON CONFLICT` and
  insert duplicates. Always `.sort()` before storing.
- `left('New York', 2)` is `'NE'`, not `'NY'`. State normalization uses an explicit map.
- `full` is a reserved word and cannot be used as a bare column alias.
- `execFileSync` embeds its full argv in thrown errors, which leaks the database
  password. `scripts/lib/q.mjs` exists to wrap `psql` and sanitize error output; prefer
  it over ad-hoc `psql` invocations in scripts.

---

## The pipeline

`scripts/pipeline.sh` chains four steps:

1. `gen-queries.mjs` — expands SERVICE × CITY × NEIGHBORHOOD into queries, skipping
   (geo_target, query) pairs already present in `search_log`. `--force` bypasses.
2. `create_search_job.py` — submits jobs to the REST API / River queue and polls for
   results. Handles retries; jobs stuck `pending` past 600s are abandoned and
   rescheduled.
3. `transform-and-score.mjs` — normalizes scraped rows, derives `service_category`, and
   scores them.
4. `db/upsert.sql` — dedups into `leads.businesses` and writes the search log.

Scoring lives in `scripts/lib/score.mjs`, shared by ingest and by `rescore.mjs`, so
there is exactly one definition of a good prospect. The governing rule: **score the
business, never the query that found it.** An earlier version put the search keyword in
the scoring text, which made every result of a "board and train" search score as a
board-and-train business — including veterinarians, dog parks and a hot dog restaurant.

Enrichment phases:

- **Phase A** (`enrich-from-raw.mjs`) — derives booking presence, review-velocity growth
  signals, and description backfill from `raw_scrape`. No network calls.
- **Phase B** (`enrich-websites.mjs`, not yet built) — crawls the ~3,424 known websites
  for marketing/booking signals into a `leads.website_crawl` table.
- **Phase C** (not yet built) — LLM extraction of decision makers via LiteLLM → Ollama,
  with verbatim-name guardrails.

### Corroborating people: what this niche actually publishes

Measured 2026-09-18 over two crawls through the SOCKS5 pool — 300 random non-social
websites (252 reachable) and the top 150 Tier 1/2 `dog_training` rows (120 reachable).
Only `href`/`src` targets were counted; bare string matching produces false positives
(`facebook.com/2008/fbml` is an XML namespace, `bbb.org/inc/legacy.js` a script path).

| Platform linked from homepage | Random sample | Top-tier trainers |
|---|---|---|
| Facebook | 58% | 71% |
| Instagram | 56% | 69% |
| Google Business (g.page / maps) | 25% | 42% |
| YouTube | 15% | 50% |
| Yelp | 17% | — |
| TikTok | 10% | — |
| **LinkedIn** | **5%** | — |
| Nextdoor, YellowPages, Thumbtack, Rover | **0%** | 0% |

**YellowPages and Nextdoor are dead ends here** — zero of 372 crawled sites link to
either. They should not be built into the enrichment pipeline.

**LinkedIn does not mark enterprise-scale entities in this niche.** Of 252 sites, 13
linked to LinkedIn; their median review count (39) is *lower* than the sites that do not
(52), and their Tier 1 share is lower too (8% vs 13%). Of those 13 links, 8 are
`/company/` and 5 are `/in/`, and several `/company/` pages belong to two-review
businesses — it reflects the owner's personal habit, not company size. What does track
with size is **breadth**: sites linking 3+ platforms have roughly double the median
reviews and 4–5× the Tier 1 rate of sites linking none. Breadth of social presence is
the usable size proxy; LinkedIn presence is not.

**Meta is not fetchable from this proxy pool.** Facebook returns HTTP 400 to the
datacenter ASN on every profile URL tested (6/6). Instagram returns either a 200 login
shell with the `og:` metadata stripped out, or 429. So a Facebook/Instagram URL is
useful only as a *stored identifier* for a human to open later, never as a page to parse.

**The two workable on-site person signals**, both from the prospect's own website:

1. **Name + title on an about/team/bio page.** Among top-tier trainers, 82/120 had such
   a page and deterministic regex extraction produced a name+title for 39 (33%), with
   about 19% of the extracted strings malformed (a leading noun captured as a first
   name: `Maryland Dog (Owner)`, `Results Cost (Owner)`). Yield is far worse on the
   random sample (7%) because it is grooming-heavy — grooming sites mostly have no
   about page at all. Decision-maker extraction is worth running on trainers, not on
   the whole database.
2. **A person-shaped email local part.** 42% of reachable sites expose one
   (`michael@atlantacanine.com`, `sarah@synergydogco.com`) versus 26% generic role
   addresses. This is an independent second source, but it rarely corroborates the
   scraped name directly — only 13–30% of sites having both had them agree, because the
   email often belongs to a different staff member.

Combined, **63% of top-tier trainer sites carry at least one person signal.** Treat the
two as separate evidence rather than requiring agreement; requiring both would discard
most of the yield.

### Known data caveat: capped review samples

The scraper caps `user_reviews` at 8 entries, and 2,165 rows sit exactly at that cap.
For a busy business the captured window is therefore truncated, so review cadence
measures the cap rather than the business. `enrich-from-raw.mjs` floors the window at 30
days and labels such figures `>=` and `[capped sample]`.

---

## Claiming ad spend: what `marketing_active` does and does not mean

Prompt 7 forbids claiming a business runs paid advertising without reliable evidence.
`scripts/lib/site-signals.mjs` enforces that by setting `marketing_active` **only** on a
confirmed paid-media pixel — a `gtag/js?id=AW-` conversion id, `googleadservices`, a
`google_conversion_id`, `fbevents.js`, or an `fbq('init')` call. GA4, Google Tag Manager,
Hotjar, Klaviyo and HubSpot are recorded separately as `martech` and never set the flag.
That asymmetry is deliberate: analytics means someone measures traffic, a pixel means
someone bought it.

The flag's honest reading is **"paid acquisition infrastructure is present"**, not "this
business is running ads today". A pixel outlives the campaign that installed it, so one
left from a six-week push two years ago is byte-identical to one backing $8k/month. The
column is evidence of intent and capability; it is not evidence of live spend, and it
must not be described as "running ads" in exports, segments or campaign copy.

Base rate, measured across all 1,206 reachable `dog_training` sites rather than a
sample: **16%** (195) carry a paid-media pixel. For comparison from the same full pass:
booking 28%, lead form 36%, an about page 70%, a person-shaped email 59%. An earlier figure of 59% quoted in conversation
was wrong — it came from a 20-row dry run drawn from the *top* of the database by ICP
score, which is the most heavily marketed tail of the distribution. Any future rate
claim should name the population it was computed over.

### Google Ads Transparency: SOLVED (2026-09-19)

Upgrading a subset of businesses from "infrastructure present" to *confirmed current
spend* needs a second source. Google's Ads Transparency Center is the plausible one —
unlike Meta's Ad Library, it is reachable from the datacenter proxy pool (HTTP 200,
~2.5MB). Meta is not: the Ad Library page returns 403 and its API 500 through this pool.

The attempt did not succeed and is recorded here so it is not blindly repeated:

- The public page at `adstransparency.google.com/?region=US&domain=<domain>` renders
  client-side and contains none of the ad data in its HTML. It only echoes the query.
- The data comes from an internal RPC, `POST /anji/_/rpc/SearchService/SearchAdvertisers`
  (and `SearchCreatives`), with an `f.req=<json array>` form body and the public API key
  that is baked into the page (`AIzaSy…`, a client key, not a project secret).
- The endpoint **is** reachable and does parse the payload: a wrongly-typed field returns
  `BadRequestException: Trouble converting f.req=… to class …SearchAdvertisersRequest`,
  which confirms both the method name and that the request arrives intact.
- Roughly 25 positional layouts were probed. Index 2 is a numeric enum (a string, bool or
  array there is rejected; a number is accepted). No arrangement of the query string at
  any index returned rows — including for `Nike`, which is certainly an advertiser, and
  including a bare request that should have failed if the query field were required.
  Every accepted shape returned `{}`.

**That conclusion was wrong, and the way it was wrong is worth recording.** Driving
Playwright to the page and reading the real request off the network tab took minutes and
showed the payload is a **JSON object with numeric string keys**, not the positional
array that roughly 25 probes had assumed:

```
f.req={"2":40,"3":{"8":[2840],"12":{"1":"<domain>","2":true}},"7":{"1":1,"2":0,"3":2840}}
```

POSTed form-encoded to `/anji/_/rpc/SearchService/SearchCreatives?authuser=` with
`content-type: application/x-www-form-urlencoded`, `x-same-domain: 1`, a browser
user-agent and a referer. **No cookies, no XSRF token, no API key** — the browser's own
request carries an empty `x-framework-xsrf-token` header.

The diagnostic trap: a JSON array is structurally valid protobuf-JSON, so the server
accepted every malformed probe and answered `{}` rather than erroring. `{}` was read as
"authenticated but empty" when it actually means **"this advertiser runs no ads"** — the
same answer the real page gets for a small dog trainer. The lesson is that an empty
success is not evidence of an auth wall, and that capturing one real request is worth
more than any number of black-box probes.

Verified: `nike.com` returns ~15KB of creatives, `booking.com` ~22KB,
`offleashk9training.com` `{}` (genuinely not advertising). Also verified through the
Webshare datacenter pool, 5 proxies out of 5 — unlike Meta, Google does not refuse this
ASN, so a full pass over the database is viable from the existing infrastructure.

This is a **confirmed-current-spend** signal and is materially stronger than
`marketing_active`: a returned creative is an ad Google is serving now, not a pixel left
over from a campaign that ended two years ago.

### Wired up: `scripts/enrich-ads-transparency.mjs` (2026-09-19)

`db/migrations/004_ads_transparency.sql` adds `leads.ads_transparency` — one row per
(business, domain), raw creative ids and date ranges in `jsonb` — plus three rolled-up
columns on `leads.businesses`: `ads_confirmed_active`, `ads_last_shown`,
`ads_creative_count`. `scripts/lib/ads-transparency.mjs` holds the request shape and the
response decoder, with 41 unit tests over captured fixtures.

**The pass is complete: 3,347 domains checked, zero unchecked, zero errors.** 935 are
advertising (28%), 761 of them within the last 30 days, and 14,212 creatives are stored.

**It disagrees with `marketing_active` in both directions, which is the point.** In the
final 1,949-domain run alone, **302 confirmed advertisers had no pixel at all** and 35
carried a pixel while running no ads — `timelessk9.com` among them, last creative 128
days old. Neither column subsumes the other: the pixel proves configuration, the
creative proves spend. Segment on `ads_confirmed_active` when the campaign copy claims
the prospect is advertising.

Confirmed spend rises monotonically with ICP tier, which is a useful independent check
that the tiers mean something: Tier 1 48% advertising, Tier 2 19%, Tier 3 13%, Tier 4 7%
(measured before the 2026-09-19 rescore, against the tiers as they then stood).

Field meanings are recorded in `scripts/lib/ads-transparency.mjs`; the ones that matter
are per-ad `7.1` (last shown — the recency signal), `6.1` (first shown), `12`
(advertiser name) and top-level `2` (pagination cursor, whose presence alongside a full
40-row page means `creative_count` is a floor, not a total).

### The rate limiter, which is not a 429

A full-speed pass at ~7 domains/s tripped Google's limiter partway through and then
collected **595 useless responses** before it was noticed. Three things about it are
worth knowing before touching this script:

- It arrives as **HTTP 302 to `https://www.google.com/sorry/index`**, not a 429 and not
  a 403. Code that only retries the usual throttle statuses will treat it as a real
  answer.
- It applies **across the whole pool at once**, not per IP. 12 of 12 sampled proxies
  were blocked simultaneously, so rotating proxies does not clear it — only waiting
  does. This is the same lesson as the Webshare direct list: refreshing addresses is not
  a throttle remedy.
- A throttled attempt **must not be stored**. It asked Google nothing, so writing it as
  an error row would make a never-checked domain look permanently checked and cause a
  resumed run to skip it forever. The script drops them and a circuit breaker aborts
  after 12 consecutive throttles, exiting non-zero so a piped or scheduled run cannot
  report success on a partial database.

Defaults are now 4 workers with a 250ms pause, roughly 3–4 domains/s. Throughput was
never the constraint — even that rate covers the whole database in under ten minutes —
so the slower setting costs nothing real and avoids the block.

Every pass begins by probing `nike.com`, a domain certain to advertise. An empty
response is indistinguishable from a malformed request, so without that check a broken
payload would silently write "no ads" for the entire database. The probe distinguishes
its two failure modes: a throttle exits 3 and says to wait, a genuinely empty result
exits 1 and says to re-capture the request with Playwright.

### Transports, and what each one costs

Google blocks the **Webshare datacenter ASN outright** on this endpoint, so a
residential exit is not an optimisation here — it is the only thing that works. The
script tries three transports in preference order, selected automatically from `.env`:

| Transport | Env var | Billing | Verdict |
|---|---|---|---|
| DataImpulse residential | `DATAIMPULSE_PROXY` | per **gigabyte** | **Use this.** |
| scrape.do `super=true` | `SCRAPEDO_TOKEN` | 10 credits/request | Works; far too dear. |
| Webshare SOCKS5 pool | `PROXY_LIST_URL` | free | Blocked by Google. |

`--no-residential` and `--no-scrapedo` step down the chain for testing.

**The cost difference is not marginal.** A response is ~2.9KB gzipped (55.7% of domains
return `{}`, mean 7.5 creatives), so the entire 2,000-domain remainder is about 6MB.
scrape.do billed 10 credits for each of those ~3KB responses: a 1,000-credit account
bought **95 domains** before running dry. DataImpulse did the remaining 1,949 domains in
801 seconds at a steady 2.4/s with **zero throttling and zero errors** — the /sorry
redirect never appeared once. Measure bytes before buying per-request pricing for an
endpoint whose responses are this small.

Credentials for both are in `.env` (gitignored) and never in committed code. The
scrape.do token sits in a URL, so `scrub()` strips it from anything printed or written
to `fetch_error`; the residential password is passed via curl's `--proxy-user` rather
than an inline URL, and is scrubbed from stored errors for the same reason.

---

## Scoring: tier calibration and the enrichment bonuses

`scripts/lib/score.mjs` is the single definition of ICP fit, shared by
`transform-and-score.mjs` (ingest) and `rescore.mjs` (backfill). Enrichment fields —
`bookingPresent`, `growthScore`, `adsConfirmedActive`, `adsCreativeCount` — are optional
and default to `null`, because ingest scores a business the moment it is discovered,
before any enrichment has run. **`null` means "not checked", not "does not have it"**, so
an unenriched row is never penalised for missing evidence it was never asked for.

Confirmed ad spend is weighted at **+12**, above booking (+8) and growth (+8), with a
further +4 for 10 or more concurrent creatives. The reasoning: a business currently
paying Google to acquire customers has already decided that buying customers is worth
money, which is precisely the decision a lead buyer must have made. Booking and growth
are evidence of a well-run business; ad spend is evidence of budget. A business that
advertised at some point but has nothing live in 30 days gets +4 ("lapsed ad spend"),
not the full bonus, because the campaign is off and may have been stopped for cost.

The scorer deliberately does **not** key this off `marketing_active` — see the section
above on why a pixel and a live creative are different claims.

### Tier calibration, and why it had to move (2026-09-19)

Thresholds are **83 / 70 / 50**, not the original 70/50/30.

The original cutoffs were set when the scorer saw only the Google Maps record. Once
booking, growth and ad spend began contributing there were up to 32 further points in
play, and at a cutoff of 70 the top tier drifted to **36% of the database** — 1,417
rows, which is not a priority list. `CLAUDE.md` is explicit that the highest-priority
segment must not be diluted, so the cutoffs were re-derived from the actual score
distribution to hold Tier 1 near 15%.

A caution worth recording, because it nearly caused a wrong diagnosis: when the first
post-enrichment rescore showed Tier 1 tripling, the obvious culprit was the new ads
bonus. Decomposing the shift signal by signal showed otherwise —

| Scenario | Tier 1 |
|---|---|
| stored in DB (pre-rescore) | 540 (13.9%) |
| scorer with no enrichment | 526 (13.5%) |
| + booking | 921 (23.7%) |
| + booking + growth | 1,137 (29.2%) |
| + ads as well | 1,417 (36.4%) |

— most of the movement was `booking_present` and `growth_score`, whose bonuses had been
**coded but never applied**, because no rescore had been run since Phase A enrichment
populated them. Ads contributed 280 rows of the 891. The lesson generalises: after an
enrichment pass writes new columns, the stored scores are stale until `rescore.mjs`
runs, and a tier histogram read before that reflects the old inputs.

Resulting distribution over 3,888 businesses: Tier 1 621 (16.0%), Tier 2 796 (20.5%),
Tier 3 1,621 (41.7%), Tier 4 850 (21.9%). Of the 621 in Tier 1, **473 have confirmed ad
spend**, 449 have booking, exactly one has no enrichment evidence at all, and zero are
veterinarians, pet-supply stores or dog parks.

`qc_status = LOW_PRIORITY` is still keyed to an absolute score below 30 rather than to a
tier. That is deliberate: it answers "is this row worth keeping at all", which is a
different question from where a row ranks, and it should not move when tiers are
recalibrated.

**Re-measure before moving these cutoffs again.** Picking a round number rather than
reading the distribution is what inflated Tier 1 the first time.

---

## Decision makers: why extraction is deterministic, and what it costs

`scripts/lib/people.mjs` + `scripts/enrich-decision-makers.mjs` implement Prompt 9 over
the page text already in `leads.website_crawl`. No network, no model. The plan allowed an
LLM here; explicit patterns turned out precise enough that a model would mostly add cost,
latency and a new way to hallucinate a name — the one thing Prompt 9 forbids outright.

The niche's defining hazard is that **"owner" usually means the dog's owner**. Measured
over the corpus, 1,355 of 1,994 pages containing "owner" (68%) use it as "dog owner",
"pet owners", "their owner". Proximity between a capitalized word and "owner" is
therefore wrong more often than right, and ownership sense is disambiguated from the
left context before any title is accepted.

Measured over 1,307 crawled businesses, at `--min-confidence high`:

| | |
|---|---|
| Businesses with a decision maker found | 276 (21%) |
| high / medium / low | 177 / 65 / 29 |
| Hand-audited precision, **all** high rows | ~97% |

Coverage is low on purpose. Recall was traded away for precision at every ambiguous
call, because a wrong name in a cold email is worse than an empty column, and an empty
`decision_maker_name` is a normal result rather than a failure.

### What the patterns had to learn

Each of these was a real false positive from a corpus run, and each is now a regression
test in the unit suite:

- **Case-insensitivity destroys the signal.** Capitalization is the *only* thing marking
  a name in running prose. Under `/i`, `[A-Z]` matches lowercase, so captures ran past
  the name ("Donald Hutcherson and"). The title patterns use explicit character classes
  instead of the `i` flag.
- **A bare space is weak evidence.** Allowing `Name Title` with no punctuation was needed
  to read staff blocks ("Valerie Fry Owner / CEO"), but it also produced a *client
  testimonial* as the founder ("As Featured In Client Steph Curry Meet the Founder").
  The bare-space form is now accepted only when nothing was trimmed off the end of the
  capture and the name is exactly two words. Punctuation earns three words; a space does
  not.
- **"Vice President" is not the principal.** The title alternation matched its
  "President" tail, which both recorded the wrong role and ate the name — yielding
  "Jake Satterlee Vice / President", and from one bio's *previous corporate job*,
  "Senior Vice / President".
- **Trimming junk can manufacture a name.** `cleanName` trims stray words rather than
  rejecting outright, so "Puppy Kindergarten" would become "Kindergarten". Trimming must
  leave at least two words, and any junk word surviving in a two-word capture voids it.
- **Guards must anchor to the cleaned name, not the raw capture.** The article guard
  tested the text before the greedy match, so "Meet the Team Zephyr Dippel Owner" was
  discarded because "the" preceded "Team". The article qualified the junk word, not the
  person. Fixing this recovered six real names.
- **Co-owner is not Owner**, and a repeated word ("Maria Maria") is a rendering artifact.

### Auditing lesson

An early precision figure of ~97% was computed from the first 30 rows and was wrong: the
full 204-row dump was ~84%, with ~32 junk rows the sample never showed. The head of a
list ordered by extraction quality is the best part of it. **Precision claims here must
be computed over the whole output set**, which is why the dry run takes `--limit 400`
rather than printing a sample.

---

## Open items / unverified

- The fleet-aware watchdog (commit `da4e4e6`) is **committed but not deployed**. The
  live copy on the database host is still the stubbed version (no `SCRAPER_SSH_HOSTS`,
  no `worker_states`, `TODO` intact). Deploying it also requires adding the
  `SCRAPER_SSH_HOSTS` array to that host's `.env` — see above. The unit files and an
  idempotent installer now live in `deploy/`, so the remaining work is a `git pull` plus
  `./deploy/install-watchdog.sh` on that host; the installer's preflight checks the
  `.env` prerequisite rather than letting it fail silently.
- Google Ads Transparency is **complete and scored**: 3,347 domains checked, 0
  unchecked, 935 advertising, and `ads_confirmed_active` now contributes to `icp_score`
  (see "Tier calibration" below). Re-running only picks up businesses discovered since.
  The recency window is fixed at 30 days (`RECENT_DAYS`), chosen from a visible gap in
  the observed data rather than measured against outcomes — worth revisiting once there
  is campaign-response data to calibrate against.
- Whether the local workstation timer should keep running is undecided. It duplicates
  the remote one into the same table; if the remote deployment is canonical, the local
  timer is arguably redundant and could be disabled to make the tick log single-writer.
