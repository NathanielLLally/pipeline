# Project Core

Lead-sourcing B2B prospect database platform. Discovers high-quality small business prospects for lead acquisition sales.

## Structure

- `flow/` — Prefect orchestration agents (research, drafting, analysis)
- `scripts/` — JavaScript (ES modules) data enrichment & transformation pipelines
- `db/` — PostgreSQL schema and queries (leads schema)
- `deploy/` — Bash deployment and system utilities
- `tests/` — pytest suite for agents
- Root-level CLI tools: `cli.py`, `ag.py`, `agg.py`

## Languages & Tech Stack

Covered in `mem:tech_stack` — Python, JavaScript, SQL, Bash, Markdown, JSON, YAML, Perl.

## Key Dependencies

- `mem:tech_stack` — orchestrator (Prefect 3.2.15), LLM proxy (LiteLLM over httpx), SDK (Warmbly 0.3.0), DB (asyncpg)
- Database: PostgreSQL with leads schema
- Configuration: `.env` for LLM_MODEL, LITELLM_API_KEY, etc.

## Known Issues

See project CLAUDE.md: test-driven development required; on bugs, file with `gh issue create`.

## Warmbly Integration Notes

Known issues with Warmbly SDK documented in global project memory at `/home/nathaniel/.claude/projects/-home-nathaniel-leads/memory/`—check there for UUID serialization and per-tenant API host details.
