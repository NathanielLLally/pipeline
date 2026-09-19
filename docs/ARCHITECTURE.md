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

| Role | Host | Notes |
|---|---|---|
| Database + watchdog | `mail.accurateleadinfo.com` | Postgres (`leads` schema). SSH on port 22 is **refused** from the workstation; administer it by other means. |
| Queue API + scraper #1 | `worker.accurateleadinfo.com` | Runs `gms-server-server-1` (REST API + River queue) **and** `gms-worker-worker-1`. |
| Scraper #2 | `worker2.accurateleadinfo.com` | Worker container only. |
| Scraper #3 | `worker3.accurateleadinfo.com` | Worker container only. |
| Workstation | `hawkeye` (local) | Runs the pipeline scripts and a second copy of the watchdog timer. |

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

### Two schedulers write to one table

The canonical deployment is on **accurateleadinfo.com** (per the project owner). The
local workstation *also* has a `leads-watchdog.timer` systemd **user** unit in
`~/.config/systemd/user/`, invoking `/home/nathaniel/leads/scripts/worker-watchdog.sh`
from the **main checkout**.

Both write to the same `leads.worker_health_log`. This is why the tick log shows rows in
phase-locked pairs roughly 13 seconds apart: two independent schedulers, not a bug. Two
consequences follow:

- Tick frequency in the table is **not** a measure of any one scheduler's period. The
  local timer's real period is about 70s (`OnUnitActiveSec=60s` measured from
  *completion*, plus `AccuracySec=10s` of jitter), not 60s.
- Hysteresis logic that counts consecutive verdicts is counting the interleaving of both
  writers. Anything depending on consecutive-tick counts must tolerate that.

### Editing the watchdog

Editing the file inside a git worktree changes nothing that is running. The local timer
executes the main checkout's copy; the real deployment is remote. A change is live only
after it is committed, merged, and deployed.

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

### Known data caveat: capped review samples

The scraper caps `user_reviews` at 8 entries, and 2,165 rows sit exactly at that cap.
For a busy business the captured window is therefore truncated, so review cadence
measures the cap rather than the business. `enrich-from-raw.mjs` floors the window at 30
days and labels such figures `>=` and `[capped sample]`.

---

## Open items / unverified

- The watchdog's deployment details on the mail host (unit name, schedule, which copy of
  the script) are **unverified** — SSH to that host is refused from the workstation. The
  fact that it runs there is from the project owner, not observed directly.
- The local workstation timer's continued presence may be intentional or vestigial; it
  has not been confirmed either way.
