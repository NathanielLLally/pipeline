"""
Serve the Discord bot as a deployment.

    $ set -a && . ./.env && set +a && python flow/serve_discord_bot.py

Start it from the Prefect UI, or `prefect deployment run discord-bot/discord-bot`.

There is deliberately no Prefect concurrency limit. Only one bot may be
connected (a second would post every Warmbly event twice), but a limit would
cancel or park a new run at the slot check, before it could clear a dead
predecessor. Instead each run stops every older run on start, so the newest
always wins (flow/discord_bot_reaper.py). Re-serving without a limit also
deletes any limit stored on the server.

Run it on the same host as serve_agents.py, so /batchdraft and /list reach the
same Prefect API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.discord_bot import discord_bot


def build_deployment():
    return discord_bot.to_deployment(
        name='discord-bot',
        description='Warmbly event feed and slash commands in Discord',
    )


if __name__ == '__main__':
    serve(build_deployment())
