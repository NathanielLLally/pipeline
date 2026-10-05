import asyncio                                                            
from warmbly import AsyncGatewayClient                                  
import sys
import os


API_KEY=os.environ.get("WARMBLY_API_KEY");
async def main() -> None:                                                 
    gateway = AsyncGatewayClient(token=API_KEY)  # needs the          realtime_subscribe scope                                                    

@gateway.on_event("CAMPAIGN_STARTED")                                 
async def handle(payload: dict) -> None:                              
    print("campaign started:", payload["campaign_id"])                

    await gateway.connect()                                               
    await gateway.subscribe("org:00000000-0000-0000-0000-000000000000")   
    await gateway.run_forever()                                           

asyncio.run(main())     
