"""
Leads orchestration flows.

Module initialization ensures Prefect refuses to silently spin up a throwaway
server if PREFECT_API_URL is unset.
"""

import os

# Fail loudly if PREFECT_API_URL is missing, instead of silently creating an
# ephemeral server that breaks on variable calls. The .env file carries the
# real address; anything importing from flow/ must have it sourced.
os.environ.setdefault('PREFECT_SERVER_ALLOW_EPHEMERAL_MODE', 'false')
