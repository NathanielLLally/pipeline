from warmbly.gateway import AsyncGatewayClient, GatewayEvent
import os
import argparse
import sys

from warmbly import Warmbly

client = Warmbly(
    api_key=os.environ.get('WARMBLY_API_KEY'),
    base_url="https://crm.accurateleadinfo.com.com/v1",
    timeout=30.0,                       # seconds, or an httpx.Timeout
    max_retries=5,                      # default is 2
)

os.environ.get
gw = AsyncGatewayClient(token=os.environ.get('WARMBLY_API_KEY'));

@gw.on_event(GatewayEvent.CAMPAIGN_STARTED)
async def handle(topic, payload):
    print(payload["campaign_id"])

await gw.connect()
await gw.subscribe("org:org_123", intents=["CAMPAIGN"])
await gw.run_forever()
