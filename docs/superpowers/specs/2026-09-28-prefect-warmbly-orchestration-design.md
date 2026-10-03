# Prefect-Warmbly Orchestration: Contact Research & Campaign Drafting

**Date:** 2026-09-28  
**Status:** Design (ready for implementation planning)  
**Scope:** Prefect service orchestrating contact research, email validation, and agentic campaign drafting; integration with Warmbly CRM for campaign import and Discord notification.

---

## 1. Overview & Intent

Warmbly's native automations are minimal — tagging and simple workflows only. This Prefect service owns the sequencing logic for anything more complex: **multi-step contact research, confidence-gated enrichment, and LLM-driven campaign drafting.**

The pipeline transforms a batch of qualified business prospects into research-backed, personalized email drafts ready for human review and campaign launch in Warmbly. It operates as a scheduled or event-triggered flow (default: cron batch), processes a configurable number of contacts per run, and surfaces rejections and drafts to you via Discord + Warmbly for review before anything sends.

### Core Principle

**Confidence gates belong in Prefect, not in prompts.** A low-confidence research pass is a recordable outcome (rejected to audit pool), not a model instruction to self-censor. This keeps quality auditable and tunable without re-prompting.

---

## 2. Pipeline Architecture

### 2.1 Stages

```
┌─────────────────────────────────────────────────────────────────┐
│ stage 1: candidate_selector (SQL, deterministic)                │
│ • Query leads.businesses for businesses meeting selection       │
│   criteria (e.g., tier 1–2, email exists, new in last 24h)      │
│ • Pull BATCH_SIZE records (configurable, default 50)            │
│ • Output: list of business records with crawl context           │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ stage 2: email_validation (mxCheck.pl wrapper task)             │
│ • Input: business records with leads.business_email + any       │
│   prior research-suggested addresses                            │
│ • Call mxCheck.pl via subprocess:                               │
│   perl scripts/mxCheck.pl --file <temp> --threads 10            │
│   --rate-limit 30 --socks5-proxy [if configured]                │
│ • Parse JSON array output: {email, verified, mx_server, error}  │
│ • Output: verified_email_pool per business                      │
│ • Reject entire business if no verified emails found            │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ stage 3a: research_agent pass 1 (LLM via LiteLLM)               │
│ • Input: business record + crawl excerpt + verified_email_pool  │
│         + ICP profile + offer context                           │
│ • Model: LiteLLM client (swappable, default: local Ollama)      │
│ • Output: ResearchOutput schema (pydantic-validated)            │
│ • Structured output: pain signals, tone, confidence score,      │
│   suggested lead email, optional next_url_to_check              │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ stage 3b: confidence_gate (Python if/else)                      │
│ • Threshold: CONFIDENCE_THRESHOLD env var (default 0.7)         │
│ • If confidence >= threshold: pass to drafting_agent             │
│ • If confidence < threshold: attempt deeper_fetch               │
└──────────────────┬──────────────────────────────────────────────┘
       ├─ below   ▼
       │ ┌─────────────────────────────────────────────────────────┐
       │ │ stage 2b: deeper_fetch (Jina Reader via flow/fetch.py) │
       │ │ • Input: next_url_to_check from research_output        │
       │ │ • Call: flow.fetch_html(url) → fetch via Jina API      │
       │ │ • Output: markdown content from URL                    │
       │ └──────────────────┬──────────────────────────────────────┘
       │                    │
       │                    ▼
       │ ┌─────────────────────────────────────────────────────────┐
       │ │ stage 3a2: research_agent pass 2 (LLM, augmented)       │
       │ │ • Input: business record + original crawl + new fetch   │
       │ │         + verified_email_pool + ICP + offer             │
       │ │ • Output: ResearchOutput (2nd attempt)                  │
       │ └──────────────────┬──────────────────────────────────────┘
       │                    │
       │                    ▼
       │ ┌─────────────────────────────────────────────────────────┐
       │ │ stage 3b2: confidence_gate (2nd check)                  │
       │ │ • If confidence >= threshold: pass to drafting_agent    │
       │ │ • If still below: reject to leads.agent_rejects         │
       │ └──────────────────┬──────────────────────────────────────┘
       │                    │
       └────────┬───────────┘
                │ at/above threshold
                ▼
┌─────────────────────────────────────────────────────────────────┐
│ stage 4: drafting_agent (LLM via LiteLLM)                       │
│ • Input: research_output + verified_email_pool + template slug  │
│         + offer context                                         │
│ • Model: LiteLLM client (swappable, default: local Ollama)      │
│ • Agent decides which verified email(s) to write to based on    │
│   the message intent (not bound by research suggestion)         │
│ • Output: DraftingOutput schema (pydantic-validated)            │
│ • Structured output: selected_emails[], subject, body, rationale│
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│ stage 5: warmbly_import (REST API call via Warmbly SDK)         │
│ • Input: DraftingOutput + verified_email_pool                   │
│ • Create campaign/sequence in Warmbly with draft content        │
│ • Trigger Warmbly event that publishes to Discord               │
│ • Output: campaign ID for audit trail                           │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────────┐
        │ Discord notification      │
        │ (Warmbly event handler)   │
        │                           │
        │ You review draft in       │
        │ Warmbly, then press start │
        │ manually.                 │
        └──────────────────────────┘
```

> **On reading the diagram:** `stage 2b: deeper_fetch` is drawn inline above, but it is
> *not* a peer stage. It lives inside the `research-agent` deployment along with both
> passes and both gate checks. See 2.3 for which stages are deployments and which are
> tasks.

### 2.2 Rejection Pool Handling

Businesses rejected at stage 3b (low confidence after 2-pass research) flow to `leads.agent_rejects`:

```
business_id
business_email (verified email from pool, even if rejected for personalization)
verified_email_pool (JSON: all validated addresses for this business)
research_output (JSON: both passes if 2-pass, else pass 1 only)
confidence (float)
rejected_at (timestamp)
manually_reviewed (bool, false initially)
```

**Purpose:** Rejections are not dead ends. Even if personalization confidence was low, you have a verified contact list and the research context. You can:
- Send a generic (non-personalized) campaign to these contacts
- Manually review and reach out to strong fit rejections
- Use rejection data to tune the confidence threshold over time

### 2.3 Node Boundaries and Prefect Objects

Prefect is the operating model for this pipeline, not a library the stages import: every
stage is deployed, operated and managed by Prefect rather than run as its own service.
Stages are therefore split into two kinds.

**Stages that are their own deployment** — independently schedulable, pausable,
versioned, and subject to their own concurrency limit:

| Deployment | Owns | Why it is separate |
|---|---|---|
| `orchestration/research-and-draft` | candidate selection, sequencing, Warmbly import | The parent; what you schedule |
| `email-validation` | `mxCheck.pl` subprocess, verified-email pool | A concurrency limit here bounds the real connection rate against other people's mail servers, so two pipeline runs cannot silently double it |
| `research-agent` | pass 1, confidence gate, `deeper_fetch`, pass 2, second gate | A concurrency limit bounds total LLM pressure *including* its own escalation |
| `drafting-agent` | draft subject/body, recipient selection | Model and prompt swap independently of research |

**Stages that are tasks inside the parent** — nothing about them is separately
operable:

- `candidate_selector` — one SQL query against `leads.businesses`.
- `warmbly_import` — one REST call.

**`deeper_fetch` is part of the research node, not a peer of it.** It is driven by
`next_url_to_check`, which only the research agent produces, and the confidence gate's
loop-back is research's own control flow. Promoting it to a sibling deployment would
force the parent to run pass 1, inspect the confidence, decide whether to fetch, and
then run pass 2 — leaking the node's retry logic into its caller. Kept inside, the node
has a single contract: *business record + verified email pool in, `ResearchOutput` or a
rejection out*, with how hard it worked to get there being its own business. It remains
reusable as `flow/fetch.py` without being separately deployed.

---

---

## 3. Data Structures

### 3.1 VerifiedEmail

```python
class VerifiedEmail(BaseModel):
    email: str                # validated via mxCheck
    verified_at: datetime
    source: str               # "leads.business_email" or "research_suggestion"
    mx_server: str | None     # from mxCheck output
```

### 3.2 ResearchOutput

```python
class ResearchOutput(BaseModel):
    business_name: str
    pain_signals: list[str]           # each traceable to evidence
    personalization_hook: str          # why this offer matters
    inferred_tone: Literal[
        "clinical", "warm", "premium", "casual", "sparse"
    ]                                 # tone inferred from site copy
    confidence: float                  # 0.0–1.0
    evidence: list[str]               # supporting excerpts
    suggested_email: str               # which of verified_email_pool
                                       # researcher recommends (not binding)
    next_url_to_check: str | None = None
                                       # optional URL for deeper_fetch
```

**Validation:** Output is requested as strict JSON schema via the LiteLLM proxy's
OpenAI-compatible `response_format={"type": "json_schema", ...}`, then validated with
Pydantic (`Model.model_validate_json`). Malformed responses are validation errors in
Prefect, not garbled emails in Warmbly.

**No `instructor` dependency.** Earlier drafts of this spec named the `instructor`
library here. It was never installed and was not chosen by the project owner; it
automates a prompt/validate/retry loop that is roughly fifteen lines against a proxy
that already supports `response_format`, with Prefect task retries supplying the
backoff. If a locally-routed model turns out to ignore strict schema requests, revisit
adding it rather than assuming it is needed.

### 3.3 DraftingOutput

```python
class DraftingOutput(BaseModel):
    selected_emails: list[str]   # agent picks which verified email(s)
                                 # (can be 1+ of the verified pool)
    subject: str
    body: str
    rationale: str               # why this framing for this business
```

---

## 4. Configuration & Environment

### 4.1 Required Environment Variables

```bash
# LLM provider -- calls go through the self-hosted LiteLLM PROXY over HTTP.
# The litellm python package is NOT required and is not installed; httpx is.
LITELLM_BASE_URL=http://127.0.0.1:4000  # litellm-compose-litellm-1, up on :4000
LITELLM_API_KEY=<proxy key>             # the proxy returns 401 without one
LLM_MODEL=<model name as the proxy routes it>   # swappable at runtime; the
                                        # proxy owns provider routing, so this
                                        # is a proxy model name, not a provider
                                        # path

# Prefect
PREFECT_API_URL=http://127.0.0.1:4200/api
# NOTE: this reads identically on both hosts and means DIFFERENT servers -- the
# local dev stack on hawkeye, or the canonical prod stack on accurateleadinfo.com.
# See 6.1. Check `prefect deployment ls` output, not this variable, to know which.

# Warmbly  (names below are the ones actually present in .env)
WARMBLY_API_URL=<per-tenant API host>   # Warmbly runs on htpc / 209.145.48.101,
                                        # NOT on accurateleadinfo.com. Exact URL
                                        # UNVERIFIED -- read it from .env, do not
                                        # assume crm.<domain>.
WARMBLY_API_TOKEN=<token>
WARMBLY_ORG_ID=<uuid>
WARMBLY_PHX_HOST=<phoenix host>         # realtime/WebSocket integration
WARMBLY_WEBSOCKET_URL=<ws url>

# Warmbly inbound webhook (flow/warmbly_http_endpoint.py)
WARMBLY_WEBHOOK_SECRET=<shared secret>  # unset disables signature checking
WARMBLY_WEBHOOK_EVENT_NAME=warmbly.webhook.received   # emitted into Prefect
WARMBLY_WEBHOOK_DEBUG=                  # 1 logs full request headers
WARMBLY_SIGNATURE_TIMESTAMP_KEY=t       # header is "t=<unix>,v1=<hex>"
WARMBLY_SIGNATURE_VERSION_KEY=v1
WARMBLY_SIGNATURE_SEPARATOR=.           # signed bytes are "<t>.<raw body>"
WARMBLY_SIGNATURE_MAX_AGE=              # unset = log age, never reject

# Jina Reader (for deeper_fetch)
JINA_API_KEY=<token>

# Database
LEADS_DB_URL=postgres://...           # existing leads schema

# mxCheck.pl
MXCHECK_SOCKS5_USER=<optional>        # if using proxy
MXCHECK_SOCKS5_PASS=<optional>

# Pipeline tuning
BATCH_SIZE=50                         # candidates per run
CONFIDENCE_THRESHOLD=0.7              # gate threshold
```

### 4.2 Optional Configuration

- **Scheduling:** on the *deployment*, not the `@flow` decorator -- `to_deployment(cron=...)` / `serve(cron=...)`, or event-triggered via `DeploymentEventTrigger`. `@flow` takes no `schedule` argument in Prefect 3.
- **Retry policy:** Prefect task-level retries (e.g., 3 attempts for Jina fetch, 1 for LLM to avoid cost)
- **Rate limiting:** mxCheck.pl's `--rate-limit` flag to avoid SMTP blocks

---

## 5. Error Handling & Observability

### 5.1 Failure Modes

| Stage | Failure | Recovery |
|-------|---------|----------|
| candidate_selector | SQL error / no results | Log and exit; next scheduled run picks up |
| email_validation | mxCheck crash / timeout | Reject business; log why (e.g., "all emails failed validation") |
| research pass 1 | LLM error / timeout | Retry 3x; on final failure, reject to agent_rejects |
| deeper_fetch | Jina API error / timeout | Skip deeper fetch, retry research with pass-1 context only |
| research pass 2 | LLM error / still low confidence | Reject to agent_rejects |
| drafting_agent | LLM error / no verified emails | Log and skip business; audit trail in Warmbly |
| warmbly_import | API error / auth failure | Prefect retry policy; on final failure, alert (Discord/email) |

### 5.2 Audit Trail

- **Prefect:** Full run history, logs per task, retry counts — visible in Prefect UI
- **Database:** `leads.agent_rejects` records all low-confidence rejections with full research context
- **Warmbly:** Campaign/sequence records with draft content, linked to Prefect run ID for tracing

---

## 6. Deployment & Operational Model

### 6.1 Infrastructure

Verified 2026-10-03. Three hosts are involved, which the earlier draft of this section
got wrong by assuming Prefect and Warmbly shared one.

| Host | Address | Runs |
|---|---|---|
| `accurateleadinfo.com` | 144.91.96.230 | **Canonical Prefect** (`prefect-compose` stack), traefik, the Warmbly webhook listener |
| `hawkeye` (workstation) | local | A **second, separate** `prefect-compose` stack used for development |
| `htpc` | 209.145.48.101 | Warmbly (12 containers: backend, web, worker, consumer, realtime, postgres, nats, …) |

**Prefect runs as a Docker stack, not `prefect server start`.** On both hosts the
`prefect-compose` stack provides `prefect-server`, `prefect-worker`, `prefect-services`,
postgres and redis. A worker therefore already exists to run deployments; nothing needs
supervising by hand.

**The two Prefect servers are a live hazard.** Both answer on `127.0.0.1:4200` from
their own host, so `PREFECT_API_URL` reads identically in both `.env` files while
meaning different servers with different databases. As of 2026-10-03 the development
server held all deployments and automations and the prod server held **none**. Work
registered against one is invisible to the other, and a flow or event emitted on the
wrong server fails silently: the emitter succeeds, and nothing ever runs.

**`accurateleadinfo.com` is canonical for anything in a delivery path**, because the
Warmbly webhook listener runs there and the workstation is not always on. Registering a
deployment is therefore an action taken *against the prod server*, and a run that
appears only in the local UI has not been deployed.

- **Scheduling:** Prefect owns cron, retries, state, history — no systemd/cron wrapper
  needed, and none should be added.

### 6.2 Starting the Pipeline

The stack is already running; these are the per-code-change steps.

```bash
# Register / update every deployment (parent + the three node deployments)
python -m flow.orchestration deploy

# Confirm they landed on the PROD server, not the local one
prefect deployment ls

# Run the parent manually for testing
prefect deployment run orchestration/research-and-draft --param batch_size=5

# Thereafter Prefect's scheduler owns cadence; nodes are paused/resumed in the UI
```

Each node deployment is independently runnable the same way, which is the point of
splitting them: `research-agent` can be re-run against a single business, or paused
during a model swap, without stopping the parent.

### 6.3 Secrets & Configuration

- All secrets in `.env` (not committed)
- Prefect loads `.env` at runtime; no need to `EnvironmentFile=` in systemd
- LiteLLM reads `LLM_MODEL` at task execution time, so model swaps don't require restart

---

## 7. Integration Points

### 7.1 Warmbly Event Flow

```
warmbly_import (Prefect)
    ↓ creates campaign
    ↓ triggers event
Warmbly.campaigns.create()
    ↓ broadcasts to integrations
Warmbly event handler (Discord)
    ↓ posts to Discord
Your notification
    ↓ you review draft
Warmbly UI
    ↓ you press start
Campaign sends (Warmbly owns delivery)
```

**Note:** Prefect posts the draft to Warmbly; Warmbly manages the notification and campaign lifecycle. Prefect does not push sends.

**Inbound direction (Warmbly -> Prefect).** Prefect's own webhook triggers are a
**Prefect Cloud feature** and do not exist on a self-hosted server, so there is no
built-in URL to point Warmbly at. The inbound path is therefore:

```
Warmbly (htpc) --HTTPS POST--> traefik (accurateleadinfo.com:443)
    -> flow/warmbly_http_endpoint.py   (uvicorn, host process)
         - validates "t=<unix>,v1=<hex>" signature over "<t>.<raw body>"
         - echoes data.challenge synchronously for webhook.test verification
         - emit_event(WARMBLY_WEBHOOK_EVENT_NAME, payload=<event>)
    -> Prefect automation (expect: that event name)
    -> warmbly-webhook-receiver deployment runs
```

Two constraints this path must respect:

- **The response must be synchronous.** Verification requires echoing
  `data.challenge` in the reply, so the listener answers immediately and the flow run
  is fired asynchronously. The handler cannot itself *be* the flow run.
- **Warmbly's `safehttp` guard rejects non-web ports** -- only 80 and 443. Its UI
  misreports this as "destination address is not publicly routable"; the real reason
  appears only in the `warmbly-backend-1` log. Hence traefik in front, never a direct
  port.

The event name is the entire routing key: the automation created by
`to_deployment(triggers=[...])` matches on name alone (`match: {}`), so a name reused
across consumers fans out to all of them.

### 7.2 Jina Reader Integration

- `flow/fetch.py` is the working task wrapper around Jina API
- `deeper_fetch` calls it to pull additional context when research confidence is low
- Rate limit: 500 req/min, 100k tokens/min (per existing key)

### 7.3 mxCheck.pl Integration

- Called via Prefect `subprocess.run()` with `--file <temp>` + `--threads 10` + `--rate-limit 30`
- Temp file created per batch, cleaned up after validation
- Output parsed as JSON array; results stored in `leads.email_verification` via existing loader

---

## 8. Tuning & Iteration

### 8.1 Confidence Threshold

Start with `CONFIDENCE_THRESHOLD=0.7`. Monitor:
- **False rejections** (low confidence but strong personalization): raise threshold
- **False passes** (high confidence but weak personalization after review): lower threshold
- Tune based on `leads.agent_rejects` audit data

### 8.2 Model Selection

Model routing is owned by the LiteLLM proxy, not by this pipeline. `LLM_MODEL` names
a model *as the proxy exposes it*; which provider that reaches, and with which key, is
proxy configuration.

- To change model: update `LLM_MODEL`, or re-point that name in the proxy config.
- No pipeline code changes either way.
- Check what the proxy actually offers with `curl -H "Authorization: Bearer $LITELLM_API_KEY" http://127.0.0.1:4000/v1/models` before assuming a name resolves.

Note the proxy runs on both the workstation and `accurateleadinfo.com`, so as with
Prefect, `127.0.0.1:4000` means different instances depending on where the code runs.

### 8.3 Batch Size

- Start small (5–10) for testing
- Scale to 50+ once agents are calibrated
- Adjust via `BATCH_SIZE` env var

---

## 9. Testing & Validation

### 9.1 Unit Tests (Pre-Implementation)

TDD applies. Before writing flow code:
- Test `candidate_selector` SQL query returns correct record shape
- Test `email_validation` task parses mxCheck JSON and rejects invalid emails
- Test `ResearchOutput` Pydantic validation (rejects malformed LLM output)
- Test `DraftingOutput` Pydantic validation
- Test confidence gate logic (threshold comparison)

### 9.2 Integration Tests

- Mock Warmbly API, validate import payload shape
- Mock LiteLLM, validate prompt structure and output parsing
- End-to-end: run pipeline on small test batch, verify records land in correct tables

### 9.3 Smoke Test

- Deploy to test Prefect (127.0.0.1:4200)
- Run manual flow with `BATCH_SIZE=3`
- Verify: candidates selected → emails validated → research runs → output in database

---

## 10. Known Constraints & Future Work

### 10.1 Current Constraints

- **Local LLM quality:** Ollama models are weaker at tone inference and copywriting. Upgrade path exists via LiteLLM provider swap.
- **Rejection follow-up:** Rejections land in audit pool; you decide if/when to send generic campaigns. No automated fallback.

### 10.2 Future Extensions

- Autonomous web search (via paid API like Brave or SerpAPI) if cost-justified
- Automated generic campaign flow for rejection pool (low priority)
- Multi-email drafting (draft different messages for multiple contacts at same business)
- Feedback loop: capture which drafts you approve/reject, use to retrain threshold or model selection

---

## 11. Success Criteria

- ✅ Pipeline processes `BATCH_SIZE` contacts per run without errors
- ✅ Research output is consistently structured and auditable
- ✅ All verified emails in `verified_email_pool` pass mxCheck validation
- ✅ Drafting agent picks emails intelligently based on message intent
- ✅ Rejection pool is populated and reviewable for low-confidence rejects
- ✅ Warmbly imports create campaigns successfully
- ✅ Discord notification surfaces each draft for human review
- ✅ No campaign sends without your manual approval in Warmbly

---

## 12. Implementation Phases

### Phase 1: Warmbly Integration (MVP)
- Prefect flow: email_validation (mxCheck) → warmbly_import
- Unit test using mxCheck-validated emails as test data
- Verify campaign/sequence appears in Warmbly UI
- Validate import payload shape and Warmbly event trigger

### Phase 2: Core Research & Drafting Pipeline
- Candidate selector
- Research agent (pass 1 & 2 with Jina deeper_fetch)
- Confidence gate + rejection pool
- Drafting agent
- Database schema (`leads.agent_rejects`)
- Full integration with Phase 1 Warmbly import

### Phase 3: Production Hardening
- Deployment on accurateleadinfo.com
- Scheduling + alert configuration
- Cost monitoring (LiteLLM + Jina)

### Phase 4: Tuning & Extensions
- Confidence threshold calibration
- Model swaps and cost optimization
- Feedback loop / retraining

---

## Appendix: File Structure

```
/home/nathaniel/leads/
├── flow/
│   ├── orchestration.py       # parent flow + `deploy` entrypoint registering all 4
│   ├── fetch.py               # Jina wrapper (existing; used inside research node)
│   ├── schemas.py             # Pydantic models
│   ├── warmbly_http_endpoint.py   # inbound listener: signature, challenge, emit_event
│   ├── warmbly_webhook_receiver.py # @flow + deployment consuming the emitted event
│   └── agents/
│       ├── research.py        # research-agent deployment (pass 1/2, gate, deeper_fetch)
│       ├── drafting.py        # drafting-agent deployment
│       └── validation.py      # email-validation deployment + mxCheck wrapper
├── docs/superpowers/specs/
│   └── 2026-09-28-prefect-warmbly-orchestration-design.md  # this file
├── .env                       # secrets + configuration
└── scripts/
    └── mxCheck.pl             # email validation (existing)
```

---

**Next Step:** Write `docs/superpowers/specs/2026-09-28-prefect-warmbly-orchestration-implementation-plan.md` via the writing-plans skill, then implement with TDD.
