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

### Known data caveat: capped review samples

The scraper caps `user_reviews` at 8 entries, and 2,165 rows sit exactly at that cap.
For a busy business the captured window is therefore truncated, so review cadence
measures the cap rather than the business. `enrich-from-raw.mjs` floors the window at 30
days and labels such figures `>=` and `[capped sample]`.

---

## Open items / unverified

- The fleet-aware watchdog (commit `da4e4e6`) is **committed but not deployed**. The
  live copy on the database host is still the stubbed version (no `SCRAPER_SSH_HOSTS`,
  no `worker_states`, `TODO` intact). Deploying it also requires adding the
  `SCRAPER_SSH_HOSTS` array to that host's `.env` — see above.
- Whether the local workstation timer should keep running is undecided. It duplicates
  the remote one into the same table; if the remote deployment is canonical, the local
  timer is arguably redundant and could be disabled to make the tick log single-writer.
