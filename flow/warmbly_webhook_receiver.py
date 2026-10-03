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

import os
from collections import OrderedDict
from typing import Any

import sys
from pathlib import Path

# These modules are started directly as scripts on the prod host
# (`python flow/warmbly_http_endpoint.py`), which puts `flow/` on sys.path
# rather than the repo root, breaking absolute `flow.*` imports. Adding the
# repo root keeps both `python flow/x.py` and `python -m flow.x` working.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow, serve
from prefect.events import DeploymentEventTrigger

from flow.warmbly_events import EVENT_NAME


# Best-effort duplicate suppression within one process. This does NOT survive
# a restart and is not shared between workers; a durable guard needs a unique
# index on the Warmbly event id. Tracked in issue #2, because the schema has no
# table for it yet.
#
# Bounded, because serve() runs for days: one retained id per event would be an
# unbounded leak, and an id is only useful for as long as Warmbly might retry.
# Oldest-first eviction, so the ids most likely to be retried are the ones kept.
MAX_SEEN_EVENT_IDS = int(
    os.environ.get('WARMBLY_MAX_SEEN_EVENT_IDS', '10000')
)
seen_event_ids: OrderedDict = OrderedDict()


def remember_event_id(event_id: str) -> None:
    """Record an event id, evicting the oldest once the cap is reached."""
    seen_event_ids[event_id] = None
    while len(seen_event_ids) > MAX_SEEN_EVENT_IDS:
        seen_event_ids.popitem(last=False)

KNOWN_EVENT_PREFIXES = ('contact', 'campaign', 'email', 'webhook')


def classify_event(payload: dict[str, Any]) -> str:
    """
    Classify an event by its dotted prefix for downstream routing.

    Deliberately not a Prefect task: it is a prefix lookup with nothing
    to retry, cache or rate-limit, so a task run would buy a server
    round-trip for a string split. The flow is the unit Prefect manages.
    """
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
        remember_event_id(event_id)

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
