# Tech Stack

## Languages

- **Python 3.15.0rc1** (58 files) — Primary orchestration and business logic
  - Pinned in requirements.txt, installed via venv
  - Type hints: Optional[str], List[...] (PEP 484, not PEP 604)
- **JavaScript (ES Modules .mjs)** (27 files) — Data enrichment & transformation pipelines
  - scripts/lib/ shared utilities (ads-transparency, rdap, emails, etc.)
  - scripts/ top-level: gen-queries, enrich-*, extract-*, transform-and-score, qc-pass, rescore
  - Node.js runtime (version unpinned; check scripts for compatibility)
- **SQL** (16 files) — PostgreSQL queries and schema
  - Namespace: leads.* (independent of public schema)
  - db/schema.sql, db/upsert.sql, db/*.sql query files
- **Bash** (17 files) — Deployment, utilities, systemd setup
  - deploy/ scripts, refresh-proxies.sh
  - Shebang: #!/usr/bin/env bash, set -euo pipefail
- **Markdown** (33 files) — Documentation
  - CLAUDE.md (project rules), ANALYSIS_*.md (guides), docs/ARCHITECTURE.md
- **JSON** (14 files) — Data serialization & config
  - Pydantic model outputs, analysis artifacts, business data
- **YAML** (4 files) — Configuration
  - pyworkflow.config.yaml (PyWorkflow config; PyWorkflow not yet integrated)
- **Perl** (2 files) — Legacy system utilities
  - scripts/mxCheck.pl (run as subprocess from flow/perl_flow.py)

## Core Frameworks & Libraries

### Python Orchestration
- **Prefect 3.2.15** — Flow orchestration and deployment
  - Agents: @flow decorator, async operations
  - Prefect variables for state persistence
  - Deployments served via CLI

### Python LLM Integration
- **LiteLLM (proxy)** — Central LLM routing, not the Python package
  - HTTP calls to http://127.0.0.1:4000/v1/chat/completions
  - Structured output via OpenAI-compatible json_schema with strict mode
  - Authentication: x-litellm-api-key header
  - Environment: LITELLM_API_KEY, LLM_MODEL, ANALYSIS_MODEL, RESEARCH_MODEL, DRAFTING_MODEL

### Python Database
- **asyncpg** — PostgreSQL async client
- **schema** — leads.* namespace

### Python APIs & SDKs
- **Warmbly 0.3.0** — Contact realtime gateway and REST API
- **FastAPI 0.141.1 + Uvicorn 0.54.0** — Webhook endpoints
- **Discord.py 2.7.1** — Discord bot integration
- **httpx 0.28.1** — HTTP client (LLM proxy)

### Python Testing
- **pytest 9.1.1** — Unit test runner
- **conftest.py** — Shared test fixtures

### Python Other
- **Pydantic 2.13.5** — Schema validation (agent outputs, LLM responses)
- **python-slugify 8.0.4** — Transliterate business IDs to Prefect Variable names
- **jsonargparse 4.52.0** — CLI argument parsing

### JavaScript (scripts/)
- **Node.js** — Runtime (version unspecified; check compatibility)
- No package manager declared (no package.json); modules use import/export

## Build & Package Management

### Python
- **pip** with venv
- **requirements.txt** (pinned versions, tested against Python 3.15.0rc1)
- No pyproject.toml for build; direct pip install

### JavaScript
- No package manager; modules assumed to be available in Node.js stdlib or global

## Configuration

- `.env` file (checked into repo)
  - Python: LLM_MODEL, ANALYSIS_MODEL, RESEARCH_MODEL, DRAFTING_MODEL
  - Python: LITELLM_API_KEY, LITELLM_BASE_URL
  - JavaScript: May read environment variables directly (verify per script)

## Deployment

- **systemd user timers** — Watchdog deployed via deploy/install-watchdog.sh
- **SSH key-based** — Connects to worker hosts on non-standard port 2222
- **Environment** — REST_SSH_USER account, reads .env per-host
