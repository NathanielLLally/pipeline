"""
Serve the Discord bot as a deployment.

    $ set -a && . ./.env && set +a && python flow/serve_discord_bot.py
    $ prefect deployment run discord-bot/discord-bot

The flow runs until cancelled, so start one run at a time: a second run would
post every Warmbly event twice. Run it on the same host as serve_agents.py, so
/batchdraft and /list reach the same Prefect API.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.discord_bot import discord_bot

if __name__ == '__main__':
    serve(discord_bot.to_deployment(
        name='discord-bot',
        description='Warmbly event feed and slash commands in Discord',
        # A second concurrent run would double-post every event.
        concurrency_limit=1,
    ))
