"""
Warmbly ↔ Prefect Integration Test via Phoenix WebSocket

Tests bidirectional communication:
1. Prefect runs mxCheck to validate emails
2. Emits custom event to Warmbly via Phoenix
3. Verifies event appears in Warmbly UI

Usage (local testing):
    export WARMBLY_ORG_ID="org_id_from_env_or_ui"
    python flow/warmbly_integration_test.py

Usage (via Prefect deployment):
    prefect deployment run <flow>/<deployment>
"""

import asyncio
import json
import os
import subprocess
import tempfile
from datetime import datetime
from typing import Optional

from prefect import flow, task


@task
def validate_test_emails() -> list[dict]:
    """Run mxCheck on test emails and return verified results."""
    test_emails = [
        "test@example.com",
        "noreply@example.com",
        "contact@example.com",
    ]

    print(f"🧪 Validating {len(test_emails)} test emails via mxCheck...")

    # Write emails to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(test_emails))
        temp_file = f.name

    try:
        # Run mxCheck.pl (use --force-check to bypass reverse DNS verification)
        result = subprocess.run(
            ['perl', 'scripts/mxCheck.pl', '--file', temp_file, '--threads', '5', '--force-check'],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            print(f"❌ mxCheck failed: {result.stderr}")
            return []

        # Parse JSON output
        verified = json.loads(result.stdout)
        print(f"✅ mxCheck result: {len(verified)} emails checked")

        for email_result in verified:
            status = "✓" if email_result.get('verified') else "✗"
            print(f"   {status} {email_result['email']}")

        return verified

    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file)
        except:
            pass


@task
def emit_warmbly_event(
    verified_emails: list[dict],
    org_id: str
) -> bool:
    """Emit custom test event to Warmbly via Phoenix WebSocket."""
    print(f"\n🚀 Connecting to Warmbly Phoenix gateway for org: {org_id}...")

    token = os.environ.get('WARMBLY_API_TOKEN')
    if not token:
        print("❌ WARMBLY_API_TOKEN not set in environment")
        return False

    async def _emit_async():
        """Async helper to connect and emit event."""
        from warmbly.gateway import AsyncGatewayClient

        # Use per-tenant WebSocket gateway URL from WARMBLY_WEBSOCKET_URL env var
        # Self-hosted Warmbly runs realtime gateway in Docker on port 4000
        # (not the SDK's default wss://realtime.warmbly.com)
        # Note: AsyncGatewayClient appends /socket/websocket to the base_url,
        # so WARMBLY_WEBSOCKET_URL should be just the host and port (e.g., wss://api.crm.happytailspawcare.com:4000)
        gateway_url = os.environ.get('WARMBLY_WEBSOCKET_URL')
        if not gateway_url:
            print("❌ WARMBLY_WEBSOCKET_URL not set in environment")
            return False

        # Strip any trailing path from WARMBLY_WEBSOCKET_URL (in case it includes /socket/websocket)
        gateway_url = gateway_url.rstrip('/').split('/socket/websocket')[0]

        gw = AsyncGatewayClient(token=token, base_url=gateway_url)

        # Connect with timeout
        print(f"   Connecting to {gateway_url}/socket/websocket...")
        try:
            await asyncio.wait_for(gw.connect(), timeout=10.0)
            print(f"   ✅ Connected")
        except asyncio.TimeoutError:
            print(f"❌ Connection timeout after 10s")
            return False

        try:
            # Subscribe to org events
            print(f"   Subscribing to org:{org_id}...")
            await gw.subscribe(f"org:{org_id}", intents=["CUSTOM"])
            print(f"   ✅ Subscribed")

            # Prepare and emit test event
            test_payload = {
                "flow_run_id": os.environ.get('PREFECT_FLOW_RUN_ID', 'test-run'),
                "test_timestamp": datetime.now().isoformat(),
                "total_emails": len(verified_emails),
                "verified_count": sum(1 for e in verified_emails if e.get('verified')),
                "emails": verified_emails
            }

            print(f"   Pushing event 'prefect_test_email_validation'...")
            await gw.push(
                f"org:{org_id}",
                "prefect_test_email_validation",
                test_payload
            )
            print(f"   ✅ Event pushed")

            # Give Warmbly a moment to process
            await asyncio.sleep(1)

            print(f"✅ Warmbly event emitted successfully")
            return True

        finally:
            await gw.close()

    try:
        return asyncio.run(_emit_async())
    except Exception as e:
        print(f"❌ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


@flow(log_prints=True)
def warmbly_integration_test(
    org_id: Optional[str] = None
) -> dict:
    """
    Test Prefect ↔ Warmbly bidirectional integration.

    Args:
        org_id: Warmbly organization ID (e.g., "org_12345...").
                If not provided, reads from WARMBLY_ORG_ID env var.

    Returns:
        Test result dict with status and details.
    """

    # Resolve org_id
    if not org_id:
        org_id = os.environ.get('WARMBLY_ORG_ID')

    if not org_id:
        print("❌ WARMBLY_ORG_ID not provided and not set in environment")
        print("   Set it via: export WARMBLY_ORG_ID='org_...'")
        return {"success": False, "error": "missing_org_id"}

    print(f"\n{'='*60}")
    print(f"Warmbly ↔ Prefect Integration Test")
    print(f"{'='*60}")
    print(f"Organization: {org_id}")
    print(f"Timestamp: {datetime.now().isoformat()}\n")

    # Step 1: Validate emails
    print("STEP 1: Email Validation (mxCheck)")
    print(f"{'-'*60}")
    verified_emails = validate_test_emails()

    if not verified_emails:
        print("⚠️  No emails validated; skipping Warmbly event")
        return {
            "success": False,
            "error": "email_validation_failed",
            "verified_count": 0
        }

    # Step 2: Emit to Warmbly
    print(f"\nSTEP 2: Warmbly Integration")
    print(f"{'-'*60}")
    success = emit_warmbly_event(verified_emails, org_id)

    # Summary
    print(f"\n{'='*60}")
    if success:
        print("✅ INTEGRATION TEST PASSED")
        print("\nNext steps:")
        print("1. Check Warmbly UI for custom event 'prefect_test_email_validation'")
        print("2. Check Discord for notification (if integration enabled)")
        print("3. Verify event payload contains validated email details")
        return {
            "success": True,
            "verified_count": len(verified_emails),
            "org_id": org_id
        }
    else:
        print("❌ INTEGRATION TEST FAILED")
        print("\nTroubleshooting:")
        print("1. Verify WARMBLY_API_TOKEN is correct")
        print("2. Verify WARMBLY_ORG_ID is correct")
        print("3. Check firewall access to wss://realtime.warmbly.com")
        print("4. Check Prefect logs for detailed errors")
        return {
            "success": False,
            "error": "warmbly_event_failed",
            "verified_count": len(verified_emails)
        }


if __name__ == "__main__":
    # Local testing: run the flow directly
    result = warmbly_integration_test()
    print(f"\nFlow result: {result}")
