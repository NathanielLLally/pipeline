"""Notification workflow example."""
from pyworkflow import workflow, step, sleep


@step()
async def send_email(to: str, subject: str, body: str) -> dict:
    """Send an email notification."""
    # In a real app, integrate with email service (SendGrid, SES, etc.)
    return {"sent": True, "to": to, "subject": subject}


@step()
async def send_sms(phone: str, message: str) -> dict:
    """Send an SMS notification."""
    # In a real app, integrate with SMS service (Twilio, etc.)
    return {"sent": True, "phone": phone}


@workflow()
async def send_notification(
    user_id: str,
    message: str,
    channels: list[str] | None = None,
) -> dict:
    """
    Send notifications through multiple channels.

    This workflow demonstrates:
    - Conditional step execution
    - Sleep/delay functionality
    - Multiple notification channels

    Run with:
        pyworkflow workflows run send_notification --input '{"user_id": "user-123", "message": "Hello!"}'
    """
    if channels is None:
        channels = ["email"]

    results = {"user_id": user_id, "notifications": []}

    if "email" in channels:
        email_result = await send_email(
            to=f"{user_id}@example.com",
            subject="Notification",
            body=message,
        )
        results["notifications"].append({"channel": "email", **email_result})

    if "sms" in channels:
        # Add small delay between channels
        await sleep("1s")
        sms_result = await send_sms(
            phone="+1234567890",
            message=message,
        )
        results["notifications"].append({"channel": "sms", **sms_result})

    return results
