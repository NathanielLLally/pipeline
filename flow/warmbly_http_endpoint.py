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

from fastapi import FastAPI, Request, HTTPException, Header
from uvicorn import run as uvicorn_run
import httpx

from prefect import flow, task


app = FastAPI(title="Warmbly Webhook Receiver")


def validate_webhook_signature(
    payload_bytes: bytes,
    signature_header: Optional[str]
) -> bool:
    """
    Validate Warmbly webhook signature.

    Header format: X-Warmbly-Signature: sha256=<hex>
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if not secret:
        return True

    if not signature_header:
        return False

    # Parse signature header: "sha256=abc123def456..."
    parts = signature_header.split('=', 1)
    if len(parts) != 2 or parts[0] != 'sha256':
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


@app.post("/webhooks/warmbly")
async def receive_warmbly_webhook(
    request: Request,
    x_warmbly_signature: Optional[str] = Header(None),
) -> dict:
    """
    HTTP endpoint for Warmbly webhooks.

    Receives events, validates signature, and routes to the research pipeline.
    """
    # Read raw body for signature validation
    body_bytes = await request.body()

    # Validate signature
    if not validate_webhook_signature(body_bytes, x_warmbly_signature):
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

    # Route to research flow
    result = await trigger_research_flow(payload)

    return {
        "status": "accepted",
        "event_type": payload.get('event_type'),
        "routing": result,
    }


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import sys

    port = int(os.environ.get('WEBHOOK_PORT', 8765))
    host = os.environ.get('WEBHOOK_HOST', '0.0.0.0')

    print(f"\n{'='*60}")
    print(f"Warmbly Webhook HTTP Server")
    print(f"{'='*60}")
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
