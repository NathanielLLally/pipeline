"""
Serve the Discord bot as a deployment.

    $ set -a && . ./.env && set +a && python flow/serve_discord_bot.py

Start a run with the launcher, not `prefect deployment run`: it clears any
stale run holding the slot first, so the start never waits.

    $ set -a && . ./.env && set +a && python flow/start_discord_bot.py

One run at a time (a second would post every Warmbly event twice), with
CANCEL_NEW: a start that cannot get the slot is cancelled at once instead of
waiting in AwaitingConcurrencySlot. Run it on the same host as serve_agents.py,
so /batchdraft and /list reach the same Prefect API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve
from prefect.client.schemas.objects import (
    ConcurrencyLimitConfig,
    ConcurrencyLimitStrategy,
)

from flow.discord_bot import discord_bot


def build_deployment():
    return discord_bot.to_deployment(
        name='discord-bot',
        description='Warmbly event feed and slash commands in Discord',
        concurrency_limit=ConcurrencyLimitConfig(
            limit=1,
            collision_strategy=ConcurrencyLimitStrategy.CANCEL_NEW,
        ),
    )


if __name__ == '__main__':
    serve(build_deployment())
