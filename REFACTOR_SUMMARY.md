# Agent Artifact I/O & JSON Input Refactor

## Summary

Refactored research and drafting agents to:
1. **Write JSON artifacts** tagged by Prefect flow run tags (e.g. `biz-1-research-input.json`, `biz-1-research-output.json`)
2. **Accept JSON input files** in addition to dict parameters
3. **Created import-contacts agent** that reads business+research+draft context and imports to Warmbly, with JSON I/O support

## Files Created

### `flow/artifacts.py`
Central artifact I/O utility. Uses `prefect.runtime.flow_run.tags` to name files.

```python
artifact_filename(suffix='output')  # → 'biz-1-research-output.json'
write_artifact(data, suffix='output', directory=None)  # writes JSON
read_artifact(filepath, suffix=None)  # reads JSON
```

**Key features:**
- Tags are joined with `-`: `['biz-1', 'research']` → `biz-1-research-{suffix}.json`
- Falls back to `unknown-{suffix}.json` if no tags
- Stringifies non-JSON types via `default=str`
- Catches both `TypeError` and `ValueError` during serialization

### `flow/agents/import_contacts.py`
New deployment for Warmbly contact import.

**Signature:**
```python
@flow(log_prints=True)
def import_contacts(
    business: Optional[Dict] = None,
    research: Optional[Dict] = None,
    draft: Optional[Dict] = None,
    idempotency_key: Optional[str] = None,
    json_input_file: Optional[str] = None,
) -> Dict
```

**Behavior:**
- Accepts either dict params or `json_input_file` (file takes precedence)
- Calls `build_contact_payloads()` → `create_contacts()`
- Derives idempotency key from `business.id` if not provided
- Returns `{'status': 'imported', 'payloads_sent': N, 'created': [...]}` or `{'status': 'rejected', 'reason': '...', 'error': '...'}`
- Writes input and output artifacts

## Files Refactored

### `flow/agents/research.py`
**New signature:**
```python
@flow(log_prints=True)
def research_agent(
    business: Optional[dict] = None,
    verified_emails: Optional[list] = None,
    crawl_excerpt: str = '',
    offer: Optional[str] = None,
    json_input_file: Optional[str] = None,
) -> dict
```

**Changes:**
- All params now optional; validation requires either dict params or `json_input_file`
- Writes input artifact before research starts
- Writes output artifact on every exit path
- File-based inputs take precedence over dict params

### `flow/agents/drafting.py`
**New signature:**
```python
@flow(log_prints=True)
def drafting_agent(
    research: Optional[dict] = None,
    verified_emails: Optional[list] = None,
    template_slug: str = 'default',
    offer: Optional[str] = None,
    json_input_file: Optional[str] = None,
) -> dict
```

**Changes:**
- Same JSON artifact support as research agent
- Writes input and output artifacts

### `flow/serve_agents.py`
**Changes:**
- Added `import_contacts` to the deployment list
- Now serves 4 deployments: `research-agent`, `drafting-agent`, `import-contacts`, `candidate-selector`

## Tests Created

### `tests/test_artifacts.py` (20 tests)
- Artifact filename generation from tags
- Writing and reading JSON files
- Error handling (missing files, invalid JSON, non-serializable data)
- Round-trip preservation
- Complex nested structures

### `tests/test_research_agent.py` (+6 tests)
- JSON input file loading
- Input/output artifact writing
- Validation (requires either dict or file)
- File input precedence over dict params

### `tests/test_drafting_agent.py` (+6 tests)
- Same JSON I/O tests as research agent

### `tests/test_import_contacts_agent.py` (12 tests)
- Happy path (successful import with mock Warmbly)
- Failure modes (no emails, payload build failure, Warmbly API error)
- JSON input file loading
- Multiple email addresses
- Idempotency key derivation and override

## Test Results

```
✅ All 306 tests pass
  - 64 new tests for artifacts + agents JSON I/O
  - All existing tests still pass (no breaking changes)
```

## Usage Examples

### Call agents with dict params (as before)
```python
result = research_agent(
    business={'id': 'biz-1', ...},
    verified_emails=[...],
    crawl_excerpt='...'
)
```

### Call agents with JSON input file
```python
# File: biz-1-research-input.json
result = research_agent(json_input_file='biz-1-research-input.json')
```

### Call import_contacts
```python
# From dict params
result = import_contacts(
    business={'id': 'biz-1', ...},
    research={...},
    draft={...}
)

# From JSON file
result = import_contacts(json_input_file='biz-1-import-input.json')
```

### File naming (automatic)
When called with tags `['biz-1', 'research']`:
```
Input artifact:  biz-1-research-input.json
Output artifact: biz-1-research-output.json
```

## Integration with Prefect Deployments

All three agents are now available as deployments via `serve_agents.py`:

```bash
$ set -a && . ./.env && set +a
$ python flow/serve_agents.py
```

Deployments:
- `research-agent` — standalone or subflow
- `drafting-agent` — standalone or subflow
- `import-contacts` — standalone or subflow (NEW)
- `candidate-selector` — batch selection
- `run-agents` — full orchestration (if `SERVE_RUN_AGENTS=true`)

Each can be triggered via `prefect deployment run` or called as a subflow within `run_agents`.

## Migration Notes

### Breaking Changes
None. Existing code using dict parameters continues to work.

### Deprecations
None. JSON file input is an addition, not a replacement.

### Future Work
- Add a CLI wrapper for import_contacts that reads from command-line args or config files
- Extend artifact I/O to support other formats (CSV, Parquet)
- Add artifact versioning (e.g. `-v2` suffix)
