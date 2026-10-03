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


def validate_webhook_signature(
    payload_bytes: bytes,
    signature_header: Optional[str]
) -> bool:
    """
    Validate Warmbly webhook signature.

    Header format: <SIGNATURE_HEADER>: <SIGNATURE_ALGORITHM>=<hex>
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if not secret:
        return True

    if not signature_header:
        return False

    # Parse signature header: "<algorithm>=abc123def456..."
    parts = signature_header.split('=', 1)
    if len(parts) != 2 or parts[0] != SIGNATURE_ALGORITHM:
        return False

    signature = parts[1]
    expected_sig = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_sig)


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

    # Validate signature
    if not validate_webhook_signature(
        body_bytes, request.headers.get(SIGNATURE_HEADER)
    ):
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
