"""
Warmbly HTTP Endpoint (Direct)

A direct HTTP server that listens for Warmbly webhook events.

This is an alternative to using Prefect Cloud's managed webhook service.
Use this when:
- You're running Prefect locally or self-hosted
- You want to expose the webhook on a custom port
- You want to handle the HTTP layer directly

Usage:
    $ python flow/warmbly_http_endpoint.py

    Server listens on http://localhost:8765/webhooks/warmbly

    Then configure Warmbly to POST to:
    https://crm.happytailspawcare.com:8765/webhooks/warmbly
    (with proper port forwarding/firewall rules)

Dependencies:
    pip install fastapi uvicorn httpx
"""

import base64
import json
import hashlib
import hmac
import os
import time
import asyncio
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from uvicorn import run as uvicorn_run
import httpx

import sys
from pathlib import Path

# These modules are started directly as scripts on the prod host
# (`python flow/warmbly_http_endpoint.py`), which puts `flow/` on sys.path
# rather than the repo root, breaking absolute `flow.*` imports. Adding the
# repo root keeps both `python flow/x.py` and `python -m flow.x` working.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow, task
from prefect.events import emit_event

from flow.warmbly_events import EVENT_NAME


app = FastAPI(title="Warmbly Webhook Receiver")


# Webhook wire protocol. Overridable so the plumbing is not baked into the code
# if Warmbly renames a header or moves the challenge field.
SIGNATURE_HEADER = os.environ.get(
    'WARMBLY_SIGNATURE_HEADER', 'X-Warmbly-Signature'
)

# Warmbly signs with Stripe-style elements: "t=<unix>,v1=<hex hmac-sha256>",
# where the signed bytes are "<timestamp>.<raw body>". Observed 2026-10-03.
SIGNATURE_TIMESTAMP_KEY = os.environ.get('WARMBLY_SIGNATURE_TIMESTAMP_KEY', 't')
SIGNATURE_VERSION_KEY = os.environ.get('WARMBLY_SIGNATURE_VERSION_KEY', 'v1')
SIGNATURE_SEPARATOR = os.environ.get('WARMBLY_SIGNATURE_SEPARATOR', '.')

# Replay rejection is opt-in: unset means log the age but accept. Enabling it
# turns clock drift into 401s, so it should be switched on deliberately.
_max_age = os.environ.get('WARMBLY_SIGNATURE_MAX_AGE', '').strip()
SIGNATURE_MAX_AGE = int(_max_age) if _max_age.isdigit() else None
CHALLENGE_HEADER = os.environ.get(
    'WARMBLY_CHALLENGE_HEADER', 'X-Warmbly-Webhook-Challenge'
)
CHALLENGE_FIELD = os.environ.get('WARMBLY_CHALLENGE_FIELD', 'challenge')
TIMESTAMP_HEADER = os.environ.get(
    'WARMBLY_TIMESTAMP_HEADER', 'X-Warmbly-Timestamp'
)

# Verbose per-request logging. Off unless WARMBLY_WEBHOOK_DEBUG is truthy;
# rejections are logged either way, since a silent 401 is undiagnosable.
DEBUG = os.environ.get('WARMBLY_WEBHOOK_DEBUG', '').strip().lower() in (
    '1', 'true', 'yes', 'on'
)

REDACTED = '<redacted>'
REDACTED_HEADERS = frozenset(
    h.strip().lower()
    for h in os.environ.get(
        'WARMBLY_REDACTED_HEADERS',
        'authorization,cookie,set-cookie,proxy-authorization',
    ).split(',')
    if h.strip()
)

# How much of a signature digest to show in logs. Enough to compare two
# values by eye, not enough to replay one.
SIGNATURE_PREVIEW_CHARS = int(
    os.environ.get('WARMBLY_SIGNATURE_PREVIEW_CHARS', '12')
)
BODY_PREVIEW_CHARS = int(os.environ.get('WARMBLY_BODY_PREVIEW_CHARS', '600'))


def _preview(value: str, limit: int) -> str:
    """Truncate a value for logging, marking that it was truncated."""
    if len(value) <= limit:
        return value
    return value[:limit] + '\u2026'


def parse_signature_header(header: str) -> dict[str, list[str]]:
    """
    Parse a "t=123,v1=abc,v1=def" signature header into its elements.

    Values are collected into lists because a sender may include more than one
    digest during a secret rotation. Only the first "=" is a separator, so a
    base64 value keeps its padding.
    """
    elements: dict[str, list[str]] = {}

    for part in header.split(','):
        key, sep, value = part.strip().partition('=')
        if not sep:
            # Not "key=value" - nothing to record.
            continue
        elements.setdefault(key.strip(), []).append(value.strip())

    return elements


def build_signed_payload(
    timestamp: Optional[str],
    payload_bytes: bytes,
) -> bytes:
    """
    Reconstruct the bytes the remote signed: "<timestamp>.<raw body>".

    Without a timestamp the raw body is the best guess. The body must be the
    bytes as received - re-serialising the parsed JSON changes the digest.
    """
    if not timestamp:
        return payload_bytes

    return f'{timestamp}{SIGNATURE_SEPARATOR}'.encode() + payload_bytes


def signature_age_seconds(timestamp: Optional[str]) -> Optional[int]:
    """Seconds since the signature timestamp, or None if it is unusable."""
    if not timestamp:
        return None

    try:
        return int(time.time()) - int(timestamp)
    except (TypeError, ValueError):
        return None


def describe_signature_check(
    payload_bytes: bytes,
    signature_header: Optional[str],
    max_age_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """
    Explain why a signature check passed or failed.

    Returns a log-safe report: the secret never appears in it, and digests are
    truncated to SIGNATURE_PREVIEW_CHARS so a log cannot be used to replay a
    request.
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if max_age_seconds is None:
        max_age_seconds = SIGNATURE_MAX_AGE

    report: dict[str, Any] = {
        'secret_configured': bool(secret),
        'header_name_expected': SIGNATURE_HEADER,
        'header_present': signature_header is not None,
        'body_bytes': len(payload_bytes),
    }

    if not secret:
        # Validation is skipped entirely when no secret is set.
        report['matched'] = True
        report['reason'] = 'no secret configured; signature check skipped'
        return report

    if not signature_header:
        report['matched'] = False
        report['reason'] = f'no {SIGNATURE_HEADER} header on the request'
        return report

    elements = parse_signature_header(signature_header)
    timestamps = elements.get(SIGNATURE_TIMESTAMP_KEY, [])
    digests = elements.get(SIGNATURE_VERSION_KEY, [])
    timestamp = timestamps[0] if timestamps else None

    report['elements_received'] = sorted(elements)
    report['timestamp'] = timestamp
    report['timestamp_age_seconds'] = signature_age_seconds(timestamp)

    if not digests:
        report['matched'] = False
        report['reason'] = (
            f'header has no {SIGNATURE_VERSION_KEY}= element; got '
            f'{sorted(elements)}'
        )
        return report

    signed_bytes = build_signed_payload(timestamp, payload_bytes)
    expected = hmac.new(
        secret.encode(), signed_bytes, hashlib.sha256
    ).hexdigest()

    report['signed_payload_form'] = (
        f'{SIGNATURE_TIMESTAMP_KEY}{SIGNATURE_SEPARATOR}body'
        if timestamp
        else 'body'
    )
    report['signature_expected'] = _preview(expected, SIGNATURE_PREVIEW_CHARS)
    report['signatures_received'] = [
        _preview(d, SIGNATURE_PREVIEW_CHARS) for d in digests
    ]
    report['matched'] = any(
        hmac.compare_digest(d, expected) for d in digests
    )

    if not report['matched']:
        report['reason'] = (
            'digest mismatch: wrong secret, or the body is signed in a form '
            'other than ' + report['signed_payload_form']
        )
        return report

    age = report['timestamp_age_seconds']
    if age is not None and age < 0:
        # Negative age means the signature is timestamped in the future, i.e.
        # our clock is behind the sender's. That is skew to diagnose, never
        # expiry -- treating it as expiry would reject every delivery.
        report['clock_skew_seconds'] = -age
    if max_age_seconds is not None and age is not None and age > max_age_seconds:
        report['matched'] = False
        report['reason'] = (
            f'signature is {age}s old, older than the configured maximum of '
            f'{max_age_seconds}s (possible replay, or clock drift)'
        )
        return report

    report['reason'] = 'signature matched'
    return report


def probe_signature_schemes(
    payload_bytes: bytes,
    signature_header: str,
) -> list[str]:
    """
    Find which signing scheme, if any, reproduces the digest we were sent.

    A mismatch has two very different causes: the secret is wrong, or the
    secret is right but the remote signs something other than what we assume.
    This separates them. An empty list points at the secret; a non-empty one
    names the form actually in use, which is what the constants should be set
    to.
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if not secret or not signature_header:
        return []

    elements = parse_signature_header(signature_header)
    digests = elements.get(SIGNATURE_VERSION_KEY, [])
    timestamps = elements.get(SIGNATURE_TIMESTAMP_KEY, [])
    timestamp = timestamps[0] if timestamps else None

    if not digests:
        # Fall back to treating the whole header as a bare digest.
        digests = [signature_header]

    candidates: dict[str, bytes] = {'body': payload_bytes}
    if timestamp:
        key = SIGNATURE_TIMESTAMP_KEY
        candidates[f'{key}.body'] = f'{timestamp}.'.encode() + payload_bytes
        candidates[f'{key}+body'] = timestamp.encode() + payload_bytes

    matches = []
    for label, signed_bytes in candidates.items():
        mac = hmac.new(secret.encode(), signed_bytes, hashlib.sha256)
        hex_digest = mac.hexdigest()
        b64_digest = base64.b64encode(mac.digest()).decode()

        for received in digests:
            if hmac.compare_digest(received, hex_digest):
                matches.append(f'hex({label})')
            if hmac.compare_digest(received, b64_digest):
                matches.append(f'base64({label})')

    return matches


def describe_request(headers: dict[str, str], payload_bytes: bytes) -> dict:
    """
    Dump an inbound request for debugging, with credential headers redacted.

    Header *names* are always kept: when a signature check fails because the
    remote uses a header we are not looking for, the name is the answer.
    """
    safe_headers = {
        name: (REDACTED if name.lower() in REDACTED_HEADERS else value)
        for name, value in headers.items()
    }

    return {
        'headers': safe_headers,
        'body_bytes': len(payload_bytes),
        'body_preview': _preview(
            payload_bytes.decode('utf-8', errors='replace'),
            BODY_PREVIEW_CHARS,
        ),
    }


def validate_webhook_signature(
    payload_bytes: bytes,
    signature_header: Optional[str]
) -> bool:
    """
    Validate Warmbly webhook signature.

    Header format: <SIGNATURE_HEADER>: <SIGNATURE_ALGORITHM>=<hex>

    describe_signature_check does the work so that the decision and the logged
    explanation of it can never disagree.
    """
    return describe_signature_check(payload_bytes, signature_header)['matched']


# Tab, newline and carriage return are legitimate in text; the rest of the C0
# range is not, and a NUL aborts the whole Postgres transaction that Prefect
# uses to persist the event.
_ALLOWED_CONTROL = {'\t', '\n', '\r'}


DEFAULT_MAX_BODY_BYTES = 1_048_576


def max_body_bytes() -> int:
    """
    Largest body we will process, read per request.

    Read at call time rather than import time so the limit can be changed
    without restarting the listener, consistent with the rest of this
    module's configuration.
    """
    configured = os.environ.get('WARMBLY_MAX_BODY_BYTES', '').strip()
    return int(configured) if configured.isdigit() else DEFAULT_MAX_BODY_BYTES


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


def emit_webhook_event(payload: dict[str, Any]) -> bool:
    """
    Hand a real Warmbly event to Prefect.

    Returns whether Prefect accepted it.
    """
    endpoint_id = (payload.get('data') or {}).get('endpoint_id', 'unknown')

    try:
        emit_event(
            event=EVENT_NAME,
            resource={
                'prefect.resource.id': f'warmbly.webhook.{endpoint_id}',
                'warmbly.event_type': str(payload.get('event_type')),
            },
            payload=strip_control_characters(payload),
        )
        return True
    except Exception as exc:
        # Deliberately broad: nothing Prefect can raise is worth a 500 to
        # Warmbly, which retries non-2xx and eventually disables the
        # endpoint. Log loudly so the dropped event is findable.
        print(f"FAILED to emit {EVENT_NAME}: {type(exc).__name__}: {exc}")
        print(f"  dropped payload id={payload.get('id')}")
        return False


def detect_challenge(payload: dict[str, Any]) -> Optional[str]:
    """
    Return the verification challenge in a payload, or None if there isn't one.

    Warmbly verifies an endpoint by sending an event carrying a challenge value
    it expects echoed back. Any payload can carry one, so detection is by field
    presence rather than by event type.
    """
    data = payload.get('data')
    if not isinstance(data, dict):
        return None

    challenge = data.get(CHALLENGE_FIELD)
    if not challenge or not isinstance(challenge, str):
        return None

    return challenge


async def route_payload(payload: dict[str, Any]):
    """
    Dispatch a validated webhook payload.

    A verification challenge is answered by echoing the value back in both the
    body and the challenge header, and goes no further — a verification ping is
    not a real event. Everything else routes to the research pipeline.
    """
    challenge = detect_challenge(payload)
    if challenge:
        print(f"Echoing verification challenge: {challenge}")
        return PlainTextResponse(
            content=challenge,
            headers={CHALLENGE_HEADER: challenge},
        )

    emitted = emit_webhook_event(payload)

    return {
        "status": "accepted",
        "event_type": payload.get('event_type'),
        "event_name": EVENT_NAME,
        "emitted": emitted,
    }


@app.post("/webhooks/warmbly")
async def receive_warmbly_webhook(request: Request):
    """
    HTTP endpoint for Warmbly webhooks.

    Receives events, validates signature, and hands off to route_payload.
    """
    # Read raw body for signature validation
    body_bytes = await request.body()

    # Bounds post-buffering work only: uvicorn has already read the body, so a
    # hard wire-level limit belongs at traefik if that ever matters.
    limit = max_body_bytes()
    if len(body_bytes) > limit:
        print(f"REJECTED 413: body {len(body_bytes)}B exceeds {limit}B")
        raise HTTPException(status_code=413, detail="Payload too large")

    headers = dict(request.headers)
    signature_header = request.headers.get(SIGNATURE_HEADER)

    if DEBUG:
        print(f"\n{'='*60}")
        print("Inbound request (WARMBLY_WEBHOOK_DEBUG)")
        print(f"{'='*60}")
        print(json.dumps(describe_request(headers, body_bytes), indent=2))

    # Validate signature. A rejection is always explained: a bare 401 in the
    # log tells you nothing about which of the several causes it was.
    check = describe_signature_check(body_bytes, signature_header)
    if not check['matched']:
        print(f"\n{'='*60}")
        print("REJECTED 401: signature check failed")
        print(f"{'='*60}")
        print(json.dumps(check, indent=2))

        if signature_header:
            schemes = probe_signature_schemes(body_bytes, signature_header)
            if schemes:
                print(
                    "The secret is correct but the signed payload form "
                    f"differs. Matching scheme(s): {schemes}"
                )
            else:
                print(
                    "No signing scheme over this body reproduces the digest, "
                    "which points at WARMBLY_WEBHOOK_SECRET not matching the "
                    "secret configured in Warmbly."
                )

        if not DEBUG:
            print(
                "Set WARMBLY_WEBHOOK_DEBUG=1 to log full request headers, "
                "which shows the header names the remote actually sends."
            )

        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse JSON
    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Log receipt
    print(f"\n{'='*60}")
    print(f"Warmbly Webhook Received")
    print(f"{'='*60}")
    print(f"Event: {payload.get('event_type')}")
    print(f"Organization: {payload.get('organization_id')}")
    print(f"Timestamp: {payload.get('created_at')}")

    return await route_payload(payload)


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import sys

    # Check for Unix socket first, then fall back to host:port
    uds_path = os.environ.get('WEBHOOK_UDS')

    print(f"\n{'='*60}")
    print(f"Warmbly Webhook HTTP Server")
    print(f"{'='*60}")

    if uds_path:
        print(f"Listening on Unix socket: {uds_path}")
        print(f"Mount this socket in traefik and configure the path")
        print(f"{'='*60}\n")
        uvicorn_run(
            app,
            uds=uds_path,
            log_level="info",
        )
    else:
        port = int(os.environ.get('WEBHOOK_PORT', 8765))
        host = os.environ.get('WEBHOOK_HOST', '0.0.0.0')
        print(f"Listening on http://{host}:{port}")
        print(f"Endpoint: POST http://{host}:{port}/webhooks/warmbly")
        print(f"Health: GET http://{host}:{port}/health")
        print(f"\nConfigure Warmbly to POST to this URL.")
        print(f"{'='*60}\n")

        uvicorn_run(
            app,
            host=host,
            port=port,
            log_level="info",
        )
