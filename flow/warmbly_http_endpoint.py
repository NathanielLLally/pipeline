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
import asyncio
from typing import Any, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from uvicorn import run as uvicorn_run
import httpx

from prefect import flow, task


app = FastAPI(title="Warmbly Webhook Receiver")


# Webhook wire protocol. Overridable so the plumbing is not baked into the code
# if Warmbly renames a header or moves the challenge field.
SIGNATURE_HEADER = os.environ.get(
    'WARMBLY_SIGNATURE_HEADER', 'X-Warmbly-Signature'
)
SIGNATURE_ALGORITHM = os.environ.get('WARMBLY_SIGNATURE_ALGORITHM', 'sha256')
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


def describe_signature_check(
    payload_bytes: bytes,
    signature_header: Optional[str],
) -> dict[str, Any]:
    """
    Explain why a signature check passed or failed.

    Returns a log-safe report: the secret never appears in it, and digests are
    truncated to SIGNATURE_PREVIEW_CHARS so a log cannot be used to replay a
    request.
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')

    report: dict[str, Any] = {
        'secret_configured': bool(secret),
        'header_name_expected': SIGNATURE_HEADER,
        'header_present': signature_header is not None,
        'algorithm_expected': SIGNATURE_ALGORITHM,
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

    parts = signature_header.split('=', 1)
    report['header_format_ok'] = len(parts) == 2
    report['algorithm_received'] = parts[0] if len(parts) == 2 else None

    if len(parts) != 2:
        report['matched'] = False
        report['reason'] = (
            'header is not "<algorithm>=<digest>"; got a bare value of '
            f'length {len(signature_header)}'
        )
        return report

    if parts[0] != SIGNATURE_ALGORITHM:
        report['matched'] = False
        report['reason'] = (
            f'algorithm is {parts[0]!r}, expected {SIGNATURE_ALGORITHM!r}'
        )
        return report

    received = parts[1]
    expected = hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()

    report['signature_received'] = _preview(received, SIGNATURE_PREVIEW_CHARS)
    report['signature_expected'] = _preview(expected, SIGNATURE_PREVIEW_CHARS)
    report['signature_received_length'] = len(received)
    report['signature_expected_length'] = len(expected)
    report['matched'] = hmac.compare_digest(received, expected)
    report['reason'] = (
        'signature matched'
        if report['matched']
        else 'digest mismatch: wrong secret, or the body was signed differently'
    )

    return report


def probe_signature_schemes(
    payload_bytes: bytes,
    headers: dict[str, str],
    received_digest: str,
) -> list[str]:
    """
    Find which signing scheme, if any, produces the digest we were sent.

    A digest mismatch has two very different causes: the secret is wrong, or
    the secret is right but the remote signs something other than the raw body.
    This distinguishes them. An empty list means no scheme matched, which
    points at the secret rather than the scheme.
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if not secret or not received_digest:
        return []

    timestamp = None
    for name, value in headers.items():
        if name.lower() == TIMESTAMP_HEADER.lower():
            timestamp = value
            break

    candidates: dict[str, bytes] = {'body': payload_bytes}
    if timestamp:
        candidates['timestamp.body'] = f'{timestamp}.'.encode() + payload_bytes
        candidates['timestamp.body'] = timestamp.encode() + payload_bytes

    matches = []
    for label, signed_bytes in candidates.items():
        digest = hmac.new(secret.encode(), signed_bytes, hashlib.sha256)
        hex_digest = digest.hexdigest()
        b64_digest = base64.b64encode(digest.digest()).decode()

        if hmac.compare_digest(received_digest, hex_digest):
            matches.append(f'hex({label})')
        if hmac.compare_digest(received_digest, b64_digest):
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


async def trigger_research_flow(event_data: dict[str, Any]) -> dict:
    """
    Call Prefect API to trigger the research flow with event data.

    This would eventually call the main research pipeline flow
    with the Warmbly event as input.
    """
    prefect_api_url = os.environ.get(
        'PREFECT_API_URL',
        'http://127.0.0.1:4200/api'
    )

    # For now, just log the event
    # In production, this would:
    # 1. Look up the contact/campaign in Warmbly
    # 2. Extract relevant fields (email, company, etc.)
    # 3. Trigger flow: research_and_draft with the extracted data

    print(f"\n📥 Would trigger research flow with: {event_data}")

    return {
        "status": "queued",
        "flow": "research_and_draft",
        "event_type": event_data.get("event_type"),
    }


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

    result = await trigger_research_flow(payload)

    return {
        "status": "accepted",
        "event_type": payload.get('event_type'),
        "routing": result,
    }


@app.post("/webhooks/warmbly")
async def receive_warmbly_webhook(request: Request):
    """
    HTTP endpoint for Warmbly webhooks.

    Receives events, validates signature, and hands off to route_payload.
    """
    # Read raw body for signature validation
    body_bytes = await request.body()
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
            digest = signature_header.split('=', 1)[-1]
            schemes = probe_signature_schemes(body_bytes, headers, digest)
            if schemes:
                print(
                    "The secret is correct but the body is signed "
                    f"differently. Matching scheme(s): {schemes}"
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
