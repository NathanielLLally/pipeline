"""
Start the discord-bot deployment, clearing anything that would block it.

    $ set -a && . ./.env && set +a && python flow/start_discord_bot.py

Reaps first (see flow/discord_bot_reaper.py): stale runs are cancelled, their
local processes stopped when provably theirs, and a leaked slot reset. Then it
triggers one run and returns without waiting for it.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.discord_bot_reaper import reap

DEPLOYMENT = os.environ.get('DISCORD_BOT_DEPLOYMENT') or 'discord-bot/discord-bot'


async def start(client: Any, run_deployment: Callable[..., Awaitable[Any]]) -> str:
    report = await reap(client, DEPLOYMENT)
    print(f'reaped: {report.summary()}')
    flow_run = await run_deployment(
        name=DEPLOYMENT, timeout=0, as_subflow=False)
    return f'started {DEPLOYMENT}: {flow_run.name} ({flow_run.id})'


async def main() -> None:
    from prefect.client.orchestration import get_client
    from prefect.deployments import run_deployment

    async with get_client() as client:
        print(await start(client, run_deployment))


if __name__ == '__main__':
    asyncio.run(main())
