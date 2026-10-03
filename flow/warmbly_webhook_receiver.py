"""
Warmbly webhook receiver flow.

Consumes the Prefect event emitted by flow/warmbly_http_endpoint.py and
appears in the Prefect UI as a managed deployment. Follows the deployment
shape demonstrated in flow/wh.py.

Signature validation is deliberately NOT here: it requires the raw request
bytes, which only the HTTP listener holds. Re-serialising parsed JSON to
re-check an HMAC cannot work, and an earlier version of this file tried.

Serve it (long-lived; must run on the same host as the listener, so the
emitted event and this process meet on the same Prefect server):

    $ python flow/warmbly_webhook_receiver.py
"""

from typing import Any

from prefect import flow, serve, task
from prefect.events import DeploymentEventTrigger

from flow.warmbly_events import EVENT_NAME


# Best-effort duplicate suppression within one process. This does NOT survive
# a restart and is not shared between workers; a durable guard needs a unique
# index on the Warmbly event id. Tracked as a GitHub issue rather than solved
# here, because the schema has no table for it yet.
seen_event_ids: set = set()

KNOWN_EVENT_PREFIXES = ('contact', 'campaign', 'email', 'webhook')


@task
def classify_event(payload: dict[str, Any]) -> str:
    """Classify an event by its dotted prefix for downstream routing."""
    event_type = payload.get('event_type') or ''
    prefix = event_type.split('.', 1)[0]

    if prefix in KNOWN_EVENT_PREFIXES:
        return prefix
    return 'other'


@flow(log_prints=True)
def warmbly_webhook_receiver(payload: dict[str, Any]) -> dict:
    """Process one Warmbly webhook event."""
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

    event_class = classify_event(payload)

    print(f"Event: {payload.get('event_type')} (class: {event_class})")
    print(f"Organization: {payload.get('organization_id')}")
    print(f"Event id: {event_id}")

    return {
        "status": "processed",
        "event_type": payload.get('event_type'),
        "organization_id": payload.get('organization_id'),
        "event_id": event_id,
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
