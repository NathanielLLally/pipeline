"""
Warmbly Webhook Receiver

Inbound HTTP webhook endpoint that receives Warmbly events and triggers
the contact research and campaign drafting pipeline.

Warmbly sends webhook events when:
- Campaigns are created or modified
- Contacts are added or updated
- Custom events are emitted (e.g., from Prefect research pipeline)

This flow acts as the bridge: it receives Warmbly events, validates them,
and queues work in the research pipeline.

Usage:
    Deploy this flow to Prefect:
    $ python flow/warmbly_webhook_receiver.py

    Configure Warmbly webhook to POST to the trigger URL:
    https://api.prefect.cloud/hooks/{webhook_id}

    Or for self-hosted Prefect:
    https://127.0.0.1:4200/api/hooks/{webhook_id}
"""

import json
import hashlib
import hmac
import os
from typing import Any, Optional

from prefect import flow, task
from prefect.events import DeploymentEventTrigger


@task
def validate_webhook_signature(
    payload: dict[str, Any],
    signature: Optional[str]
) -> bool:
    """
    Validate Warmbly webhook signature (if configured).

    Warmbly signs webhooks with HMAC-SHA256.
    Header: X-Warmbly-Signature (format: sha256=<hex>)
    Secret: WARMBLY_WEBHOOK_SECRET env var
    """
    secret = os.environ.get('WARMBLY_WEBHOOK_SECRET')
    if not secret:
        # No secret configured, skip validation
        return True

    if not signature:
        return False

    # Reconstruct the signature
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode()
    expected_sig = hmac.new(
        secret.encode(),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_sig)


@task
def classify_event(payload: dict[str, Any]) -> str:
    """
    Classify the incoming Warmbly event by type.

    Returns the event class (e.g., 'campaign', 'contact', 'custom')
    for downstream routing.
    """
    event_type = payload.get('event_type', '')

    if event_type.startswith('campaign.'):
        return 'campaign'
    elif event_type.startswith('contact.'):
        return 'contact'
    elif event_type.startswith('email.'):
        return 'email'
    else:
        return 'custom'


@task
def route_event(event_class: str, payload: dict[str, Any]) -> dict:
    """
    Route the event to the appropriate downstream handler.

    For now, all events are logged and forwarded to the research pipeline.
    Future: could implement selective routing based on event type.
    """
    print(f"\n📥 Received Warmbly event: {payload.get('event_type')}")
    print(f"   Organization: {payload.get('organization_id')}")
    print(f"   Event class: {event_class}")
    print(f"   Timestamp: {payload.get('created_at')}")

    return {
        "event_type": payload.get('event_type'),
        "organization_id": payload.get('organization_id'),
        "event_data": payload.get('data', {}),
        "event_class": event_class,
        "received_at": payload.get('created_at'),
    }


@flow(log_prints=True)
def warmbly_webhook_receiver(
    payload: dict[str, Any],
    signature: Optional[str] = None
) -> dict:
    """
    Main webhook receiver flow.

    Receives events from Warmbly, validates them, classifies by type,
    and routes to appropriate handlers.

    Args:
        payload: The webhook payload from Warmbly
        signature: Optional X-Warmbly-Signature header value

    Returns:
        Event routing details and status
    """
    print("\n" + "="*60)
    print("Warmbly Webhook Receiver")
    print("="*60)

    # Validate signature
    if not validate_webhook_signature(payload, signature):
        print("❌ Invalid webhook signature")
        return {
            "status": "rejected",
            "reason": "invalid_signature",
        }

    # Classify the event
    event_class = classify_event(payload)

    # Route to handler
    result = route_event(event_class, payload)

    print("\n✅ Event accepted and routed")
    print("="*60)

    return {
        "status": "accepted",
        "event_class": event_class,
        "event_type": payload.get('event_type'),
        "routing_result": result,
    }


if __name__ == "__main__":
    # Deploy the webhook receiver with event-driven trigger
    deployment = warmbly_webhook_receiver.to_deployment(
        name="warmbly-webhook-receiver",
        triggers=[
            DeploymentEventTrigger(
                expect={"api.webhook.received"},
                parameters={
                    "payload": {
                        "__prefect_kind": "json",
                        "value": {
                            "__prefect_kind": "jinja",
                            "template": "{{ event.payload | tojson }}",
                        }
                    }
                },
            )
        ],
    )

    print("Deploying Warmbly webhook receiver...")
    print("  Deployment: warmbly-webhook-receiver")
    print("  Trigger: api.webhook.received events")
    print("\nOnce deployed, retrieve the webhook URL from Prefect Cloud or your local server.")
    print("Configure Warmbly to POST events to that URL.")

    deployment.serve()
