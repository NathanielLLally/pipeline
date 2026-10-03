"""
The Warmbly webhook event name.

Lives alone, with no heavy imports, because both the HTTP listener (which
emits the event) and the receiver deployment (whose trigger matches it) import
it, and the receiver runs inside a Prefect worker where constructing the
FastAPI app would be pointless work.

The name is the entire routing key: an automation created by
to_deployment(triggers=[...]) matches on name alone, so a name reused across
consumers fans out to all of them.
"""

import os

EVENT_NAME = os.environ.get(
    'WARMBLY_WEBHOOK_EVENT_NAME', 'warmbly.webhook.received'
)
