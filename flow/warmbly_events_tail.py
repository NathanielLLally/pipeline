"""
Bare-bones Warmbly gateway listener: connects, subscribes to org:{WARMBLY_ORG_ID},
and prints every event it receives, unfiltered. For diagnosing whether the
server actually broadcasts a given event family (e.g. CONTACT_*) independent
of the Discord bot, the CLI, or any client-side filtering.

Usage:
    python flow/warmbly_events_tail.py
    WARMBLY_EVENT_INTENTS=CONTACT python flow/warmbly_events_tail.py  # narrow server-side

Env required: WARMBLY_API_TOKEN, WARMBLY_WEBSOCKET_URL, WARMBLY_ORG_ID
Ctrl-C to stop.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from warmbly.gateway import AsyncGatewayClient, GatewayEvent


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f'{name} is not set')
    return value


async def main() -> None:
    token = _env('WARMBLY_API_TOKEN')
    url = _env('WARMBLY_WEBSOCKET_URL')
    org_id = _env('WARMBLY_ORG_ID')
    intents_raw = os.environ.get('WARMBLY_EVENT_INTENTS', '').strip()
    intents = [t.strip().upper() for t in intents_raw.split(',') if t.strip()] or None

    gw = AsyncGatewayClient(token=token, base_url=url)

    # Every UPPER_CASE business event, no exclusions -- mirrors what the
    # Discord bot registers, minus Discord. presence_* / rate_limited /
    # resumed / resume_failed are lowercase and deliberately skipped.
    names = sorted({v for k, v in vars(GatewayEvent).items()
                    if k.isupper() and isinstance(v, str) and v.isupper()})

    async def handler(topic: str, payload, _name: str) -> None:
        print(f'\n=== {_name} on {topic} ===')
        print(json.dumps(payload, indent=2, default=str))

    for name in names:
        gw.on_event(name)(lambda topic, payload, _name=name: handler(topic, payload, _name))

    print(f'connecting to {url} ...')
    await gw.connect()
    print(f'subscribing to org:{org_id} (intents={intents or "all"}) ...')
    reply = await gw.subscribe(f'org:{org_id}', intents=intents)
    print(f'joined: {reply}')
    print('listening -- Ctrl-C to stop\n')

    try:
        await gw.run_forever()
    finally:
        await gw.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
