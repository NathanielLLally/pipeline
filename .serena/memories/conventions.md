# Code Conventions

## Python

- Type hints: `Optional[str]`, `List[...]` (PEP 484, not PEP 604 unions)
- Docstrings: Module-level and function-level
- Pydantic models: Field descriptors for LLM prompt context
- No f-strings in complex formatting; use `.format()` for clarity
- Async operations: asyncpg, FastAPI, Prefect flows

## JavaScript (scripts/)

- ES Modules (.mjs) with import/export
- Shared utilities in scripts/lib/ (rdap.mjs, ads-transparency.mjs, emails.mjs, etc.)
- Test files: *.test.mjs (e.g., ads-transparency.test.mjs)
- Environment: scripts access process.env for config
- No bundler; direct node execution

## Testing (TDD)

- **Python**: Always write tests first before implementation
  - Native pytest mocking (no instructor library)
  - Run with: `pytest`
  - Test fixtures in conftest.py
- **JavaScript**: Test files alongside source (*.test.mjs)
  - Run individually as needed

## Bash

- `set -euo pipefail` in deployment scripts
- Absolute paths: `cd "$(dirname "${BASH_SOURCE[0]}")/.." `
- Comments explaining non-obvious intent

## Schemas & Validation

- Pydantic BaseModel for all structured outputs
- Field validators (e.g., @field_validator for placeholder rejection)
- LLM schema requests use strict mode: `"strict": True`

## Agents (Python)

- All agents return dict with 'status' and payload
- Write artifacts via `write_artifact()` for traceability
- Catch LLMSchemaError and LLMTransportError explicitly
- Print results for flow logs (no silent failures)

## Database

- Queries in `db/` directory, separate from schema
- Use asyncpg for async operations
- Deduplicate via place_id, domain, phone, name_city_state_key

## Configuration

- Environment-based (LLM_MODEL, LITELLM_API_KEY, etc.)
- No hardcoded API hosts or credentials
- Per-tenant models: ANALYSIS_MODEL, RESEARCH_MODEL, DRAFTING_MODEL
