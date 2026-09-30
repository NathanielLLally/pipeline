# Warmbly Webhook Setup Guide

This guide walks you through setting up webhooks to integrate Warmbly with the Prefect contact research and campaign drafting pipeline.

## Two Deployment Options

### Option 1: Prefect Cloud Managed Webhooks (Recommended)

Use Prefect Cloud's built-in webhook service. Webhooks are automatically provisioned, secured, and managed by Prefect.

**Pros:**
- No infrastructure to manage
- Automatic HTTPS and signature verification
- URL is static and managed by Prefect
- Scaling handled automatically

**Cons:**
- Requires Prefect Cloud account
- Webhook URL depends on Prefect Cloud availability

**Setup:**

1. Deploy the webhook receiver flow:
   ```bash
   python flow/warmbly_webhook_receiver.py
   ```

2. Once deployed, retrieve the webhook URL from Prefect Cloud:
   ```bash
   prefect deployment ls
   # Find "warmbly-webhook-receiver" and note its trigger URL
   ```
   Or from the Prefect Cloud UI:
   - Go to Deployments → warmbly-webhook-receiver
   - Click "Triggers" tab
   - Copy the webhook URL

3. In Warmbly settings, create a webhook:
   - Settings → Integrations → Webhooks
   - Click "+ New Webhook"
   - **URL:** Paste the Prefect webhook URL
   - **Events:** Select which events to subscribe to:
     - `campaign.created`
     - `campaign.started`
     - `campaign.paused`
     - `contact.created`
     - `contact.updated`
     - `email.sent`
     - `email.replied`
   - **Authentication:** None (Prefect handles this)
   - **Test:** Click "Send Test Event"
   - **Save**

4. Verify in Prefect:
   - Check Prefect Cloud for incoming webhook events
   - Look for `api.webhook.received` events
   - Verify flow runs are being triggered

---

### Option 2: Direct HTTP Endpoint (Self-Hosted)

Run an HTTP server that listens for Warmbly webhooks directly. Use this when you want full control or are running Prefect locally.

**Pros:**
- No external dependencies
- Full control over the endpoint
- Can run on your own infrastructure
- Webhook URL is under your control

**Cons:**
- Need to expose and manage the HTTP server
- Requires HTTPS certificates (for production)
- Need to handle signature verification yourself
- Port forwarding/firewall configuration needed

**Setup:**

1. Install dependencies:
   ```bash
   pip install fastapi uvicorn httpx
   ```

2. Start the HTTP server:
   ```bash
   # For local testing
   python flow/warmbly_http_endpoint.py
   
   # For production, use environment variables
   WEBHOOK_HOST=0.0.0.0 WEBHOOK_PORT=8765 python flow/warmbly_http_endpoint.py
   ```

3. Expose the endpoint to Warmbly:
   ```bash
   # Option A: SSH tunnel (for local development)
   ssh -R 8765:localhost:8765 user@happytailspawcare.com
   
   # Option B: Configure firewall and port forwarding
   sudo firewall-cmd --add-port=8765/tcp --permanent
   sudo firewall-cmd --reload
   
   # Then Warmbly can reach:
   https://crm.happytailspawcare.com:8765/webhooks/warmbly
   ```

4. In Warmbly settings, create a webhook:
   - Settings → Integrations → Webhooks
   - Click "+ New Webhook"
   - **URL:** `https://crm.happytailspawcare.com:8765/webhooks/warmbly`
   - **Events:** Select events (see above)
   - **Authentication:** None (we handle it in the code)
   - **Test:** Click "Send Test Event"
   - **Save**

5. Verify:
   - Check server logs: `Server received event: ...`
   - Check Prefect: Flow runs should be triggered

---

## Webhook Signature Verification

Both implementations support optional HMAC-SHA256 signature verification.

**To enable:**

1. In Warmbly, generate a webhook secret:
   - Settings → API Keys → Webhook Secret
   - Copy the secret

2. Set the environment variable:
   ```bash
   export WARMBLY_WEBHOOK_SECRET="whs_your_secret_here"
   ```

3. Restart the flow/server. Signatures will now be validated.

**How it works:**
- Warmbly signs each webhook with header: `X-Warmbly-Signature: sha256=<hex>`
- We reconstruct the signature and compare (constant-time, timing-safe)
- Mismatched signatures are rejected with HTTP 401

---

## Event Types Supported

Warmbly sends these event types to the webhook:

### Campaign Events
- `campaign.created` — New campaign created
- `campaign.started` — Campaign started sending
- `campaign.paused` — Campaign paused
- `campaign.resumed` — Campaign resumed
- `campaign.ended` — Campaign completed
- `campaign.scheduled` — Campaign scheduled

### Contact Events
- `contact.created` — New contact added
- `contact.updated` — Contact information updated
- `contact.deleted` — Contact removed

### Email Events
- `email.sent` — Email delivered to contact
- `email.opened` — Contact opened email
- `email.clicked` — Contact clicked link in email
- `email.replied` — Contact replied to email
- `email.bounced` — Email bounced
- `email.marked_spam` — Marked as spam

### Custom Events
- Custom events emitted by external systems (e.g., Prefect)
- Topic: `prefect_*` (our convention)

---

## Webhook Payload Format

All webhook payloads follow this structure:

```json
{
  "id": "webhook_123abc",
  "event_type": "campaign.started",
  "organization_id": "00e33e77-6d57-4e2f-a0cd-e0bc66afd774",
  "created_at": "2026-09-29T19:00:00Z",
  "data": {
    "campaign_id": "cam_abc123",
    "campaign_name": "Q4 Outreach",
    "contact_count": 250,
    ...
  }
}
```

---

## Troubleshooting

### Webhook not triggering

1. **Check event selection:** Verify the events you're interested in are selected in Warmbly
2. **Test manually:** In Warmbly, click "Send Test Event" to verify the endpoint receives data
3. **Check logs:**
   - Prefect Cloud: Look at deployment runs
   - HTTP server: Check console output
4. **Verify URL:** Copy-paste the URL from Warmbly settings and test with curl:
   ```bash
   curl -X POST https://your-webhook-url \
     -H "Content-Type: application/json" \
     -d '{"event_type": "test", "data": {}}'
   ```

### Signature validation failing

1. **Verify secret is set:** `echo $WARMBLY_WEBHOOK_SECRET`
2. **Check header is being sent:** Add logging to see `X-Warmbly-Signature` header
3. **Disable temporarily for testing:** Comment out signature check to isolate the issue

### Flow not running

1. **Check flow is deployed:** `prefect deployment ls`
2. **Verify trigger is configured:** `prefect trigger ls`
3. **Check flow logs:** Review the flow run details in Prefect Cloud or local server
4. **Test flow manually:** Run it directly to ensure no syntax errors

---

## Security Considerations

1. **Use HTTPS in production:** Webhooks carry sensitive data; use TLS certificates
2. **Enable signature verification:** Set `WARMBLY_WEBHOOK_SECRET` to verify requests come from Warmbly
3. **Rotate secrets regularly:** Change webhook secrets every 90 days
4. **Rate limit the endpoint:** Add rate limiting to prevent abuse (not yet implemented)
5. **Restrict by IP:** If possible, whitelist Warmbly's webhook sender IPs (contact Warmbly support for this)

---

## Next Steps

Once webhooks are receiving events:

1. Update the research flow to accept Warmbly webhook payloads
2. Extract contact/campaign details from the webhook data
3. Trigger the research pipeline with that data
4. Create campaigns in Warmbly from research results
5. Monitor webhook delivery in Warmbly's logs

See `docs/superpowers/specs/2026-09-28-prefect-warmbly-orchestration-design.md` for the full research and drafting pipeline architecture.
