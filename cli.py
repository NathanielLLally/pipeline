import asyncio
from warmbly import AsyncGatewayClient

async def main() -> None:
    gateway = AsyncGatewayClient(token="wmbly_ovkgABVQgYWxpVfI9Dj88B06J8hDAnzE8c9bu1AtZsE", base_url="wss://ws.api.crm.happytailspawcare.com")  # needs the realtime_subscribe scope

    @gateway.on_event("CAMPAIGN_STARTED")
    async def handle(payload: dict) -> None:
        print("campaign started:", payload["campaign_id"])

    await gateway.connect()
    await gateway.subscribe("org:00e33e77-6d57-4e2f-a0cd-e0bc66afd774")
    print("subscribed\n")
    await gateway.run_forever()


asyncio.run(main())
