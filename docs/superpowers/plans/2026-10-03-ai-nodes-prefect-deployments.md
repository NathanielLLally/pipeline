# AI Nodes as Prefect Deployments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `research-agent` and `drafting-agent` Prefect deployments — independently runnable, pausable and rate-limitable from the Prefect UI — each with a single honest contract and no standalone service to supervise.

**Architecture:** Both nodes are `@flow`s turned into deployments and served together, following `flow/wh.py`. LLM calls go to the self-hosted **LiteLLM proxy** over HTTP (`httpx`), not through any provider SDK, asking for `response_format={"type":"json_schema"}` and validating the reply with Pydantic. The research node owns its own escalation — pass 1, confidence gate, `deeper_fetch`, pass 2, second gate — so its caller never sees that machinery.

**Tech Stack:** Python (3.9-compatible source), Prefect 3.2.15 self-hosted, `httpx`, `pydantic` 2.13.5, the LiteLLM proxy on `:4000`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-28-prefect-warmbly-orchestration-design.md` (§2.1 stages, §2.3 node boundaries, §3 data structures, §8.1–8.2 tuning)

## Global Constraints

- **Write 3.9-compatible source.** The prod interpreter is Python 3.9: PEP 604 unions (`str | None`) crash at import. Use `Optional[str]`. **The spec's §3 snippets use `str | None` — they are illustrative, not copy-paste.** PEP 585 generics (`dict[str, Any]`) are fine.
- **No hardcoded plumbing.** Hosts, ports, model names, thresholds and prompts' tunable values come from environment variables, using names already in `.env` where they exist.
- **No new dependencies.** `httpx` + `pydantic` are present; `litellm` the *package* and `instructor` are deliberately not used (see spec §3.2). `psycopg2` is absent from both the venv and the worker image — this plan touches no database.
- **TDD, per `CLAUDE.md`:** failing test first, watched fail, then minimal code.
- **Never call a real LLM in a unit test.** Every test mocks the HTTP layer. A test that spends money or needs the proxy up is not a unit test.
- **Calling a Prefect `@flow` directly creates a real flow run** against `PREFECT_API_URL`. Test flows through `.fn`. `prefect_test_harness` is unusable here (its temp server 500s on `/api/admin/version` under this Python).
- **`serve()` and anything emitting to it must share a host**, and `PREFECT_API_URL` reads `127.0.0.1:4200` on both the workstation and prod while meaning different servers.
- **File a GitHub issue for bugs and TODOs** (`gh issue create`) rather than leaving comments.

## Review Focus

Failure modes the spec implies but whose happy-path tests would not catch, most damaging first:

1. **The drafting agent selects an email outside the verified pool.** §3.3 lets it pick "which verified email(s)", and §2.1 says it is "not bound by research suggestion" — but nothing binds it to the *pool*. An LLM returning a plausible-looking address that was never MX-validated means mail to an unverified recipient. → Task 4, Step 6.
2. **The model returns well-formed JSON that fails schema validation** (confidence as a string, a tone outside the Literal, a missing field). Retrying forever burns money; failing silently emits a garbled draft. → Task 2, Step 8.
3. **Confidence exactly equal to the threshold.** §3b says "if confidence >= threshold" — an off-by-one here silently routes every borderline business the wrong way, and the rejection pool is where they land. → Task 3, Step 6.
4. **Low confidence with no `next_url_to_check`.** The escalation path needs a URL; without one there is nothing to fetch, and the node must reject rather than re-run pass 1 identically or loop. → Task 3, Step 8.
5. **The proxy returns 401, 429 or times out.** The key is required (verified: the proxy 401s without one) and a stalled request inside a flow run holds a slot indefinitely. → Task 2, Step 10.

---

## File Structure

| File | Responsibility |
|---|---|
| `flow/schemas.py` (create) | The Pydantic models from spec §3, 3.9-compatible. No I/O, no Prefect. |
| `flow/llm.py` (create) | The only thing that talks to the LiteLLM proxy. Schema-constrained call + validation retry. No Prefect. |
| `flow/agents/__init__.py` (create) | Package marker. |
| `flow/agents/research.py` (create) | `research-agent` deployment: pass 1, gate, `deeper_fetch`, pass 2, gate. |
| `flow/agents/drafting.py` (create) | `drafting-agent` deployment: `ResearchOutput` → `DraftingOutput`. |
| `flow/serve_agents.py` (create) | Serves both deployments in one process. |
| `tests/test_schemas.py` (create) | Model validation and rejection. |
| `tests/test_llm.py` (create) | Request shape, validation retry, error handling — all HTTP mocked. |
| `tests/test_research_agent.py` (create) | Gate boundaries and the escalation path. |
| `tests/test_drafting_agent.py` (create) | Pool binding and output shape. |

`flow/fetch.py` (the existing Jina wrapper) is consumed by the research node, unchanged.

---

### Task 1: Schemas

**Files:**
- Create: `flow/schemas.py`
- Test: `tests/test_schemas.py`

**Interfaces:**
- Produces: `VerifiedEmail`, `ResearchOutput`, `DraftingOutput` (Pydantic `BaseModel`s), and `REJECTION_REASONS: tuple`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from pydantic import ValidationError

from flow.schemas import DraftingOutput, ResearchOutput, VerifiedEmail


def _research(**over):
    base = dict(
        business_name="Happy Tails", pain_signals=["no online booking"],
        personalization_hook="they book by phone only",
        inferred_tone="warm", confidence=0.8, evidence=["Call us to book"],
        suggested_email="owner@happytails.com",
    )
    base.update(over)
    return base


class TestResearchOutput:
    def test_accepts_a_complete_payload(self):
        out = ResearchOutput(**_research())
        assert out.confidence == 0.8
        assert out.next_url_to_check is None

    def test_rejects_tone_outside_the_vocabulary(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(inferred_tone="enthusiastic"))

    def test_rejects_confidence_above_one(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(confidence=1.4))

    def test_rejects_confidence_below_zero(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(confidence=-0.1))

    def test_coerces_numeric_string_confidence(self):
        """Models often return numbers as strings; that is not a failure."""
        assert ResearchOutput(**_research(confidence="0.75")).confidence == 0.75


class TestVerifiedEmail:
    def test_mx_server_is_optional(self):
        ve = VerifiedEmail(
            email="a@b.com", verified_at="2026-10-03T00:00:00Z",
            source="leads.business_email",
        )
        assert ve.mx_server is None


class TestDraftingOutput:
    def test_requires_at_least_one_selected_email(self):
        with pytest.raises(ValidationError):
            DraftingOutput(selected_emails=[], subject="s", body="b",
                           rationale="r")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -v`
Expected: FAIL at collection, `ModuleNotFoundError: No module named 'flow.schemas'`

- [ ] **Step 3: Write the models**

```python
"""
Pydantic models for the research and drafting pipeline (spec section 3).

No I/O and no Prefect imports: these are the contract between the nodes, and
they must stay importable anywhere, including inside a Prefect worker.

Written for Python 3.9 -- `Optional[str]`, never `str | None`. The spec's
snippets use PEP 604 unions illustratively; they would crash on the prod
interpreter.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

REJECTION_REASONS = (
    'no_verified_emails',
    'low_confidence_after_two_passes',
    'low_confidence_no_escalation_url',
    'llm_schema_failure',
)


class VerifiedEmail(BaseModel):
    email: str
    verified_at: datetime
    source: str
    mx_server: Optional[str] = None


class ResearchOutput(BaseModel):
    business_name: str
    pain_signals: List[str]
    personalization_hook: str
    inferred_tone: Literal['clinical', 'warm', 'premium', 'casual', 'sparse']
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str]
    suggested_email: str
    next_url_to_check: Optional[str] = None


class DraftingOutput(BaseModel):
    selected_emails: List[str] = Field(min_length=1)
    subject: str
    body: str
    rationale: str
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_schemas.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Verify 3.9 compatibility**

Run: `grep -nE ':[^=]*\b(str|int|float|bytes|dict|list|bool|Any)\s*\|' flow/schemas.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add flow/schemas.py tests/test_schemas.py
git commit -m "feat(agents): pydantic schemas for research and drafting

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The LiteLLM proxy client

**Files:**
- Create: `flow/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: the models from Task 1.
- Produces: `complete_structured(prompt: str, schema_model, system: Optional[str] = None, max_attempts: int = 2)` returning a validated instance of `schema_model`; `LLMSchemaError`; `LLMTransportError`; `PROXY_URL`, `MODEL`, `REQUEST_TIMEOUT`.

- [ ] **Step 1: Write the failing test for the request shape**

```python
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from flow.llm import (
    LLMSchemaError,
    LLMTransportError,
    MODEL,
    complete_structured,
)
from flow.schemas import ResearchOutput

VALID = {
    "business_name": "Happy Tails", "pain_signals": ["no booking"],
    "personalization_hook": "phone only", "inferred_tone": "warm",
    "confidence": 0.8, "evidence": ["Call us"],
    "suggested_email": "owner@happytails.com",
}


def _reply(content):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    r.raise_for_status.return_value = None
    return r


class TestRequestShape:
    def test_asks_the_proxy_for_a_json_schema(self):
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        body = post.call_args.kwargs['json']
        assert body['model'] == MODEL
        assert body['response_format']['type'] == 'json_schema'
        assert 'properties' in body['response_format']['json_schema']['schema']

    def test_sends_the_api_key(self, monkeypatch):
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-test')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer sk-test'

    def test_returns_a_validated_model(self):
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            out = complete_structured("describe", ResearchOutput)

        assert isinstance(out, ResearchOutput)
        assert out.confidence == 0.8
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: FAIL at collection, `ModuleNotFoundError: No module named 'flow.llm'`

- [ ] **Step 3: Write the client**

```python
"""
The only module that talks to the LiteLLM proxy.

Calls go over HTTP to the self-hosted proxy, which owns provider routing and
keys. The `litellm` Python package is deliberately not used: the proxy is the
integration point, and `httpx` is already a dependency.

Structured output is requested with OpenAI-compatible
`response_format={"type": "json_schema", ...}` and validated with Pydantic.
No `instructor` -- see spec section 3.2.
"""

import json
import os
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

PROXY_URL = os.environ.get('LITELLM_BASE_URL', 'http://127.0.0.1:4000')
MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
REQUEST_TIMEOUT = float(os.environ.get('LLM_REQUEST_TIMEOUT', '120'))


class LLMTransportError(RuntimeError):
    """The proxy could not be reached, refused us, or timed out."""


class LLMSchemaError(ValueError):
    """The model answered, but never in the shape we asked for."""


def _request(messages: list, schema_model: Type[BaseModel]) -> str:
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LITELLM_API_KEY')
    if key:
        headers['Authorization'] = f'Bearer {key}'

    body = {
        'model': MODEL,
        'messages': messages,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {
                'name': schema_model.__name__,
                'schema': schema_model.model_json_schema(),
                'strict': True,
            },
        },
    }

    try:
        response = httpx.post(
            f'{PROXY_URL}/v1/chat/completions',
            json=body, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LLMTransportError(
            f'proxy returned {exc.response.status_code}'
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransportError(f'{type(exc).__name__}: {exc}') from exc

    return response.json()['choices'][0]['message']['content']


def complete_structured(
    prompt: str,
    schema_model: Type[BaseModel],
    system: Optional[str] = None,
    max_attempts: int = 2,
) -> Any:
    """
    Ask the model for an instance of schema_model and validate it.

    On a validation failure the model is asked again with its own errors
    attached, up to max_attempts. Bounded deliberately: an unbounded retry
    against a model that cannot produce the shape spends money forever.
    """
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    last_error = None
    for attempt in range(1, max_attempts + 1):
        content = _request(messages, schema_model)

        try:
            return schema_model.model_validate_json(content)
        except ValidationError as exc:
            last_error = exc
            print(f'schema validation failed (attempt {attempt}): {exc}')
        except json.JSONDecodeError as exc:
            last_error = exc
            print(f'response was not JSON (attempt {attempt}): {content[:200]}')

        messages.append({'role': 'assistant', 'content': content})
        messages.append({
            'role': 'user',
            'content': (
                'That did not validate against the schema. Errors:\n'
                f'{last_error}\nReturn only JSON matching the schema.'
            ),
        })

    raise LLMSchemaError(
        f'{schema_model.__name__} not produced in {max_attempts} attempts: '
        f'{last_error}'
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add flow/llm.py tests/test_llm.py
git commit -m "feat(agents): litellm proxy client with schema-constrained output

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing test for a non-JSON reply**

```python
class TestMalformedReplies:
    def test_prose_reply_is_retried_then_raises(self):
        with patch('flow.llm.httpx.post', return_value=_reply("Sure! Here you go:")) as post:
            with pytest.raises(LLMSchemaError):
                complete_structured("describe", ResearchOutput, max_attempts=2)

        assert post.call_count == 2
```

Run it: expect FAIL if the non-JSON path escapes as something other than `LLMSchemaError`.

- [ ] **Step 7: Run and confirm**

Run: `.venv/bin/python -m pytest tests/test_llm.py -k prose_reply -v`
Expected: PASS. `model_validate_json` raises `ValidationError` (not `JSONDecodeError`) for non-JSON input in Pydantic 2, which the existing `except ValidationError` already covers — confirm that is why it passes rather than assuming.

- [ ] **Step 8: Write the failing test for valid-JSON-wrong-shape** *(Review Focus 2)*

```python
    def test_valid_json_wrong_shape_is_retried_with_the_errors(self):
        bad = json.dumps(dict(VALID, inferred_tone="enthusiastic"))
        with patch('flow.llm.httpx.post', return_value=_reply(bad)) as post:
            with pytest.raises(LLMSchemaError):
                complete_structured("describe", ResearchOutput, max_attempts=3)

        assert post.call_count == 3
        # The retry must tell the model what was wrong, or it will repeat it.
        final_messages = post.call_args.kwargs['json']['messages']
        assert any('inferred_tone' in str(m['content']) for m in final_messages)

    def test_recovers_when_the_second_attempt_validates(self):
        bad = _reply(json.dumps(dict(VALID, confidence=5.0)))
        good = _reply(json.dumps(VALID))
        with patch('flow.llm.httpx.post', side_effect=[bad, good]) as post:
            out = complete_structured("describe", ResearchOutput)

        assert out.confidence == 0.8
        assert post.call_count == 2
```

Run: `.venv/bin/python -m pytest tests/test_llm.py -v` → both PASS (the implementation in Step 3 already feeds errors back; these pin it).

- [ ] **Step 9: Commit**

```bash
git add tests/test_llm.py
git commit -m "test(agents): pin schema-retry behaviour for malformed llm replies

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 10: Write the failing test for transport failures** *(Review Focus 5)*

```python
class TestTransportFailures:
    def test_401_becomes_a_transport_error_not_a_schema_error(self):
        """The proxy 401s without a key; that is config, not a bad model."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        err = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = err

        with patch('flow.llm.httpx.post', return_value=resp):
            with pytest.raises(LLMTransportError) as caught:
                complete_structured("describe", ResearchOutput)

        assert '401' in str(caught.value)

    def test_timeout_becomes_a_transport_error(self):
        with patch('flow.llm.httpx.post',
                   side_effect=httpx.ReadTimeout("too slow")):
            with pytest.raises(LLMTransportError):
                complete_structured("describe", ResearchOutput)

    def test_transport_failure_is_not_retried_as_a_schema_problem(self):
        """Retrying a 401 three times just 401s three times."""
        with patch('flow.llm.httpx.post',
                   side_effect=httpx.ReadTimeout("too slow")) as post:
            with pytest.raises(LLMTransportError):
                complete_structured("describe", ResearchOutput, max_attempts=3)

        assert post.call_count == 1
```

Run: `.venv/bin/python -m pytest tests/test_llm.py -v`
Expected: PASS. `_request` raises before the validation loop, so transport errors escape immediately — confirm from the call counts, not from reading the code.

- [ ] **Step 11: Commit**

```bash
git add tests/test_llm.py
git commit -m "test(agents): transport errors are distinct from schema errors

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The research-agent deployment

**Files:**
- Create: `flow/agents/__init__.py` (empty), `flow/agents/research.py`
- Test: `tests/test_research_agent.py`

**Interfaces:**
- Consumes: `complete_structured`, `LLMSchemaError`, `LLMTransportError` from `flow.llm`; `ResearchOutput`, `VerifiedEmail` from `flow.schemas`; `fetch_html` from `flow.fetch`.
- Produces: `research_agent(business: dict, verified_emails: list, crawl_excerpt: str = "") -> dict` returning either `{"status": "researched", "research": <ResearchOutput as dict>, "passes": int}` or `{"status": "rejected", "reason": <one of REJECTION_REASONS>, "research": <dict or None>, "passes": int}`; `CONFIDENCE_THRESHOLD`; `build_research_prompt(business, verified_emails, crawl_excerpt, extra_context="")`.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import patch

import pytest

from flow.agents.research import (
    CONFIDENCE_THRESHOLD,
    build_research_prompt,
    research_agent,
)
from flow.schemas import ResearchOutput

BUSINESS = {"id": 1, "business_name": "Happy Tails",
            "website": "https://happytails.example"}
EMAILS = [{"email": "owner@happytails.example", "verified_at":
           "2026-10-03T00:00:00Z", "source": "leads.business_email"}]


def _out(**over):
    base = dict(
        business_name="Happy Tails", pain_signals=["no online booking"],
        personalization_hook="phone only", inferred_tone="warm",
        confidence=0.9, evidence=["Call us to book"],
        suggested_email="owner@happytails.example",
    )
    base.update(over)
    return ResearchOutput(**base)


class TestPromptConstruction:
    def test_prompt_names_the_business_and_the_verified_pool(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "some crawl text")

        assert "Happy Tails" in prompt
        assert "owner@happytails.example" in prompt
        assert "some crawl text" in prompt


class TestConfidentFirstPass:
    def test_high_confidence_returns_without_escalating(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)) as llm:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert result['passes'] == 1
        assert llm.call_count == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_research_agent.py -v`
Expected: FAIL at collection, `ModuleNotFoundError: No module named 'flow.agents'`

- [ ] **Step 3: Write the research node**

```python
"""
The research-agent deployment.

Its contract: a business record plus a verified email pool in, a
ResearchOutput or a rejection out. Pass 1, the confidence gate, deeper_fetch
and pass 2 all live inside, because escalation is this node's own business and
its caller should not have to drive it (spec section 2.3).
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.fetch import fetch_html
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.schemas import ResearchOutput

CONFIDENCE_THRESHOLD = float(
    os.environ.get('CONFIDENCE_THRESHOLD', '0.7')
)

SYSTEM_PROMPT = (
    'You research small service businesses to find one specific, evidenced '
    'reason a lead-generation offer would matter to them. Quote evidence '
    'from the supplied text. Never invent an email address.'
)


def build_research_prompt(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str,
    extra_context: str = '',
) -> str:
    """Assemble the research prompt. Pure, so it can be asserted on."""
    pool = ', '.join(e['email'] for e in verified_emails) or '(none)'

    parts = [
        f"Business: {business.get('business_name')}",
        f"Website: {business.get('website')}",
        f"Verified email pool: {pool}",
        '',
        'Site text:',
        crawl_excerpt or '(none available)',
    ]
    if extra_context:
        parts += ['', 'Additional page fetched for more detail:', extra_context]

    parts += [
        '',
        'Return pain_signals, a personalization_hook, inferred_tone, a '
        'confidence between 0 and 1, evidence quoted from the text above, and '
        'suggested_email chosen from the verified pool. If confidence is low '
        'and another page on the site would help, set next_url_to_check.',
    ]
    return '\n'.join(parts)


@flow(log_prints=True)
def research_agent(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str = '',
) -> dict:
    """Research one business, escalating once if confidence is low."""
    prompt = build_research_prompt(business, verified_emails, crawl_excerpt)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        return {'status': 'rejected', 'reason': 'llm_schema_failure',
                'research': None, 'passes': 1}

    print(f'pass 1 confidence {first.confidence} '
          f'(threshold {CONFIDENCE_THRESHOLD})')

    if first.confidence >= CONFIDENCE_THRESHOLD:
        return {'status': 'researched', 'research': first.model_dump(mode='json'),
                'passes': 1}

    if not first.next_url_to_check:
        print('below threshold and no next_url_to_check; nothing to escalate')
        return {'status': 'rejected',
                'reason': 'low_confidence_no_escalation_url',
                'research': first.model_dump(mode='json'), 'passes': 1}

    print(f'escalating: fetching {first.next_url_to_check}')
    extra = fetch_html(first.next_url_to_check)

    second_prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt, extra_context=extra or '',
    )
    try:
        second = complete_structured(second_prompt, ResearchOutput,
                                     system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 2 failed: {type(exc).__name__}: {exc}')
        return {'status': 'rejected', 'reason': 'llm_schema_failure',
                'research': first.model_dump(mode='json'), 'passes': 2}

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        return {'status': 'researched',
                'research': second.model_dump(mode='json'), 'passes': 2}

    return {'status': 'rejected',
            'reason': 'low_confidence_after_two_passes',
            'research': second.model_dump(mode='json'), 'passes': 2}
```

Create `flow/agents/__init__.py` as an empty file.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_research_agent.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add flow/agents/__init__.py flow/agents/research.py tests/test_research_agent.py
git commit -m "feat(agents): research-agent flow owning its own escalation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing test for the threshold boundary** *(Review Focus 3)*

```python
class TestThresholdBoundary:
    def test_confidence_exactly_at_threshold_passes(self):
        """Spec 3b says 'confidence >= threshold'. Equality must pass.

        Getting this backwards sends every borderline business to the
        rejection pool silently.
        """
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=CONFIDENCE_THRESHOLD)) as llm:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert llm.call_count == 1

    def test_confidence_just_below_threshold_escalates(self):
        low = _out(confidence=CONFIDENCE_THRESHOLD - 0.01,
                   next_url_to_check="https://happytails.example/about")
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low, _out(confidence=0.95)]), \
             patch('flow.agents.research.fetch_html', return_value="about text"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert result['passes'] == 2
```

Run: `.venv/bin/python -m pytest tests/test_research_agent.py -k Threshold -v`
Expected: PASS. These pin the boundary rather than discovering a bug — if either fails, the comparison in Step 3 is wrong.

- [ ] **Step 7: Write the failing test for the second gate**

```python
    def test_still_low_after_two_passes_is_rejected(self):
        low1 = _out(confidence=0.3,
                    next_url_to_check="https://happytails.example/about")
        low2 = _out(confidence=0.4)
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low1, low2]), \
             patch('flow.agents.research.fetch_html', return_value="more text"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'low_confidence_after_two_passes'
        assert result['passes'] == 2
        # The research is kept: the rejection pool needs it (spec 2.2).
        assert result['research']['confidence'] == 0.4
```

- [ ] **Step 8: Write the failing test for low confidence with no URL** *(Review Focus 4)*

```python
    def test_low_confidence_without_a_url_rejects_without_refetching(self):
        """No URL means nothing to escalate to.

        Re-running pass 1 unchanged would cost a second call for an identical
        prompt, and looping would cost unboundedly.
        """
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.2,
                                     next_url_to_check=None)) as llm, \
             patch('flow.agents.research.fetch_html') as fetch:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'low_confidence_no_escalation_url'
        assert llm.call_count == 1
        fetch.assert_not_called()
```

- [ ] **Step 9: Run the research tests**

Run: `.venv/bin/python -m pytest tests/test_research_agent.py -v`
Expected: PASS (all). Any failure here is a real gate bug, not a test problem.

- [ ] **Step 10: Write the failing test for LLM failure handling**

```python
class TestLLMFailures:
    def test_schema_failure_on_pass_one_is_a_rejection_not_a_crash(self):
        from flow.llm import LLMSchemaError

        with patch('flow.agents.research.complete_structured',
                   side_effect=LLMSchemaError("never validated")):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'llm_schema_failure'

    def test_transport_failure_on_pass_two_keeps_pass_one_research(self):
        from flow.llm import LLMTransportError

        low = _out(confidence=0.3,
                   next_url_to_check="https://happytails.example/about")
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low, LLMTransportError("proxy down")]), \
             patch('flow.agents.research.fetch_html', return_value="t"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['research']['confidence'] == 0.3
```

Run: `.venv/bin/python -m pytest tests/test_research_agent.py -v` → PASS

- [ ] **Step 11: Commit**

```bash
git add tests/test_research_agent.py
git commit -m "test(agents): pin research gate boundaries and failure handling

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The drafting-agent deployment

**Files:**
- Create: `flow/agents/drafting.py`
- Test: `tests/test_drafting_agent.py`

**Interfaces:**
- Consumes: `complete_structured`, `LLMSchemaError`, `LLMTransportError`; `DraftingOutput`, `ResearchOutput`.
- Produces: `drafting_agent(research: dict, verified_emails: list, template_slug: str = "default") -> dict` returning `{"status": "drafted", "draft": <DraftingOutput as dict>}` or `{"status": "rejected", "reason": str}`; `build_drafting_prompt(research, verified_emails, template_slug)`.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import patch

import pytest

from flow.agents.drafting import build_drafting_prompt, drafting_agent
from flow.schemas import DraftingOutput

RESEARCH = {
    "business_name": "Happy Tails", "pain_signals": ["no online booking"],
    "personalization_hook": "phone only", "inferred_tone": "warm",
    "confidence": 0.9, "evidence": ["Call us to book"],
    "suggested_email": "owner@happytails.example", "next_url_to_check": None,
}
EMAILS = [
    {"email": "owner@happytails.example", "verified_at": "2026-10-03T00:00:00Z",
     "source": "leads.business_email"},
    {"email": "info@happytails.example", "verified_at": "2026-10-03T00:00:00Z",
     "source": "research_suggestion"},
]


def _draft(**over):
    base = dict(selected_emails=["owner@happytails.example"],
                subject="Booking enquiries you are missing",
                body="Hi -- noticed you take bookings by phone only.",
                rationale="warm tone, booking friction")
    base.update(over)
    return DraftingOutput(**base)


class TestPromptConstruction:
    def test_prompt_carries_the_hook_tone_and_pool(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS, "default")

        assert "phone only" in prompt
        assert "warm" in prompt
        assert "info@happytails.example" in prompt


class TestHappyPath:
    def test_returns_a_validated_draft(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['draft']['subject'].startswith("Booking")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_drafting_agent.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'flow.agents.drafting'`

- [ ] **Step 3: Write the drafting node**

```python
"""
The drafting-agent deployment.

Its contract: a ResearchOutput plus the verified email pool in, a
DraftingOutput out. The agent chooses which verified address(es) to write to
based on the message's intent -- it is not bound by the researcher's
suggestion (spec section 2.1), but it IS bound to the verified pool.
"""

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.schemas import DraftingOutput

SYSTEM_PROMPT = (
    'You write short, specific first-contact emails to small service business '
    'owners. One concrete observation, one offer, one ask. No flattery, no '
    'placeholders, no invented facts. Write only to addresses you were given.'
)


def build_drafting_prompt(
    research: dict,
    verified_emails: list,
    template_slug: str = 'default',
) -> str:
    """Assemble the drafting prompt. Pure, so it can be asserted on."""
    pool = ', '.join(e['email'] for e in verified_emails) or '(none)'

    return '\n'.join([
        f"Business: {research.get('business_name')}",
        f"Tone observed on their site: {research.get('inferred_tone')}",
        f"Why this matters to them: {research.get('personalization_hook')}",
        f"Pain signals: {'; '.join(research.get('pain_signals') or [])}",
        f"Evidence: {'; '.join(research.get('evidence') or [])}",
        f"Researcher suggested: {research.get('suggested_email')}",
        f"Template: {template_slug}",
        '',
        f"Verified addresses you may write to: {pool}",
        '',
        'Choose selected_emails from the verified addresses above and only '
        'those. Return subject, body, and a rationale for the framing.',
    ])


@flow(log_prints=True)
def drafting_agent(
    research: dict,
    verified_emails: list,
    template_slug: str = 'default',
) -> dict:
    """Draft one outreach email for a researched business."""
    pool = {e['email'] for e in verified_emails}
    prompt = build_drafting_prompt(research, verified_emails, template_slug)

    try:
        draft = complete_structured(prompt, DraftingOutput,
                                    system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'drafting failed: {type(exc).__name__}: {exc}')
        return {'status': 'rejected', 'reason': 'llm_schema_failure',
                'draft': None}

    # The model is told to choose from the pool. Telling it is not the same
    # as it complying, and the cost of non-compliance is mail to an address
    # that was never MX-validated.
    unverified = [e for e in draft.selected_emails if e not in pool]
    if unverified:
        print(f'draft selected unverified addresses: {unverified}')
        return {'status': 'rejected', 'reason': 'selected_unverified_email',
                'draft': draft.model_dump(mode='json'),
                'unverified': unverified}

    print(f'drafted to {draft.selected_emails}: {draft.subject}')
    return {'status': 'drafted', 'draft': draft.model_dump(mode='json')}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_drafting_agent.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add flow/agents/drafting.py tests/test_drafting_agent.py
git commit -m "feat(agents): drafting-agent flow bound to the verified pool

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Write the failing test for pool binding** *(Review Focus 1 — the most important test in this plan)*

```python
class TestVerifiedPoolBinding:
    def test_address_outside_the_pool_is_rejected(self):
        """An invented address would mean mail to an unverified recipient.

        The prompt asks the model to choose from the pool; this test is here
        because asking is not enforcing.
        """
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'selected_unverified_email'
        assert result['unverified'] == ["ceo@happytails.example"]

    def test_partially_valid_selection_is_rejected_whole(self):
        """One good address does not license one bad one."""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example", "ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['unverified'] == ["ceo@happytails.example"]

    def test_agent_may_ignore_the_researcher_suggestion(self):
        """Spec 2.1: not bound by the suggestion, only by the pool."""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'

    def test_multiple_pool_addresses_are_allowed(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example",
                       "info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'

    def test_empty_pool_rejects_any_selection(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()):
            result = drafting_agent.fn(RESEARCH, [])

        assert result['status'] == 'rejected'
        assert result['reason'] == 'selected_unverified_email'
```

- [ ] **Step 7: Run and confirm**

Run: `.venv/bin/python -m pytest tests/test_drafting_agent.py -v`
Expected: PASS (7 tests). If `test_address_outside_the_pool_is_rejected` fails, the check in Step 3 is missing and the node would hand unverified addresses downstream.

- [ ] **Step 8: Commit**

```bash
git add tests/test_drafting_agent.py
git commit -m "test(agents): drafting must not select outside the verified pool

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Serve both nodes as deployments

**Files:**
- Create: `flow/serve_agents.py`
- Modify: `docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: `research_agent`, `drafting_agent`.
- Produces: two registered deployments, `research-agent/research-agent` and `drafting-agent/drafting-agent`, on the Prefect server the process polls.

- [ ] **Step 1: Write the serve entrypoint**

No test precedes this: it is configuration whose only assertion is that the deployments register, which Step 3 checks directly against the server.

```python
"""
Serve the AI node deployments.

One process serves both, so each node is independently runnable, pausable and
rate-limitable in the Prefect UI while costing one process rather than two.
Concurrency limits are the point of the split: a limit on research-agent
bounds total LLM pressure including its own escalation pass.

    $ python flow/serve_agents.py

This must run on the same host as whatever triggers these deployments:
PREFECT_API_URL reads 127.0.0.1:4200 on both the workstation and the prod
host while meaning different servers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.agents.drafting import drafting_agent
from flow.agents.research import research_agent

if __name__ == "__main__":
    serve(
        research_agent.to_deployment(name="research-agent"),
        drafting_agent.to_deployment(name="drafting-agent"),
    )
```

- [ ] **Step 2: Confirm it starts and registers**

```bash
cd /home/nathaniel/leads
.venv/bin/python flow/serve_agents.py
```
Expected: both deployment names listed, then "polling for scheduled runs". A
`ModuleNotFoundError: No module named 'flow'` means the `sys.path` bootstrap is
missing or wrong — the same defect that bit the webhook work.

- [ ] **Step 3: Verify registration, in a second terminal**

```bash
.venv/bin/prefect deployment ls
```
Expected: rows for `research-agent/research-agent` and
`drafting-agent/drafting-agent`.

- [ ] **Step 4: Run drafting-agent from the UI path, with the proxy mocked out**

Prove a deployment actually executes before trusting it with a real model. Run
with a stub so this costs nothing:

```bash
LLM_MODEL=stub .venv/bin/prefect deployment run 'drafting-agent/drafting-agent' \
  --param research='{"business_name":"Smoke Co","inferred_tone":"warm","personalization_hook":"h","pain_signals":[],"evidence":[],"confidence":0.9,"suggested_email":"a@b.com"}' \
  --param verified_emails='[{"email":"a@b.com","verified_at":"2026-10-03T00:00:00Z","source":"leads.business_email"}]'
.venv/bin/prefect flow-run ls --limit 3
```
Expected: the run reaches a terminal state. `Completed` with
`status: rejected, reason: llm_schema_failure` is a **pass** for this step — it
proves the deployment ran, imported cleanly and handled an unreachable or
unconfigured model without crashing. A `Crashed` run is the failure case.

- [ ] **Step 5: Add a concurrency limit to research-agent**

This is why the nodes are separate deployments; without it the split buys
nothing operationally.

```bash
.venv/bin/prefect concurrency-limit create research-agent 2 || true
.venv/bin/prefect concurrency-limit ls
```

- [ ] **Step 6: Stop the serving process**

Ctrl-C it. Record in `docs/ARCHITECTURE.md`: both node deployments, that
`serve()` runs them and does not survive a reboot, that LLM calls go to the
LiteLLM proxy on `:4000` and need `LITELLM_API_KEY`, the
`CONFIDENCE_THRESHOLD` default, and that the drafting node rejects any address
outside the verified pool. Mark as verified locally only.

- [ ] **Step 7: Commit**

```bash
git add flow/serve_agents.py docs/ARCHITECTURE.md
git commit -m "feat(agents): serve research and drafting as prefect deployments

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## What This Plan Does Not Cover

Deliberately out of scope, each needing its own plan:

- **`candidate_selector`** — needs a Python Postgres driver. `psycopg2` is absent from the venv *and* the Prefect worker image; `asyncpg` and `sqlalchemy` are present, and all existing database access in this repo is Node (`scripts/lib/*.mjs`). That choice should be made deliberately, not inside this plan.
- **`email_validation`** — the `mxCheck.pl` subprocess wrapper. `perl` exists in the worker image, but the script's CPAN module dependencies are unverified there and in the prod venv.
- **The parent orchestrator** (`orchestration/research-and-draft`) that sequences selector → validation → research → drafting → import, and the `leads.agent_rejects` rejection pool of spec §2.2. The nodes are callable via `run_deployment` once they exist, which is what makes building them first coherent.
- **`warmbly_import`** — creating the campaign in Warmbly from a `DraftingOutput`.
- **Prompt quality.** This plan builds the machinery and pins its failure modes. Whether the research node produces *useful* hooks is a tuning exercise against real businesses (spec §8), and no unit test can answer it.
