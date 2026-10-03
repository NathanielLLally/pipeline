"""
Serve the pipeline deployments.

    $ set -a && . ./.env && set +a && python flow/serve_agents.py

Two deployments only:

  run_agents        the thing you schedule. Selects a batch (or takes explicit
                    business_ids) and calls research and drafting per business
                    as subflows, so every stage exchanges plain Python objects
                    inside one flow -- no result persistence, no serialization
                    boundary, no database handoff.

  candidate-selector  kept standalone because it is useful and free: it answers
                    "what would the next batch be" without spending a token.

research-agent and drafting-agent are deliberately NOT deployments any more.
They are subflows of run_agents, so they still get their own flow run, state
and retry in the UI, but they are not separately schedulable -- that was the
cost of letting them pass data directly. To research specific businesses, call
run_agents with business_ids=[...].

Must run on the same host as whatever triggers these: PREFECT_API_URL reads
127.0.0.1:4200 on both the workstation and the prod host while meaning
different servers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.agents.selector import candidate_selector
from flow.run_agents import run_agents

if __name__ == "__main__":
    serve(
        run_agents.to_deployment(name="run-agents"),
        candidate_selector.to_deployment(name="candidate-selector"),
    )
