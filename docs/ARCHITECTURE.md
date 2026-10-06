# Architecture

Operational reference for the lead-sourcing pipeline: what runs where, what writes to
what, and which facts have actually been verified. Written so that infrastructure
details do not have to be re-derived from scratch each session.

**How to use this document:** when you discover or correct a non-obvious fact about the
deployment, record it here in the same session. Mark anything you did not personally
verify as unverified rather than stating it as settled — the value of this file depends
on being trustworthy without re-checking.

Last verified: 2026-09-30.

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

### Google Ads Transparency: attempted, not working (2026-09-18)

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

The conclusion is that the call needs session state the page carries and a bare POST does
not — most likely cookies or a per-session token beyond the API key. Cracking that would
mean either driving a real browser (Playwright is available) and reading the request off
the network tab, or reverse-engineering the obfuscated bundle. Neither was judged worth
the budget at the time. Until it is solved, **there is no confirmed-current-spend signal
in this database**, and `marketing_active` stands alone with the meaning above.

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

## Contact Email Extraction

The deliverable of the pipeline is **contact email addresses** — a scored prospect database with no contactable recipients is incomplete. Emails are extracted from four channels and stored in `leads.business_email` (one row per unique address per business), with a rolled-up best address per business cached on `leads.businesses.contact_email` for fast export.

### Channel 1: Website Crawl Text + Mailto: Links
**Status:** Verified complete (2026-09-21)

`scripts/extract-emails.mjs` reads `leads.website_crawl.text_excerpt` (stripped page text) and `signals->person_emails` / `signals->role_emails` (extracted from raw HTML by detectPage() in site-signals.mjs). The mailto: channel recovers addresses that appeared only as `<a href="mailto:erin@x.com">Email us</a>` — toText() strips tags before text_excerpt is written, so those addresses would be lost without reading signals.

**Results (verified 2026-09-21):**
- **8,269 crawled pages** scanned across 3,349 businesses
- **3,018 addresses** extracted and verified
- **2,145 businesses** with ≥1 address
- **5,870 pages** contributed a mailto: link from signals
- Distribution: 1,971 personal emails (michael@), 1,047 role emails (info@), 1,622 on own domain, 928 free-mail (gmail)
- **1,799 rejected** as placeholder local parts (filler@godaddy.com, etc.), **132 as infrastructure domains** (registrar nameservers)

Precision is prioritized over recall throughout; a junk address in a send list costs sender reputation while a missed address may be recovered on the next crawl.

### Channel 2: RDAP Domain Registrant
**Status:** Verified, low yield

`leads.domain_rdap` stores one-time RDAP (WHOIS replacement) lookups per domain. Post-GDPR redaction is common (~62% of answers carry no contact info). Of 92 responses in a measured sample, 7 carried a usable contact address (3–4% yield).

**Results (verified 2026-09-18, from architecture doc):**
- **45 addresses** extracted from domain registrants
- Used as secondary signal, distinct from website-published addresses

### Channel 3: Form Submission
**Status:** On hold (2026-09-21)

242 businesses publish a contact form and no email address. Submitting forms is risky without proper bot detection and CAPTCHA handling — honeypot and CAPTCHA triggers are unacceptable using owner credentials and business name.

**Plan:** Refactor `scripts/submit-forms.mjs` (with `scripts/lib/forms.mjs`) using Playwright for proper browser automation. Alternative: evaluate scrapemate/Go extractor that can utilize current infrastructure. Decision pending.

### Address Verification: `scripts/mxCheck.pl`

Extraction finds addresses; it does not establish that they still receive mail. `scripts/mxCheck.pl` closes that gap by asking each address's own mail server whether the mailbox exists, without sending anything.

Per address it resolves the domain's MX record, opens an SMTP session to the lowest-preference host, issues `MAIL FROM` / `RCPT TO`, and disconnects at `QUIT`. **`DATA` is never sent**, so no message is delivered and the mailbox owner observes only a connection.

The critical part is the **catch-all guard**. Many mail hosts accept `RCPT TO` for every local part at their domain, which would make a naive probe report every address as valid. Before testing the real address the script offers a random local part at the same domain; if that is accepted, the host is a catch-all and the result is recorded as a failed check (`false positive check failed for mx ...`) rather than as a verification. This is why verified counts from this tool are trustworthy but conservative — catch-all domains are reported as unknown, not as valid.

Concurrency is `Parallel::ForkManager` (default 30 workers). Each child builds **its own** `Net::DNS::Resolver`; a resolver constructed before the fork would share one UDP socket across all children and misattribute replies between domains.

```
./scripts/mxCheck.pl --email owner@example.com
./scripts/mxCheck.pl --file addresses.txt --threads 30
./scripts/mxCheck.pl --file addresses.txt --debug              # trace to STDERR
./scripts/mxCheck.pl --file addresses.txt --rate-limit 5        # cap starts/sec
./scripts/mxCheck.pl --file addresses.txt --socks5-proxy host:port
```

Output is a JSON array on STDOUT: `{email, verified, mx_server, error}` per address. `--debug` writes an execution trace to STDERR only, so it is safe to use while piping results. Every DNS and SMTP call is announced by a `NET>` line **before** the call is made, paired with a `NET<` line carrying the outcome and elapsed milliseconds; each line is stamped with elapsed time and pid, which is what makes an interleaved 30-worker trace readable. The `NET>`-before-the-call ordering is deliberate: a trace must be able to answer "what was it about to talk to when it hung."

**`--rate-limit N`** caps checks *started* to N per second, summed across all workers, enforced in the parent's dispatch loop (there is no cheap way to share a budget across already-forked children). `--threads` still bounds concurrency; `--rate-limit` bounds how fast new connections begin. A burst of RCPT TOs from one IP looks like directory harvesting to a receiving mail server and is what gets a sending IP blocklisted — this exists to avoid that on a bulk run.

**`--socks5-proxy host:port`** routes the SMTP TCP connection (never the DNS MX lookup, which always resolves directly) through a SOCKS5 proxy. Credentials come from `MXCHECK_SOCKS5_USER` / `MXCHECK_SOCKS5_PASS` in the environment, never from a CLI flag — a password on the command line is visible to every local process via `ps aux` and lands in shell history, the same class of leak that put a live Postgres password in a public repo earlier in this project. `--socks5-user`/`--socks5-pass` exist only for local testing and print a loud warning when used.

The proxied path doesn't use `Net::SMTP->new()`, because `Net::SMTP` has no hook for handing it an already-open socket. Instead the tunnel is opened directly with `IO::Socket::Socks`, then the connected handle is re-blessed into `Net::SMTP` and its connection bookkeeping (`net_smtp_arg`, `net_smtp_host`, the banner read, `HELO`) is redone by hand — mirroring exactly what `Net::SMTP::new()` does after its own TCP connect succeeds. `IO::Socket::Socks::Wrapper`, the usual way to make `Net::SMTP` proxy-transparent, was tried and rejected first: 3 of its own 57 test-suite assertions fail on this workstation, and it produced "Bad file descriptor" against a re-blessed `Net::SMTP` object in testing rather than a working connection.

**Reverse DNS lookup for HELO hostname** — the `HELO` parameter sent to the mail server is set via a reverse DNS lookup on the outgoing IP address, not `Sys::Hostname`. This ensures the hostname presented to the receiving mail server matches its own reverse-DNS view of the connecting IP, which improves authentication signals and reduces spam filter false positives. The lookup is best-effort — if PTR resolution fails, the script falls back to the system hostname. For direct SMTP connections, if the reverse DNS differs from the initial HELO, the connection is dropped and re-established with the correct hostname; for SOCKS5 tunneled connections, the correct HELO is determined before the `HELLO` handshake. Debug mode (`--debug`) emits a `NET>` line before the PTR query and a `NET<` line with the result.

**Proxy reality check, measured 2026-09-21** — worth knowing before pointing this at anything:
- The project's Webshare pool (`PROXY_LIST_URL` in `.env`) refuses the CONNECT for ports 25, 465 and 587 outright ("Not allowed" — a proxy-side ACL, not a network failure). It works fine for 80/443/8080. **It cannot be used for SMTP verification as currently configured.**
- All three scraper hosts (`worker`/`worker2`/`worker3.accurateleadinfo.com`) also cannot reach port 25 outbound at all, proxy or no proxy — standard hosting-provider anti-spam policy, confirmed directly with `/dev/tcp/smtp.google.com/25` from each host. An `ssh -D` tunnel to any of them inherits that block.
- Only this workstation's own IP has been confirmed to reach port 25 today.
- `--socks5-proxy` itself was verified working end-to-end (full SMTP session, catch-all probe, real-address RCPT) through an `ssh -D` loopback tunnel on this workstation — the mechanism is solid — but not against anything already in this project's proxy inventory. A working target (a VPS configured to permit outbound 25, or a commercial proxy that doesn't filter mail ports) is still needed before this flag does anything useful in production.

CPAN dependencies (all installed on the workstation): `Net::DNS`, `Net::SMTP`, `Parallel::ForkManager`, `Try::Tiny`, `JSON::PP`, `IO::Socket::Socks`.

```
cpanm Net::DNS Net::SMTP Parallel::ForkManager Try::Tiny JSON::PP IO::Socket::Socks
```

Three further modules — `Net::DNS::Async`, `URI::Encode`, `Coro::AnyEvent` — were specified for this script but are **deliberately not loaded**, because the current implementation has no use for them: resolution is synchronous inside forked children and there are no URLs to escape. They become real dependencies only if concurrency moves from process forking to an event loop. Of the three, `Coro::AnyEvent` is the one not currently installed here, and `Coro` is the usual source of build trouble on a recent perl — worth knowing before attempting that refactor.

Verdicts are stored in `leads.email_verification` (`db/migrations/008_email_verification.sql`, applied), one row per `business_email_id`, with a rolled-up `email_verified_count`/`email_unverifiable_count`/`email_rejected_count` cached on `leads.businesses` following the same evidence-table-plus-cache pattern as `business_email`.

**Not yet run at scale.** Every code path (direct SMTP, catch-all probe, `--rate-limit` pacing, `--socks5-proxy` tunneling, connect failures) is verified working individually; it has not been run across the 3,063 addresses in `leads.business_email`, and no script yet reads `business_email`, shells out to `mxCheck.pl`, and upserts the JSON output into `leads.email_verification` — that loader still needs to be written. A bulk run makes ~3,000 outbound SMTP connections from one IP, which some providers rate-limit or blocklist — pace it with `--rate-limit`.

### Current Email Coverage

| Metric | Value |
|---|---|
| Businesses with ≥1 email | 2,190 of 3,888 (56%) |
| Total addresses in business_email | 3,063 |
| Avg addresses per business | 1.4 |
| Source breakdown | 3,018 crawl, 45 RDAP |
| Tier 1+2 with email | 790 of 1,417 (56%) |
| Tier 1+2 with email + decision maker | 180 (13%) |

The email table is authoritative; `businesses.contact_email` is a cache of the best address per business, rewritten by extraction scripts and never edited directly.

---

## Second crawler: Jina, for the sites curl cannot reach (2026-10-03)

`flow/jina_crawl.py` fetches the same business websites through `r.jina.ai` and writes
`leads.website_crawl_jina`. It reuses `flow/fetch.py`'s `fetch_html` unchanged, which is
why the module is small; everything else in it is target selection, response parsing and
the upsert.

**Why a second crawler.** `scripts/enrich-websites.mjs` fetches raw HTML with curl
through the Webshare SOCKS5 pool. Measured 2026-10-03: of **54,150** eligible businesses
(website present, not a social URL, not `qc_status = 'REJECTED'`), **30,713 — 57% — have
no usable `text_excerpt` at all**, and a further 867 have text for some pages but an
error or non-2xx/3xx status on others. That shortfall is overwhelmingly the class curl
cannot get: JS-only sites, WAF challenges, and hosts that refuse datacenter ASNs. Jina
runs a real browser from its own addresses and returns rendered markdown.

**Default target set is that shortfall, not the database.** `--mode gaps` (the default)
selects businesses with no usable crawl text *or* with a crawl row carrying an error or a
bad status. `--mode all` selects every eligible business and exists only for a deliberate
side-by-side comparison. Either way, businesses this crawler already has text for are
excluded unless `--refetch` is passed, so a re-run costs nothing for what it has.

**Why a separate table.** Jina returns rendered markdown from a headless browser; the mjs
crawler returns tag-stripped HTML. They are not the same kind of text. Writing both into
`leads.website_crawl` would make `text_excerpt` mean two things and silently change what
every downstream reader quotes from — `extract-emails.mjs`,
`enrich-decision-makers.mjs`, `flow/agents/selector.py`. The table is a mirror of
`website_crawl` minus `content_bytes`: the response is JSON, so a byte count would
measure Jina's envelope rather than the site. `signals` holds the response object minus
`content` (stored once, in `text_excerpt`) — kept whole because `links` carries `mailto:`
addresses the markdown body does not, and `usage.tokens` is how the API budget is
accounted for.

**A 200 is not evidence of a page.** Observed on the first live run: a GBP `website`
pointing at an ad-click URL resolved through `match.adsrvr.org`, and Jina rendered the
tracker — HTTP 200, 37 characters, the redirect target echoed as the entire body. Stored
as-is that reads downstream as usable page text. Content under `MIN_CONTENT_CHARS` (300)
is therefore recorded as a `fetch_error`, not as a crawl. Three of the first eight
businesses tripped it; the five real pages in the same batch returned 10k–19k characters
each, so the floor is nowhere near them.

**Verified locally 2026-10-03.** 165 unit tests pass. Live: 12 rows written, 9 with text
averaging ~13,900 characters, 3 recorded as too-short failures. A second run over the
same `--limit 8` re-selected only the failures, confirming resumability. Token cost ran
~2,700–5,300 per page (`JINA_API_KEY` is rate-limited to 500 req/min and 100k
tokens/min, so `--concurrency` above ~8 will start hitting the token ceiling, not the
request one).

**Link extraction.** Jina returns a `data.links` map of display text → href pairs.
`flow/jina_crawl.py` extracts all of them into `leads.website_crawl_jina_link`, keyed
on (business_id, url, link_href) so a re-crawl of the same page does not duplicate.
Includes mailto:, https://, tel:, and other schemes. Observed on first live run: 38 links
extracted from 1 page (1 mailto, 36 https, 1 tel), compared to a handful (if any) per
page when relying on tag-stripped HTML. The `links` map complements the rendered markdown
body — addresses that appeared *only* as `<a href="mailto:...">Email us</a>` are visible
here where the stripped text alone would lose them.

**Status: development.** Nothing downstream reads `website_crawl_jina` or its links yet.
Deciding whether it supersedes, feeds or merges into `website_crawl` needs a yield
comparison on the gap set first. Email extraction from the links map is the natural
first downstream consumer.

---

## Open items / unverified

- The fleet-aware watchdog (commit `da4e4e6`) is **committed but not deployed**. The
  live copy on the database host is still the stubbed version (no `SCRAPER_SSH_HOSTS`,
  no `worker_states`, `TODO` intact). Deploying it also requires adding the
  `SCRAPER_SSH_HOSTS` array to that host's `.env` — see above. The unit files and an
  idempotent installer now live in `deploy/`, so the remaining work is a `git pull` plus
  `./deploy/install-watchdog.sh` on that host; the installer's preflight checks the
  `.env` prerequisite rather than letting it fail silently.
- `scripts/enrich-decision-makers.mjs` **executed successfully** (2026-09-21). Extracted
  425 decision makers (medium+ confidence) across 2,901 crawled businesses in 9.5s. All
  four `decision_maker_*` columns updated. Precision measured at ~97% on high-confidence
  rows (315 of 425). Coverage: 16% yield across all service categories, 30% on Tier 1
  dog_training trainers.
- Whether the local workstation timer should keep running is undecided. It duplicates
  the remote one into the same table; if the remote deployment is canonical, the local
  timer is arguably redundant and could be disabled to make the tick log single-writer.

## Inbound webhooks: Warmbly → host process behind traefik (2026-09-30)

Warmbly delivers events to a FastAPI/uvicorn process running **on the host** (not in a
container) on `accurateleadinfo.com`, fronted by the traefik container from the
`~/n8n-compose` stack.

- **`~/leads` on the prod host is a symlink to `~/src/git/pipeline/`.** Editing a file
  locally is not deploying it — an earlier session lost many turns debugging an
  environment variable that was in fact reaching unmodified remote code. The deployed
  file is `~/src/git/pipeline/flow/warmbly_http_endpoint.py`. A **stale duplicate**
  exists at `~/src/git/pipeline/flows/` (plural) and should be deleted.
- The prod interpreter is **Python 3.9**, so PEP 604 unions (`str | None`) crash at
  import; use `Optional[str]`. PEP 585 generics (`dict[str, Any]`) are fine.
- The `~/n8n-compose` stack has been **repurposed** — n8n itself is gone. It now runs
  postgres (`2345:5432`), adminer (`8081:8080`) and traefik (`80`, `443`).
- **Warmbly's `safehttp` SSRF guard rejects non-web ports.** Only 80 and 443 are
  permitted. The UI reports this as "destination address is not publicly routable",
  which is misleading; the real reason (`blocked request to a non-web port`) appears only
  in the `warmbly-backend-1` container log. This is why the endpoint must sit behind
  traefik rather than being exposed directly on 8765.
- **Container → host networking:** `127.0.0.1` inside the container is the container's
  own loopback, and `host.docker.internal` does not resolve on this Linux server Docker.
  The Docker bridge gateway (`172.20.0.1`) works.
- **Traefik's file provider cannot proxy to a Unix socket** — `unix://…` is rejected as a
  `loadBalancer.servers[].url`, even with the socket bind-mounted and world-writable.
  uvicorn serves it fine (`uvicorn_run(app, uds=…)`); traefik is the limitation.
- **A router needs a `Host()` matcher, not just `Path()`.** Without one, ACME never binds
  a certificate to the hostname and traefik serves `CN=TRAEFIK DEFAULT CERT`.
- **`docker compose restart` does NOT apply a changed `command:`.** It restarts the
  existing container with its existing arguments; verified by `docker inspect … .Config.Cmd`
  still showing the old `--providers.file` flags after a restart. Recreating the container
  with `docker compose up -d traefik` is required.
- Webhook verification is **challenge/response**: a `webhook.test` event carries
  `data.challenge` (`whcg_…`) and must be echoed back in the body and/or the
  `X-Warmbly-Webhook-Challenge` header. Handled by `route_payload()` /
  `detect_challenge()`; header and field names come from `WARMBLY_CHALLENGE_HEADER` and
  `WARMBLY_CHALLENGE_FIELD` (defaults match the values above).
- **Warmbly signs webhooks Stripe-style, not as `sha256=<hex>`.** Observed 2026-10-03:

      X-Warmbly-Signature: t=1791037655,v1=eccdf01d…

  `t` is a unix timestamp and `v1` is an HMAC-SHA256 hex digest. The timestamp is
  carried *inside* the signature header, not as a separate header. An earlier
  implementation assumed `<algorithm>=<digest>` and rejected every request with
  `algorithm is 't', expected 'sha256'` — a 401 that looked like a wrong secret.
  Element keys are configurable via `WARMBLY_SIGNATURE_TIMESTAMP_KEY` and
  `WARMBLY_SIGNATURE_VERSION_KEY`; multiple `v1=` elements are accepted so a secret
  rotation does not break delivery.
- **The signed bytes are believed to be `<timestamp>.<raw body>`** (separator from
  `WARMBLY_SIGNATURE_SEPARATOR`, default `.`). This is inferred from the header's
  resemblance to Stripe's scheme and has **not** been confirmed against a known-good
  secret. `probe_signature_schemes()` tests the alternatives (`t.body`, `t+body`, raw
  body; hex and base64) on every rejection and names the form that matches, so a wrong
  guess here self-corrects in the log rather than presenting as a bad secret. The raw
  received bytes must be hashed — re-serialising the parsed JSON changes the digest.
- **Replay rejection is opt-in.** `WARMBLY_SIGNATURE_MAX_AGE` (seconds) enables it;
  unset, the signature age is logged but never rejected. Enabling it converts clock
  drift into 401s, so it is deliberately off by default.
- Rejections always log a structured reason; `WARMBLY_WEBHOOK_DEBUG=1` additionally logs
  every request header, with credential headers redacted per
  `WARMBLY_REDACTED_HEADERS`. Digests in logs are truncated to
  `WARMBLY_SIGNATURE_PREVIEW_CHARS` (default 12) so a log cannot be used to replay.
- The webhook process is a **bare foreground process in an interactive shell**, not a
  managed service; it will not survive a reboot or a closed terminal.

### Warmbly webhook → Prefect, verified locally 2026-10-03

The listener no longer prints and drops events: it emits a Prefect event and the
deployment picks it up.

```
Warmbly (htpc) --HTTPS--> traefik --> flow/warmbly_http_endpoint.py  (uvicorn)
    signature check -> challenge echo (synchronous, no event)
    real event -> emit_event("warmbly.webhook.received")
        --> Prefect automation (matches on event NAME alone; match: {})
            --> warmbly-webhook-receiver deployment  --> flow run
```

- **The event name is the whole routing key.** It lives in `flow/warmbly_events.py`,
  imported by both the emitter and the deployment's trigger so they cannot drift, and
  is overridable via `WARMBLY_WEBHOOK_EVENT_NAME`. Automations created from
  `to_deployment(triggers=[...])` have an empty `match`, so any event with the same name
  fires every consumer subscribed to it.
- **`serve()` and the listener must run on the same host.** `emit_event` goes to
  whichever server the listener's `PREFECT_API_URL` resolves to, and the serving process
  only runs work from the server it polls. Split them and the listener reports
  `"emitted": true` while nothing ever runs.
- **Both modules are started directly as scripts, which breaks absolute imports.**
  `python flow/x.py` puts `flow/` on `sys.path`, not the repo root, so
  `from flow.warmbly_events import ...` raises `ModuleNotFoundError: No module named
  'flow'`. Both files now insert the repo root into `sys.path` at the top so the script
  and `python -m flow.x` forms both work. `tests/test_scripts_runnable.py` pins this,
  because the failure is invisible under pytest (which adds the rootdir itself).
- **Emission failure is not an HTTP failure.** If Prefect is unreachable the listener
  logs the exception and still returns 200 with `"emitted": false`, because Warmbly
  retries non-2xx and eventually disables the endpoint. An orchestration outage must not
  cost the delivery channel.
- **C0 control characters are stripped before emitting.** A NUL would abort the Postgres
  transaction Prefect persists the event into; tab, LF and CR are preserved.
- **Calling a Prefect `@flow` directly creates a real flow run** against
  `PREFECT_API_URL`. Unit tests therefore exercise `warmbly_webhook_receiver.fn`, the
  undecorated function. Note that `tests/test_warmbly_integration.py` (pre-existing) does
  *not* do this and does write flow runs into the live server when the suite runs.
- `prefect_test_harness` is **not usable here**: its temporary server returns 500 on
  `/api/admin/version` under Python 3.15 with Prefect 3.2.15.

**Verified locally only.** Deployment to `accurateleadinfo.com`, and verification against
real Warmbly traffic, have **not** been done. The prod Prefect server still has zero
deployments.

### AI node deployments, verified locally 2026-10-03

`research-agent` and `drafting-agent` are Prefect deployments, served together by
`flow/serve_agents.py` (run: `python flow/serve_agents.py`). Each is independently
runnable, pausable and rate-limitable in the UI, which is the point of splitting them.

- **LLM calls go to the self-hosted LiteLLM proxy on `:4000`** via `httpx`, asking for
  `response_format={"type":"json_schema"}` and validating with Pydantic
  (`flow/llm.py`). The `litellm` Python package and `instructor` are not used and not
  installed. The proxy **401s without `LITELLM_API_KEY`**; the nodes surface that as
  `LLMTransportError` and return a rejection rather than crashing.
- **`research-agent` owns its own escalation** — pass 1, confidence gate
  (`CONFIDENCE_THRESHOLD`, default 0.7, `>=` passes), `deeper_fetch`, pass 2, second
  gate. Its caller never drives that.
- **`drafting-agent` does not enforce the verified pool.** Warmbly will not send to an
  address it has not itself verified and has its own verification, so the pool is an
  input and a prompt steer. Out-of-pool selections come back in `outside_known_pool`
  for observability.
- **Concurrency limits** are set per-deployment when registered to a work pool
  (via `PATCH /api/deployments/<id> {"global_concurrency_limit": N}`). The `serve()`
  development mode does not support work-pool-specific configuration.
- Unit tests verified (20 total passing): research-agent tests 8/8, drafting-agent
  tests 12/12. All pool-reporting behavior verified.
- `tests/test_warmbly_contacts.py` verified: 23/23 tests passing (verified 2026-10-04).
- `tests/test_warmbly_integration.py` requires `PREFECT_API_URL` to be set and does not
  set it; without it Prefect starts an ephemeral server that returns 500 under this
  Python (the same breakage that makes `prefect_test_harness` unusable).

**Verified locally only.** Deployments are created, tested and ready; nothing is
deployed to `accurateleadinfo.com` yet.

## Discord bot: Warmbly event feed + slash commands (2026-10-04)

`flow/discord_bot.py` holds the async flow `discord-bot`, served by
`flow/serve_discord_bot.py`. A single long-running flow run shares one event loop
between a discord.py client and the Warmbly `AsyncGatewayClient`.

- **Event feed.** The flow subscribes to `org:$WARMBLY_ORG_ID` on
  `$WARMBLY_WEBSOCKET_URL`. The `/socket/websocket` suffix is stripped because the SDK
  appends it. The SDK has no wildcard handler, so the bot registers one handler per
  `GatewayEvent` constant whose value is UPPER_CASE (53 business events). The
  lowercase constants (`presence_state`, `presence_diff`, `rate_limited`, `resumed`,
  `resume_failed`) are transport frames and are deliberately **not** forwarded:
  presence is people opening and closing the CRM dashboard, and an API-key client
  joining does not generate it (verified 2026-10-05). Events that are not named in
  the SDK are not forwarded either.
- **Event families.** Optional `$WARMBLY_EVENT_INTENTS`, e.g. `EMAIL,CAMPAIGN,MEETING`,
  is passed as `intents` on the `org:` subscribe. The server keeps an event if any
  token is a substring of its upper-cased name. There is no exclusion syntax. Unset
  means everything. Intents do not filter presence frames, which is why those are
  dropped client-side.
  Every event is posted to `$DISCORD_EVENT_CHANNEL_ID` in arrival order through a
  single queue.
- **Display filter.** `EventDisplayFilter` sits between the gateway handlers and the
  queue. It renders each event as a bold event name, the topic, and the payload as
  pretty-printed JSON inside a ```` ```json ```` code block, truncated to fit
  Discord's 2000-character limit. Literal triple backticks in the payload are broken
  with a zero-width space so they cannot close the block. Every occurrence of a known
  organization id, in the topic or anywhere in the payload, is replaced with the
  organization's name.
- **Org name.** The name is fetched once at startup from
  `GET $WARMBLY_API_URL/v1/me`, which returns `organization_id` and
  `organization_name` for any valid key. `GET /v1/organization` exists (unknown
  sibling paths return 404) but answers **401 "Invalid or expired token"** to API keys
  that `/v1/me` accepts. That matches the CLI's note that org endpoints are
  session-only, and `warmbly org view` itself calls `/v1/me` (verified 2026-10-05).
  If the lookup fails, the bot logs it and posts events labelled with the raw id.
- **`/batchdraft [n]`** calls `run_deployment($BATCHDRAFT_DEPLOYMENT, batch_size=n or 50,
  timeout=0, as_subflow=False)`. The default deployment is `run-agents/run-agents`, and
  `serve_agents.py` registers it only when `SERVE_RUN_AGENTS=1`.
- **`/auth`** runs `warmbly auth login --web --force --hostname <host> --api-url
  $WARMBLY_API_URL`. The host is `$WARMBLY_API_URL` with the `api.` prefix removed. The
  CLI uses an OAuth **device flow**: it prints `Your code:` and `Approve at: <url>`,
  then polls for up to 10 minutes. The bot sends that URL to the invoker as an
  ephemeral message. Credentials go to `~/.config/warmbly/hosts.yml` of the user the
  worker runs as.
- **`/<group> args`** registers one slash command per `warmbly` command group, parsed
  from `warmbly --help` at startup (30 groups as of CLI 2026-10). `args` is
  shlex-split, the command runs with no shell and stdin closed, and output is
  ephemeral. `auth`, `browse`, `completion`, `help`, `upgrade` and `events` are
  excluded.
- **`/list`** shows Prefect flows and their deployments from `get_client()`.
- **Reload without restart.** The event feed's credential is
  `warmbly auth token` (the CLI's active sign-in), falling back to
  `$WARMBLY_API_TOKEN`. A successful `/auth`, the `/reload` command, or `SIGHUP`
  closes the current gateway connection and opens a new one with whatever token is
  active at that moment, and re-resolves the org name. The Discord connection,
  the event queue and the announcement are untouched, so events already queued are
  not lost and the bot does not re-announce. A reload request also cuts short the
  30s retry wait after a fatal gateway error (e.g. a revoked key), so `/auth` fixes
  a dead feed immediately.
- **SIGHUP under Prefect.** A served run is its own `python -m prefect.engine`
  process. It imports the flow module on the **main thread**, but an *async* flow body
  runs on Prefect's `RunSyncEventLoopThread` via `run_coro_as_sync`, where
  `signal.signal` raises ValueError. The handler is therefore installed at import
  time on a module-level `RELOADER`, and the flow attaches its event loop to it;
  delivery uses `call_soon_threadsafe`. The run logs `kill -HUP <pid>` at startup.
  Prefect itself handles only SIGTERM, in the runner parent, so it does not claim
  SIGHUP (verified against prefect 3.2.15, 2026-10-05).
- **Announcement.** When Discord is ready, the bot posts an introduction to the
  channel listing the event source, the built-in commands and the CLI commands. It
  goes out once per flow run, before any queued events; automatic reconnects do not
  repeat it. If the post fails, the error is logged and the event feed keeps running.
- **Access.** Only members holding the role named by `$DISCORD_ADMIN_ROLE`
  (`CRMadmin`) can use any command. The name must match exactly. The check lives in
  `CommandTree.interaction_check`, so it covers every command, including CLI groups
  discovered at startup. Anyone else gets a private refusal and nothing runs. DMs
  carry no roles and are refused. Commands have no Discord-side
  `default_permissions`, so they are visible to everyone; to hide them as well, set
  per-role overrides under Server Settings > Integrations.
- **Env vars.** These already existed: `WARMBLY_API_TOKEN`, `WARMBLY_WEBSOCKET_URL`,
  `WARMBLY_ORG_ID`, `WARMBLY_API_URL`. These are new: `DISCORD_BOT_TOKEN`,
  `DISCORD_CHANNEL_ID` (announcement), `DISCORD_EVENT_CHANNEL_ID` (event feed),
  `DISCORD_ADMIN_ROLE`, optional `DISCORD_GUILD_ID` (instant guild sync; global sync
  can take up to an hour to appear) and optional `BATCHDRAFT_DEPLOYMENT`.
- **Unverified.** The bot has not connected to Discord, because no bot token exists
  yet. The gateway feed is still subject to the realtime UUID bug in the private
  notes: JOIN succeeded, but whether broadcasts arrive afterwards was never confirmed.
  The concurrency setup below was verified against the live server.

**Do not name a file in `flow/` after a package it imports.** When Prefect runs a
deployment, it puts the entrypoint's directory first on the import path. A file
`flow/warmbly.py` therefore shadowed the `warmbly` SDK, and the first served
`discord-bot` run failed with `No module named 'warmbly.gateway'; 'warmbly' is not a
package`. The unit tests passed anyway because they import the module as
`flow.discord_bot`. The file is now `flow/warmbly_contacts.py`, and
`tests/test_discord_bot.py::TestDeploymentImportPath` reproduces that path layout.

## LLM call timeouts (2026-10-05)

Every agent's LLM call goes through `flow/llm.py::_request`. It has two timeouts:

- `LLM_REQUEST_TIMEOUT` (default 120s) is httpx's own timeout. It bounds each
  connect and socket read, **not the whole call**. A proxy that accepts a request and
  then trickles bytes, or never answers, can keep a call open indefinitely.
- `LLM_CALL_TIMEOUT` (default 180s) caps the whole call. `httpx.post` runs on a
  **daemon** thread and the caller waits at most this long. When the cap is hit, the
  call raises `LLMTransportError` ("no reply from proxy … within LLM_CALL_TIMEOUT"),
  which the agents already handle; it is not retried. A daemon thread is used rather
  than a `ThreadPoolExecutor` because an executor's worker blocks interpreter exit
  until the stuck request ends (measured: 20s vs 0.5s).

How this was found: commit 22f8636 added `analysis_agent` to `run_agents` without
updating `tests/test_run_agents.py`. Nine tests then sent real requests to the
LiteLLM proxy on :4000, and the full suite hung indefinitely. The test file now has
an autouse fixture that stubs `analysis_agent`. The suite passes with
`LITELLM_BASE_URL=http://127.0.0.1:9`, which proves no unit test reaches the proxy.
Those runs also overwrote the tracked `analysis-*-output.json` files.

Do not `importlib.reload(flow.llm)` inside the test session. A reload creates new
`LLMTransportError`/`LLMSchemaError` classes, and agents that imported the old ones
stop catching them in every later test. Read module-level settings in a subprocess
instead.

## discord-bot: never wait on a concurrency slot (2026-10-06)

The deployment allows one run at a time, because a second run would post every
Warmbly event twice. It now uses **`CANCEL_NEW`** instead of the default `ENQUEUE`: a
start that cannot get the slot is cancelled at once, rather than sitting in
`Scheduled/AwaitingConcurrencySlot` indefinitely. `to_deployment` stores this as
`concurrency_limit=1` plus `concurrency_options.collision_strategy`.

**Start the bot with `python flow/start_discord_bot.py`**, not
`prefect deployment run`. The launcher calls `flow/discord_bot_reaper.reap()` and
then triggers one run without waiting for it. The reap step:

1. Treats every other `discord-bot` run in Running, Pending or Cancelling, or
   Scheduled as `AwaitingConcurrencySlot`, as a blocker.
2. If a blocker's tags say it runs on this host (`host:<name>`, `pid:<n>`) **and**
   `/proc/<pid>/environ` contains `PREFECT__FLOW_RUN_ID=<that run>`, sends it SIGTERM,
   then SIGKILL after 10s. A recycled PID or a process on another host is never
   signalled. The tags exist because the runner leaves `flow_run.infrastructure_pid`
   empty.
3. Forces the blocker to Cancelled. Leaving Running, Pending or Cancelling is what
   makes the server release the slot (`ReleaseFlowConcurrencySlots` in
   `server/orchestration/core_policy.py`), and a forced transition counts.
4. If the limit still shows a slot held, resets `active_slots` with
   `update_global_concurrency_limit`: to 0 from the launcher, or to 1 from inside a run
   (that slot is the run's own). Slot decay is 0.0, so a leaked slot never expires
   by itself.

The flow also runs `claim_and_reap()` first thing: it tags itself with its PID and
host, then reaps other runs. That second layer covers starts that bypass the
launcher. A failure there is logged and the bot carries on.

**Why the launcher is needed:** with `CANCEL_NEW`, a run that finds the slot held is
cancelled at the slot check, before any of its own code runs. So it can never reap
the run that blocks it; only something outside the run can.

**Verified live 2026-10-06** with a throwaway deployment configured the same way: an
abandoned Running run held 1 slot; a new run was cancelled with "Deployment
concurrency limit reached"; `reap` cancelled the stale run and the server dropped
the count to 0 without needing the reset; a fresh run then took the slot. The live
`discord-bot` deployment still shows `concurrency_options=None` until
`serve_discord_bot.py` is restarted.
