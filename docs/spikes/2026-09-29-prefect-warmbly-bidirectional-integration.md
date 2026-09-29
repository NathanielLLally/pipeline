# Spike: Prefect ↔ Warmbly Bidirectional Integration Mechanisms

**Date:** 2026-09-29  
**Objective:** Evaluate four integration mechanisms for bidirectional Prefect ↔ Warmbly communication and recommend the best fit for a mxCheck wrapper test flow.

---

## Summary

| Mechanism | Direction | Latency | Reliability | Complexity | Recommendation |
|-----------|-----------|---------|-------------|-----------|---|
| **Phoenix WebSocket (Realtime Gateway)** | Bidirectional | Low (~real-time) | Medium (lossy on disconnect, has resume) | Medium | ✅ **RECOMMENDED** |
| **Webhooks (REST callbacks)** | Unidirectional (W→P) | Medium (~seconds) | High (retries, at-least-once) | Low | Complement to Phoenix |
| **Integrations subsystem** | Bidirectional | Medium | High | High | Too heavyweight for MVP |
| **Org-scoped HTTPS callbacks** | Unidirectional (W→P) | Medium | High | Low | Same as Webhooks (variant) |

**Recommendation:** Use **Phoenix WebSocket (Realtime Gateway)** as the primary bidirectional mechanism. Supplement with **Webhooks** for durable event delivery on Warmbly → Prefect events you cannot afford to lose (e.g., campaign state changes).

---

## Mechanism Deep-Dive

### 1. Phoenix WebSocket (Realtime Gateway)

**What it is:** Persistent WebSocket connection over `wss://realtime.warmbly.com/socket/websocket` using the Phoenix channel protocol (erlang/elixir websocket framework).

**Capabilities:**
- **Warmbly → Prefect:** Subscribe to live events (`CAMPAIGN_STARTED`, `EMAIL_SENT`, `REPLY_RECEIVED`, custom events, etc.)
- **Prefect → Warmbly:** Emit custom events via `push()` method (e.g., `research_complete`, `draft_ready`)
- **Resume semantics:** Automatic reconnection with `last_seq` tracking; can resume from last processed seq on reconnect (survives brief network hiccups)
- **Heartbeat:** Server sends heartbeats; client must reply or connection is culled after timeout

**Python SDK Support:**
```python
from warmbly.gateway import AsyncGatewayClient, GatewayEvent

gw = AsyncGatewayClient(token=WARMBLY_API_TOKEN)
await gw.connect()
await gw.subscribe("org:org_123", intents=["EMAIL", "CAMPAIGN"])

@gw.on_event(GatewayEvent.CAMPAIGN_STARTED)
async def on_campaign_started(topic, payload):
    print(f"Campaign started: {payload['campaign_id']}")

# Emit custom event back to Warmbly
await gw.push("org:org_123", "research_complete", {"business_id": 123})

await gw.run_forever()
```

**Pros:**
- True bidirectional communication
- Low latency (suitable for real-time dashboards)
- Explicit resume support (designed for exactly this use case)
- Already available in Warmbly Python SDK

**Cons:**
- Lossy on disconnect (must re-subscribe after reconnect)
- Heartbeat timeout means connection dies if Prefect doesn't ack
- Custom events must be explicitly defined/recognized by Warmbly
- Requires holding open connection (not ideal for serverless, but fine for Prefect)

**Best for:** Live feedback loops, Prefect → Warmbly state updates, watching for campaign/email events in real-time.

---

### 2. Webhooks (Org-scoped HTTPS Callbacks)

**What it is:** Stateless HTTP POST callbacks from Warmbly to a Prefect-owned webhook receiver endpoint.

**Capabilities:**
- **Warmbly → Prefect:** One-way push of events (campaign started, email sent, reply received, etc.)
- **Prefect → Warmbly:** None (one-way only; requires separate REST call to react)

**Python SDK Support:**
```python
# Create a webhook on Warmbly side
client.webhooks.create(
    url="https://my-prefect-receiver.example.com/webhooks/warmbly",
    event_types=["campaign.started", "email.sent", "email.replied"],
    enabled=True
)

# Prefect receives POST with payload:
# {
#   "id": "webhook_id",
#   "event_type": "campaign.started",
#   "organization_id": "org_123",
#   "created_at": "2026-09-29T...",
#   "data": { ... }
# }
```

**Reliability features:**
- Delivery guarantees via retries with exponential backoff
- Webhook delivery logs inspectable in Warmbly UI
- Signature verification via `verify_webhook_signature()`
- Idempotency: payload includes `id` for deduplication

**Pros:**
- Stateless and simple (no connection to manage)
- High reliability (guaranteed delivery, inspectable logs)
- No heartbeat/timeout concerns
- Good for data pipelines and serverless

**Cons:**
- One-way only (Warmbly → Prefect)
- Latency higher (~seconds before retry loop settles)
- Requires Prefect to expose a public endpoint (or webhook receiver)
- Not true bidirectional

**Best for:** Durable event recording, auditable event chains, non-time-critical reactions.

---

### 3. Integrations Subsystem

**What it is:** Warmbly's provider integration framework. Allows Prefect to register as a Warmbly "integration provider" with persistent connections and field mappings.

**Capabilities:**
- **Connections:** `create_connection()`, `test_connection()`, `update_connection_config()`
- **Event mapping:** `add_event()`, `list_events()`, `remove_event()`
- **Bidirectional sync:** `push()` to send data, events via gateway for receiving
- **Field mappings:** `set_field_mappings()` to define how Warmbly data maps to Prefect

**Python SDK Support:**
```python
# Create integration connection
conn = client.integrations.create_connection(
    provider="prefect",  # custom provider ID
    label="Prefect Orchestrator",
    config={"api_key": PREFECT_API_KEY, "base_url": PREFECT_API_URL}
)

# Add events this integration exposes
client.integrations.add_event(
    connection_id=conn.id,
    event_name="research_complete"
)

# Push data into Warmbly
client.integrations.push(
    connection_id=conn.id,
    data={...},
    provider_event="research_complete"
)
```

**Pros:**
- Purpose-built for external service integration
- Bidirectional (integrations framework owns both directions)
- Persistent connection management by Warmbly
- Can define custom events and field mappings

**Cons:**
- Complex API; requires registering Prefect as a provider
- Warmbly must approve/recognize `prefect` provider ID
- Overhead of connection state management
- Less documented than webhooks/gateway

**Best for:** Deep third-party integrations where Warmbly owns the lifecycle.

---

### 4. Org-Scoped HTTPS Callbacks

**What it is:** A variant of webhooks; essentially the same mechanism with org-level routing.

**Capabilities:** Same as webhooks (Warmbly → Prefect one-way).

**Status:** Covered under "Webhooks" above; no separate mechanism.

---

## Evaluation for MVP: Warmbly ↔ Prefect Bridge

### Test Goal
Create a Prefect flow that:
1. Wraps `mxCheck.pl` to validate emails
2. Demonstrates bidirectional communication with Warmbly
3. Can be tested against live Prefect and live Warmbly servers

### Scenario
```
Prefect flow (test)
  │
  ├─ Run mxCheck on test emails
  │
  ├─ Emit custom event to Warmbly (Phoenix): "test_validation_complete"
  │
  ├─ Listen for Warmbly events (Phoenix): watch for campaign state changes
  │
  └─ React to Warmbly state in Prefect task
```

### Recommendation: **Phoenix WebSocket + Webhooks (dual ingress)**

**Phase 1 (MVP): Phoenix WebSocket**
- Prefect holds open `AsyncGatewayClient` connection
- Emits test event to Warmbly after mxCheck completes
- Warmbly publishes event to Discord (proving it landed)
- Test passes if event appears in Warmbly UI + Discord

**Phase 2 (production): Add Webhooks**
- Register webhook for critical events (campaign state, email delivery)
- Webhook receiver in Prefect handles durable event recording
- Combines low-latency Phoenix (for real-time) + high-reliability Webhooks (for audit)

### Why Not Others?

**Integrations subsystem:** Too heavyweight for MVP. Requires Warmbly to recognize "prefect" as a provider. Save for later if needed.

**Webhooks alone:** Can't demonstrate true bidirectionality (missing Prefect → Warmbly push). Fine as a complement, not as the primary mechanism.

---

## Test Flow Design (MVP)

```python
# flow/warmbly_integration_test.py

import asyncio
from prefect import flow, task
from warmbly import Warmbly
from warmbly.gateway import AsyncGatewayClient
import subprocess
import json
import tempfile

@task
def validate_test_emails() -> list[dict]:
    """Run mxCheck on test emails"""
    test_emails = [
        "test@example.com",
        "info@happytailspawcare.com",
        "contact@trainwithtrust.com",
    ]
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('\n'.join(test_emails))
        temp_file = f.name
    
    result = subprocess.run(
        ['perl', 'scripts/mxCheck.pl', '--file', temp_file, '--threads', '5'],
        capture_output=True,
        text=True
    )
    
    verified = json.loads(result.stdout)
    return verified

@task
async def emit_warmbly_event(verified_emails: list[dict]):
    """Emit test event to Warmbly via Phoenix"""
    token = os.environ['WARMBLY_API_TOKEN']
    org_id = os.environ['WARMBLY_ORG_ID']  # needed for Phoenix
    
    gw = AsyncGatewayClient(token=token)
    await gw.connect()
    await gw.subscribe(f"org:{org_id}", intents=["CUSTOM"])
    
    # Push custom event
    await gw.push(
        f"org:{org_id}",
        "test_email_validation_complete",
        {
            "total": len(verified_emails),
            "verified": sum(1 for e in verified_emails if e['verified']),
            "timestamp": datetime.now().isoformat()
        }
    )
    
    await gw.close()

@flow
async def warmbly_integration_test():
    """Test Prefect ↔ Warmbly bidirectional communication"""
    print("🧪 Starting Warmbly integration test...")
    
    # Step 1: Validate emails
    verified = await validate_test_emails()
    print(f"✅ Validated {len(verified)} emails via mxCheck")
    
    # Step 2: Emit to Warmbly
    await emit_warmbly_event(verified)
    print(f"✅ Emitted test event to Warmbly")
    
    print("🎉 Integration test complete. Check Warmbly UI for event.")

if __name__ == "__main__":
    asyncio.run(warmbly_integration_test())
```

**Test verification:**
1. Run flow: `prefect flow run warmbly_integration_test`
2. Check Warmbly UI for custom event (should appear in audit log or custom events feed)
3. Check Discord for notification (if Warmbly has integration set up)
4. Inspect Prefect logs for task output

---

## Implementation Roadmap

**MVP (This Spike):**
- [ ] Test Phoenix WebSocket connection to live Warmbly
- [ ] Verify custom event emission works
- [ ] Create test flow with mxCheck + Phoenix emit
- [ ] Run against live servers; verify event lands in Warmbly UI

**Phase 1 (Production):**
- [ ] Build `flow/warmbly_integration.py` with `AsyncGatewayClient`
- [ ] Add graceful reconnection + heartbeat handling
- [ ] Write unit tests with mocked gateway
- [ ] Deploy to test Prefect

**Phase 2:**
- [ ] Add webhook receiver for durable events
- [ ] Integrate with full research/drafting pipeline
- [ ] Add Discord notifications

---

## References

- **Warmbly Realtime API:** https://github.com/warmbly/warmbly/blob/main/docs/content/docs/api/realtime.mdx
- **Warmbly Webhooks:** https://github.com/warmbly/warmbly/blob/main/docs/content/docs/api/reference/webhooks.mdx
- **Warmbly Integrations:** https://github.com/warmbly/warmbly/blob/main/docs/content/docs/api/reference/integrations.mdx
- **Python SDK:** `warmbly` 0.3.0 (installed, in `.venv/`)

---

## Next Steps

1. **Verify Phoenix connection:** Test `AsyncGatewayClient` against live Warmbly with real org token
2. **Test custom event:** Emit a test event, verify it appears in Warmbly
3. **Build mxCheck wrapper:** Create the test flow structure
4. **Run end-to-end:** Execute on live Prefect + live Warmbly, confirm visibility

Recommend starting with a quick 15-min spike to just connect and emit one event, then iterate.
