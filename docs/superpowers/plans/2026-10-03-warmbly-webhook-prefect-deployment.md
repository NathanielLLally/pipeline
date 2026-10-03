# Warmbly Inbound Webhook as a Prefect Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Warmbly webhook delivery arrive as a Prefect-managed flow run on the prod Prefect server, visible and operable in the UI, without changing the HTTPS contract Warmbly already depends on.

**Architecture:** The uvicorn listener stays in front, because Prefect's webhook triggers are a Cloud-only feature and self-hosted Prefect has no inbound URL. The listener keeps sole ownership of signature validation and the synchronous challenge echo — it is the only place holding the raw request bytes. For a real event it calls `emit_event()` and returns 200 immediately. A Prefect automation, created by `to_deployment(triggers=[...])`, matches that event by name and runs the `warmbly-webhook-receiver` deployment.

**Tech Stack:** Python 3.9 (prod interpreter), FastAPI, uvicorn, Prefect 3.2.15 self-hosted, pytest.

**Deployment mechanism:** `to_deployment(...)` + `serve()`, exactly the shape of
`flow/wh.py`. Chosen for development: the code is whatever is on the host's disk, so the
edit/restart loop is immediate and there is no push-to-deploy step. `serve()` is a
long-lived process, but that is not a systemd unit — if it needs supervising later it
becomes a service in the existing `prefect-compose` stack with `restart: always`, and
Docker supervises it.

`serve()` must run on the **same host as the listener**, because the listener's
`emit_event` goes to whichever Prefect server its `PREFECT_API_URL` resolves to, and the
serving process only picks up runs from the server it polls. Split them across hosts and
the listener will report `"emitted": true` while nothing ever runs.

**Revisit for production:** `.deploy()` against `local-pool` makes the deployed code a
named commit instead of whatever is on disk, which removes a class of "I edited it but
that is not what is running" mistake. Deferred, not rejected. Note that it pins
dependencies to the worker image, which has `git`, `fastapi`, `httpx`, `pydantic` and
`perl` but **not** `psycopg2`.

**Spec:** `docs/superpowers/specs/2026-09-28-prefect-warmbly-orchestration-design.md` (§2.3 node boundaries, §6.1 infrastructure, §7.1 inbound direction)

## Global Constraints

- **Prod runs Python 3.9.** PEP 604 unions (`str | None`) crash at import; use `Optional[str]`. PEP 585 generics (`dict[str, Any]`) are fine.
- **No hardcoded plumbing.** Hosts, ports, header names, field names and event names come from environment variables. Use the variable that already exists in `.env` before introducing a new name.
- **TDD, per `CLAUDE.md`:** write the failing test first, run it to confirm it fails cleanly, then write the minimal code to pass.
- **`accurateleadinfo.com` is the canonical Prefect** for anything in a delivery path. A deployment visible only in the local UI is not deployed. Verify with `prefect deployment ls` against the prod server, never by reading `PREFECT_API_URL` — it reads `http://127.0.0.1:4200/api` on both hosts and means different servers.
- **Do not add systemd units or service files.** Prefect is the supervisor.
- **`~/leads` on prod is a symlink to `~/src/git/pipeline/`.** Editing locally is not deploying.
- **Never write a raw payload to Postgres without stripping C0 control characters.** A NUL byte aborts the whole transaction.
- **File a GitHub issue for bugs and TODOs** (`gh issue create`) rather than leaving comments in code.

## Review Focus

Input classes the spec implies but no task's happy-path tests exercise, most likely to bite first:

1. **Prefect unreachable when `emit_event` is called.** Warmbly must still get 200, or it will retry and eventually disable the endpoint. An orchestration outage must not look like a webhook failure. → covered by Task 1, Step 6.
2. **Duplicate delivery.** Warmbly retries on timeout; the same `id` arrives twice and produces two flow runs against one real event. → covered by Task 2, Step 6.
3. **Payload containing NUL or other C0 control bytes.** It is passed through Prefect and written to Prefect's Postgres; an unstripped NUL aborts the transaction and loses the event. → covered by Task 1, Step 8.
4. **Clock skew with `WARMBLY_SIGNATURE_MAX_AGE` set.** A host whose clock is behind yields a negative age; one ahead rejects every delivery. Enabling replay protection must not become a silent outage. → covered by Task 1, Step 10.
5. **Oversized body.** `await request.body()` buffers the whole request before any check, so an unbounded body is read into memory before it can be rejected. → covered by Task 1, Step 12.

---

## File Structure

| File | Responsibility |
|---|---|
| `flow/warmbly_events.py` (create) | The one definition of the event name. Imported by both emitter and trigger so they cannot drift. No heavy imports. |
| `flow/warmbly_http_endpoint.py` (modify) | HTTP edge only: signature, challenge echo, emit. Owns raw bytes. |
| `flow/warmbly_webhook_receiver.py` (rewrite) | The `@flow` + deployment that consumes the emitted event. Follows `flow/wh.py`. |
| `tests/test_warmbly_http_endpoint.py` (modify) | Edge behaviour, including the five Review Focus cases. |
| `tests/test_warmbly_webhook_receiver.py` (create) | Flow behaviour and idempotency. |

`flow/wh.py` is the reference implementation for the deployment shape and stays unchanged.

---

### Task 1: Listener emits a Prefect event instead of printing

**Files:**
- Modify: `flow/warmbly_http_endpoint.py` (`route_payload`, constants block)
- Test: `tests/test_warmbly_http_endpoint.py`

**Interfaces:**
- Consumes: `detect_challenge(payload) -> Optional[str]`, `CHALLENGE_HEADER`, existing constants block.
- Produces: `EVENT_NAME: str`, `emit_webhook_event(payload: dict) -> bool` — returns True if the event was accepted by Prefect, False if emission failed. `route_payload` keeps its current signature and return union (`PlainTextResponse | dict`).

- [ ] **Step 1: Write the failing test for emission on a real event**

```python
def test_real_event_emits_prefect_event(self, client):
    with patch('flow.warmbly_http_endpoint.emit_event') as mock_emit:
        client.post("/webhooks/warmbly", json=REAL_EVENT)

    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs['event'] == EVENT_NAME
    assert kwargs['payload'] == REAL_EVENT
    assert 'prefect.resource.id' in kwargs['resource']


def test_challenge_does_not_emit(self, client):
    with patch('flow.warmbly_http_endpoint.emit_event') as mock_emit:
        response = client.post("/webhooks/warmbly", json=TEST_EVENT)

    mock_emit.assert_not_called()
    assert response.text == CHALLENGE
```

Add `EVENT_NAME` and `emit_event` to the existing import block from `flow.warmbly_http_endpoint`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_warmbly_http_endpoint.py -k "emits_prefect_event or does_not_emit" -v`
Expected: FAIL at collection with `ImportError: cannot import name 'EVENT_NAME'`

- [ ] **Step 3: Add the event name in its own module**

The emitter and the deployment trigger must agree on this string, and the receiver runs
in the worker where importing the FastAPI app would construct `app = FastAPI(...)` and
read a dozen environment variables for nothing. So the constant gets its own module.

Create `flow/warmbly_events.py`:

```python
"""
The Warmbly webhook event name.

Lives alone, with no heavy imports, because both the HTTP listener (which
emits the event) and the receiver deployment (whose trigger matches it) import
it, and the receiver runs inside the Prefect worker.

The name is the entire routing key: an automation created by
to_deployment/deploy(triggers=[...]) matches on name alone, so a name reused
across consumers fans out to all of them.
"""

import os

EVENT_NAME = os.environ.get(
    'WARMBLY_WEBHOOK_EVENT_NAME', 'warmbly.webhook.received'
)
```

Then in `flow/warmbly_http_endpoint.py`, beside the other imports:

```python
from flow.warmbly_events import EVENT_NAME
```

- [ ] **Step 4: Replace the print-only stub with emission**

Add the import at the top of the file, beside the existing `from prefect import flow, task`:

```python
from prefect.events import emit_event
```

Replace the body of `route_payload` after the challenge branch:

```python
    emitted = emit_webhook_event(payload)

    return {
        "status": "accepted",
        "event_type": payload.get('event_type'),
        "event_name": EVENT_NAME,
        "emitted": emitted,
    }
```

And add the new function above `route_payload`:

```python
def emit_webhook_event(payload: dict[str, Any]) -> bool:
    """
    Hand a real Warmbly event to Prefect.

    Returns whether Prefect accepted it. Emission failure is reported, never
    raised: see Step 6 for why the HTTP response must not depend on it.
    """
    endpoint_id = (payload.get('data') or {}).get('endpoint_id', 'unknown')

    emit_event(
        event=EVENT_NAME,
        resource={
            'prefect.resource.id': f'warmbly.webhook.{endpoint_id}',
            'warmbly.event_type': str(payload.get('event_type')),
        },
        payload=payload,
    )
    return True
```

Delete `trigger_research_flow` and its now-unused `prefect_api_url` lookup; it only ever printed.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_warmbly_http_endpoint.py -v`
Expected: PASS. Any test still referencing `trigger_research_flow` must be updated to patch `emit_event` instead.

- [ ] **Step 6: Write the failing test for Prefect being unreachable** *(Review Focus 1)*

```python
def test_emit_failure_still_returns_200(self, client):
    """An orchestration outage must not look like a webhook failure.

    Warmbly retries non-2xx and eventually disables the endpoint, so losing
    Prefect must not cost us the delivery channel too.
    """
    with patch(
        'flow.warmbly_http_endpoint.emit_event',
        side_effect=ConnectionError("prefect unreachable"),
    ):
        response = client.post("/webhooks/warmbly", json=REAL_EVENT)

    assert response.status_code == 200
    assert response.json()['emitted'] is False
```

Run it: expect FAIL with `ConnectionError` propagating as a 500.

- [ ] **Step 7: Make emission failure non-fatal**

```python
    try:
        emit_event(
            event=EVENT_NAME,
            resource={
                'prefect.resource.id': f'warmbly.webhook.{endpoint_id}',
                'warmbly.event_type': str(payload.get('event_type')),
            },
            payload=payload,
        )
        return True
    except Exception as exc:
        # Deliberately broad: nothing Prefect can raise is worth a 500 to
        # Warmbly. Log loudly so the dropped event is findable.
        print(f"FAILED to emit {EVENT_NAME}: {type(exc).__name__}: {exc}")
        print(f"  dropped payload id={payload.get('id')}")
        return False
```

Run: `.venv/bin/python -m pytest tests/test_warmbly_http_endpoint.py -v` → PASS

- [ ] **Step 8: Write the failing test for control bytes in the payload** *(Review Focus 3)*

```python
def test_control_bytes_are_stripped_before_emitting(self, client):
    body = json.dumps({
        "id": "ctl-1", "event_type": "contact.created",
        "data": {"note": "line1\u0000line2\u0001end"},
    }).encode()

    with patch('flow.warmbly_http_endpoint.emit_event') as mock_emit:
        client.post("/webhooks/warmbly", content=body,
                    headers={'Content-Type': 'application/json'})

    emitted = mock_emit.call_args.kwargs['payload']
    assert '\u0000' not in json.dumps(emitted)
    assert emitted['data']['note'] == "line1line2end"
```

Run it: expect FAIL — the NUL survives.

- [ ] **Step 9: Strip C0 control characters before emitting**

Add above `emit_webhook_event`:

```python
# Tab, newline and carriage return are legitimate in text; the rest of the C0
# range is not, and a NUL aborts the whole Postgres transaction that Prefect
# uses to persist the event.
_ALLOWED_CONTROL = {'\t', '\n', '\r'}


def strip_control_characters(value: Any) -> Any:
    """Recursively remove C0 control characters from strings in a payload."""
    if isinstance(value, str):
        return ''.join(
            c for c in value
            if c in _ALLOWED_CONTROL or ord(c) >= 0x20
        )
    if isinstance(value, dict):
        return {k: strip_control_characters(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_control_characters(v) for v in value]
    return value
```

In `emit_webhook_event`, use `payload=strip_control_characters(payload)`.

Run: PASS

- [ ] **Step 10: Write the failing test for clock skew** *(Review Focus 4)*

```python
def test_future_timestamp_is_not_rejected_as_stale(self):
    """A host whose clock is behind Warmbly's yields a negative age.

    Negative age means 'from the future', not 'expired', and must never trip
    the max-age check -- that would reject every delivery.
    """
    secret, body = 'test_secret', b'{"a":1}'
    os.environ['WARMBLY_WEBHOOK_SECRET'] = secret
    future = str(int(time.time()) + 600)
    signed = build_signed_payload(future, body)
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

    report = describe_signature_check(
        body, f't={future},v1={digest}', max_age_seconds=300
    )

    assert report['timestamp_age_seconds'] < 0
    assert report['matched'] is True
```

Run it: expect FAIL — `-600 > 300` is False so it passes by luck today; confirm it fails if the comparison is written as `abs(age) > max_age`. If it already passes, keep the test as a regression guard and note that in the commit message.

- [ ] **Step 11: Make the age check directional**

In `describe_signature_check`, ensure the comparison rejects only genuinely old signatures and records skew separately:

```python
    age = report['timestamp_age_seconds']
    if age is not None and age < 0:
        report['clock_skew_seconds'] = -age
    if max_age_seconds is not None and age is not None and age > max_age_seconds:
```

Run: PASS

- [ ] **Step 12: Write the failing test for an oversized body** *(Review Focus 5)*

```python
def test_oversized_body_is_rejected_before_parsing(self, client):
    os.environ['WARMBLY_MAX_BODY_BYTES'] = '1024'
    try:
        response = client.post(
            "/webhooks/warmbly",
            content=b'{"pad":"' + b'x' * 4096 + b'"}',
            headers={'Content-Type': 'application/json'},
        )
    finally:
        os.environ.pop('WARMBLY_MAX_BODY_BYTES', None)

    assert response.status_code == 413
```

Run it: expect FAIL with 200 or 401.

- [ ] **Step 13: Enforce a body size limit**

Add to the constants block:

```python
_max_body = os.environ.get('WARMBLY_MAX_BODY_BYTES', '').strip()
MAX_BODY_BYTES = int(_max_body) if _max_body.isdigit() else 1_048_576
```

In `receive_warmbly_webhook`, immediately after `body_bytes = await request.body()`:

```python
    if len(body_bytes) > MAX_BODY_BYTES:
        print(f"REJECTED 413: body {len(body_bytes)}B exceeds {MAX_BODY_BYTES}B")
        raise HTTPException(status_code=413, detail="Payload too large")
```

Note in a comment that this bounds post-buffering work only; uvicorn has already read the body, so a hard limit belongs at traefik if that matters.

Run: `.venv/bin/python -m pytest tests/test_warmbly_http_endpoint.py -v` → PASS (all)

- [ ] **Step 14: Verify Python 3.9 compatibility**

Run: `grep -nE ':[^=]*\b(str|int|bytes|dict|list|bool|Any)\s*\|' flow/warmbly_http_endpoint.py`
Expected: no output. Any match is a PEP 604 union that will crash on prod's interpreter.

- [ ] **Step 15: Commit**

```bash
git add flow/warmbly_http_endpoint.py tests/test_warmbly_http_endpoint.py
git commit -m "feat(warmbly): emit prefect event from webhook listener

Replaces the print-only trigger_research_flow stub with emit_event, so a
delivery becomes a Prefect-managed flow run. Emission failure returns 200
with emitted=false rather than a 500, because Warmbly disables endpoints
that return errors.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Receiver flow and deployment

**Files:**
- Rewrite: `flow/warmbly_webhook_receiver.py`
- Create: `tests/test_warmbly_webhook_receiver.py`

**Interfaces:**
- Consumes: `EVENT_NAME` from `flow.warmbly_http_endpoint` (single source of truth — the emitter and the trigger must agree, so the trigger imports it rather than repeating the literal).
- Produces: `warmbly_webhook_receiver(payload: dict) -> dict` returning `{"status", "event_type", "organization_id", "event_id"}`; `classify_event(payload) -> str`.

- [ ] **Step 1: Write the failing test**

```python
import json
from unittest.mock import patch

from flow.warmbly_webhook_receiver import (
    classify_event,
    warmbly_webhook_receiver,
)

CONTACT_EVENT = {
    "id": "aa11", "event_type": "contact.created",
    "organization_id": "org-1", "data": {"contact_id": "c1"},
}


class TestClassifyEvent:
    def test_classifies_contact_events(self):
        assert classify_event(CONTACT_EVENT) == 'contact'

    def test_classifies_campaign_events(self):
        assert classify_event({"event_type": "campaign.reply_received"}) == 'campaign'

    def test_unknown_prefix_is_other(self):
        assert classify_event({"event_type": "weird.thing"}) == 'other'

    def test_missing_event_type_is_other(self):
        assert classify_event({}) == 'other'


class TestReceiverFlow:
    def test_returns_event_identity(self):
        result = warmbly_webhook_receiver(CONTACT_EVENT)

        assert result['status'] == 'processed'
        assert result['event_type'] == 'contact.created'
        assert result['event_id'] == 'aa11'
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_warmbly_webhook_receiver.py -v`
Expected: FAIL — `ImportError: cannot import name 'classify_event'`

- [ ] **Step 3: Rewrite the receiver following `flow/wh.py`**

Replace the entire contents of `flow/warmbly_webhook_receiver.py`:

```python
"""
Warmbly webhook receiver flow.

Consumes the Prefect event emitted by flow/warmbly_http_endpoint.py and
appears in the Prefect UI as a managed deployment. Follows the deployment
shape demonstrated in flow/wh.py.

Signature validation is deliberately NOT here: it requires the raw request
bytes, which only the HTTP listener holds. Re-serialising parsed JSON to
re-check an HMAC cannot work.

Serve it (long-lived; must run on the same host as the listener):
    $ python flow/warmbly_webhook_receiver.py
"""

from typing import Any

from prefect import flow, serve, task
from prefect.events import DeploymentEventTrigger

from flow.warmbly_events import EVENT_NAME


@task
def classify_event(payload: dict[str, Any]) -> str:
    """Classify an event by its dotted prefix for downstream routing."""
    event_type = payload.get('event_type') or ''
    prefix = event_type.split('.', 1)[0]

    if prefix in ('contact', 'campaign', 'email', 'webhook'):
        return prefix
    return 'other'


@flow(log_prints=True)
def warmbly_webhook_receiver(payload: dict[str, Any]) -> dict:
    """Process one Warmbly webhook event."""
    event_class = classify_event(payload)

    print(f"Event: {payload.get('event_type')} (class: {event_class})")
    print(f"Organization: {payload.get('organization_id')}")
    print(f"Event id: {payload.get('id')}")

    return {
        "status": "processed",
        "event_type": payload.get('event_type'),
        "organization_id": payload.get('organization_id'),
        "event_id": payload.get('id'),
        "event_class": event_class,
    }


if __name__ == "__main__":
    deployment = warmbly_webhook_receiver.to_deployment(
        name="warmbly-webhook-receiver",
        triggers=[
            DeploymentEventTrigger(
                expect={EVENT_NAME},
                parameters={
                    "payload": {
                        "__prefect_kind": "json",
                        "value": {
                            "__prefect_kind": "jinja",
                            "template": "{{ event.payload | tojson }}",
                        },
                    }
                },
            )
        ],
    )

    serve(deployment)
```



- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_warmbly_webhook_receiver.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing idempotency test** *(Review Focus 2)*

```python
def test_duplicate_event_id_is_not_processed_twice(self):
    """Warmbly retries on timeout, so the same id can arrive twice."""
    from flow.warmbly_webhook_receiver import seen_event_ids

    seen_event_ids.clear()
    first = warmbly_webhook_receiver(CONTACT_EVENT)
    second = warmbly_webhook_receiver(CONTACT_EVENT)

    assert first['status'] == 'processed'
    assert second['status'] == 'duplicate'
```

Run it: expect FAIL — `ImportError: cannot import name 'seen_event_ids'`

- [ ] **Step 6: Add in-run duplicate suppression**

```python
# Best-effort duplicate suppression within a worker process. This does NOT
# survive a restart and is not shared between workers; a durable guard needs a
# unique index on event id in the leads schema. Filed as a follow-up issue
# rather than solved here, because the pipeline has no table for it yet.
seen_event_ids: set = set()
```

In the flow, before classification:

```python
    event_id = payload.get('id')
    if event_id and event_id in seen_event_ids:
        print(f"Duplicate event {event_id}; skipping")
        return {
            "status": "duplicate",
            "event_type": payload.get('event_type'),
            "event_id": event_id,
        }
    if event_id:
        seen_event_ids.add(event_id)
```

Run: PASS

- [ ] **Step 7: File the follow-up issue for durable idempotency**

```bash
gh issue create \
  --title "Warmbly webhook idempotency is process-local" \
  --body "flow/warmbly_webhook_receiver.py suppresses duplicate event ids in an in-memory set. It does not survive a worker restart and is not shared across workers, so a retried Warmbly delivery can still be processed twice. Needs a table with a unique index on the Warmbly event id."
```

- [ ] **Step 8: Commit**

```bash
git add flow/warmbly_events.py flow/warmbly_webhook_receiver.py tests/test_warmbly_webhook_receiver.py
git commit -m "feat(warmbly): receiver flow as a prefect deployment

Rewrites the receiver to the flow/wh.py deployment shape, triggered by the
event the listener emits. Drops the old signature-validation task, which
re-serialised parsed JSON and could never have matched the HMAC.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Serve the deployment and verify end to end

Verification runs in two phases: the whole chain locally first, where the loop is fast
and Warmbly is not involved, then on prod where the real traffic lands. Do not skip to
prod — a broken import or a trigger that failed to register looks identical to a webhook
problem from the outside, and that confusion is expensive.

**Files:**
- Modify: `docs/ARCHITECTURE.md` (record the verified topology)

**Interfaces:**
- Consumes: both files from Tasks 1 and 2.
- Produces: a `warmbly-webhook-receiver` deployment and automation on whichever Prefect
  server the serving process polls.

- [ ] **Step 1: Serve the deployment locally**

```bash
cd /home/nathaniel/leads
.venv/bin/python flow/warmbly_webhook_receiver.py
```

Leave it running in its own terminal. Expected: output naming the served deployment and
then polling. A traceback here is an import problem, not a Prefect problem.

- [ ] **Step 2: Verify the deployment and its automation registered**

In a second terminal:

```bash
cd /home/nathaniel/leads
.venv/bin/prefect deployment ls
.venv/bin/prefect automation ls
```
Expected: a `warmbly-webhook-receiver` row, and an automation expecting
`warmbly.webhook.received`. If the automation is missing, the trigger did not register
and the emitted event will go nowhere — the listener would log `"emitted": true` and
nothing would run, which is the silent failure this plan exists to prevent.

- [ ] **Step 3: Run the deployment directly, before any HTTP is involved**

This separates "can this flow execute at all" from "does the event reach it".

```bash
.venv/bin/prefect deployment run \
  'warmbly-webhook-receiver/warmbly-webhook-receiver' \
  --param payload='{"id":"smoke-1","event_type":"contact.created","organization_id":"org-smoke","data":{}}'
.venv/bin/prefect flow-run ls --limit 3
```
Expected: the run reaches `Completed`, and the serving terminal logs the event type,
organization and event id.

- [ ] **Step 4: Start the listener locally and prove the full chain**

In a third terminal:

```bash
cd /home/nathaniel/leads
WEBHOOK_PORT=8765 .venv/bin/python flow/warmbly_http_endpoint.py
```

Then, in the second terminal, post a synthetic event with no signing secret set:

```bash
curl -sS -i -X POST http://127.0.0.1:8765/webhooks/warmbly \
  -H 'Content-Type: application/json' \
  -d '{"id":"chain-1","event_type":"contact.created","organization_id":"org-1","data":{"contact_id":"c1"}}'
```

Expected, in order:
1. `HTTP/1.1 200`, body contains `"emitted": true`.
2. The serving terminal logs a new flow run for `chain-1`.
3. `prefect flow-run ls --limit 3` shows it `Completed`.

This is the real proof. Everything after it is the same chain on a different host.

- [ ] **Step 5: Prove the challenge still short-circuits**

```bash
curl -sS -X POST http://127.0.0.1:8765/webhooks/warmbly \
  -H 'Content-Type: application/json' \
  -d '{"id":"ch-1","event_type":"webhook.test","data":{"challenge":"whcg_local_probe"}}'
```
Expected: the body is exactly `whcg_local_probe`, and **no** new flow run appears. A
flow run here means `detect_challenge` is not short-circuiting and Warmbly verification
will be answered with a flow run it did not ask for.

- [ ] **Step 6: Stop the local processes**

Ctrl-C both the listener and the serving process. Local verification is done; prod runs
its own copies.

- [ ] **Step 7: Deploy both files to the prod host**

```bash
cd /home/nathaniel/leads
scp -P 2222 flow/warmbly_http_endpoint.py flow/warmbly_events.py \
  flow/warmbly_webhook_receiver.py \
  nathaniel@accurateleadinfo.com:/home/nathaniel/src/git/pipeline/flow/
ssh -p 2222 nathaniel@accurateleadinfo.com \
  "cd /home/nathaniel/src/git/pipeline && python3.9 -m py_compile flow/warmbly_http_endpoint.py flow/warmbly_events.py flow/warmbly_webhook_receiver.py && echo OK"
```
Expected: `OK`. A `TypeError` about `|` means a PEP 604 union slipped past Task 1
Step 14 and would crash on prod's 3.9 interpreter.

Remember `~/leads` on prod is a symlink to `~/src/git/pipeline/`; editing locally is not
deploying.

- [ ] **Step 8: Serve on prod**

Must be the same host as the listener, so the emitted event and the serving process meet
on the same Prefect server.

```bash
ssh -p 2222 nathaniel@accurateleadinfo.com \
  "cd /home/nathaniel/src/git/pipeline && nohup python flow/warmbly_webhook_receiver.py > /tmp/warmbly-receiver-serve.log 2>&1 & sleep 5; tail -20 /tmp/warmbly-receiver-serve.log"
```

Note plainly in the log line that this process does not survive a reboot. That is
acceptable for development and is the thing `.deploy()` or a compose service fixes
later; it is recorded in `docs/ARCHITECTURE.md` at Step 11 rather than left as folklore.

- [ ] **Step 9: Verify registration landed on the PROD server**

```bash
ssh -p 2222 nathaniel@accurateleadinfo.com \
  "docker exec prefect-compose-prefect-server-1 prefect deployment ls; docker exec prefect-compose-prefect-server-1 prefect automation ls"
```
Expected: `warmbly-webhook-receiver` and its automation. The prod server had **zero**
deployments before this plan, so this is the first evidence that anything is registered
there at all.

- [ ] **Step 10: Restart the listener and trigger real Warmbly traffic**

The listener runs as a foreground process in the owner's interactive shell and its
stdout is their live log view, so ask them to Ctrl-C and re-run it rather than killing it
from here.

Then ask them to press the webhook test button in the Warmbly UI. Expected: the
challenge is echoed, and **no** flow run appears. Then ask them to cause a real event.
Expected: the listener logs `"emitted": true` and a flow run appears in the prod UI and
completes.

If the listener logs `"emitted": false`, Prefect was unreachable from it and the printed
exception names why. The 200 returned to Warmbly is correct behaviour, not a bug.

- [ ] **Step 11: Record the verified topology**

Append to the webhook section of `docs/ARCHITECTURE.md`: that the receiver is served by
a `nohup` process on prod polling the prod Prefect server, that this does not survive a
reboot, the event name joining emitter to automation, and that `serve()` and the listener
must share a host. Mark anything not actually observed as unverified.

- [ ] **Step 12: Commit**

```bash
git add docs/ARCHITECTURE.md
git commit -m "docs: record verified warmbly webhook prefect topology

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Decisions Taken, and What Is Deferred

**`serve()` for development** — chosen by the owner. The code served is whatever is on
the host's disk, so the edit/restart loop is immediate with no push step. This is not a
systemd unit: if it needs supervising it becomes a service in the existing
`prefect-compose` stack with `restart: always`.

**`.deploy()` deferred, not rejected.** It makes the running code a named commit rather
than whatever is on disk, removing a whole class of "I changed it but that is not what is
running" mistake — a mistake this project has actually paid for. Revisit when the
webhook stops being developed. It would pin dependencies to the worker image, which has
`git`, `fastapi`, `httpx`, `pydantic` and `perl` but **not** `psycopg2`.

**Known gap while on `serve()`:** the serving process is a bare `nohup` and will not
survive a reboot of the prod host. Webhooks will still return 200 and Prefect will still
record the events, so the flow runs queue as `Late` rather than being lost — but nothing
will process them until it is restarted.

**Deferred to the pipeline plan:** `psycopg2` is absent from the worker image, so
`candidate_selector` cannot run under that worker. `mxCheck.pl`'s CPAN module
dependencies are unverified in both the image and the prod venv and need checking before
`email-validation` is assumed to work.
