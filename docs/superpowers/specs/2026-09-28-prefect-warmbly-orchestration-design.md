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
│ • Output: ResearchOutput schema (validated via instructor)      │
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
│ • Output: DraftingOutput schema (validated via instructor)      │
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

**Validation:** Output forced to strict JSON schema via `instructor` + Pydantic. Malformed responses are validation errors in Prefect, not garbled emails in Warmbly.

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
# LLM provider
LLM_MODEL=ollama/mistral              # e.g. ollama/*, openai/gpt-4, etc.
                                      # swappable at runtime via LiteLLM
OLLAMA_BASE_URL=http://127.0.0.1:11434  # if using local Ollama

# Prefect
PREFECT_API_URL=http://127.0.0.1:4200   # OSS server (test)
                                         # prod points to accurateleadinfo.com
                                         # Prefect manages deployment

# Warmbly
WARMBLY_URL=https://crm.accurateleadinfo.com
WARMBLY_API_TOKEN=<token>

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

- **Scheduling:** `@flow` decorator with `schedule=...` (Prefect cron syntax) or event-triggered via `DeploymentEventTrigger`
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

- **Prod:** Prefect service runs on `accurateleadinfo.com` (same host as Warmbly CRM)
- **Test:** Prefect OSS at `127.0.0.1:4200` (local development)
- **Scheduling:** Prefect owns cron, retries, state, history — no systemd/cron wrapper needed

### 6.2 Starting the Pipeline

```bash
# Start Prefect service (one-time)
prefect server start

# Deploy flows to Prefect (one-time per code update)
python -m flow.orchestration deploy

# Run manually (for testing)
prefect flow run orchestration.research_and_draft --batch-size 5

# Or let Prefect's scheduler handle cron (in deployment config)
```

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

Default: `ollama/mistral` (local, free). If quality is insufficient:
- Try `ollama/neural-chat` or other Ollama models
- Or swap to Claude/GPT via LiteLLM (e.g., `openai/gpt-4o`) with API key
- No code changes needed; just update `LLM_MODEL` env var

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
- **Search capability:** Deeper fetch only works if research agent proposes a valid URL. No autonomous web search (datacenter proxies blocked by search engines). Research agent must identify a link in the crawled content.
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

### Phase 1: Core Pipeline (This Spec)
- Candidate selector
- Email validation
- Research agent (pass 1 & 2)
- Confidence gate
- Drafting agent
- Warmbly import
- Database schema (`leads.agent_rejects`)

### Phase 2: Production Hardening
- Systemd/deployment on accurateleadinfo.com
- Scheduling + alert configuration
- Cost monitoring (LiteLLM + Jina)

### Phase 3: Tuning & Extensions
- Confidence threshold calibration
- Model swaps and cost optimization
- Feedback loop / retraining

---

## Appendix: File Structure

```
/home/nathaniel/leads/
├── flow/
│   ├── orchestration.py       # main flows (research_and_draft, deeper_fetch, etc.)
│   ├── fetch.py               # Jina wrapper (existing)
│   ├── schemas.py             # Pydantic models
│   └── agents/
│       ├── research.py        # research_agent prompts & LiteLLM calls
│       ├── drafting.py        # drafting_agent prompts & LiteLLM calls
│       └── validation.py      # email_validation + mxCheck wrapper
├── docs/superpowers/specs/
│   └── 2026-09-28-prefect-warmbly-orchestration-design.md  # this file
├── .env                       # secrets + configuration
└── scripts/
    └── mxCheck.pl             # email validation (existing)
```

---

**Next Step:** Write `docs/superpowers/specs/2026-09-28-prefect-warmbly-orchestration-implementation-plan.md` via the writing-plans skill, then implement with TDD.
