from typing import Any

from prefect import flow, serve
from prefect.events import DeploymentEventTrigger


@flow(log_prints=True)
def process_webhook(payload: dict[str, Any]):
    """Process data from a custom event"""
    print(f"User: {payload.get('user_id')}")
    print(f"Action: {payload.get('action')}")
    print(f"Full payload: {payload}")


if __name__ == "__main__":
    deployment = process_webhook.to_deployment(
        name="webhook-processor",
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

    serve(deployment)
