"""
Serve the AI node deployments.

One process serves both, so each node is independently runnable, pausable and
rate-limitable in the Prefect UI while costing one process rather than two.
Concurrency limits are the point of the split: a limit on research-agent
bounds total LLM pressure including its own escalation pass.

    $ python flow/serve_agents.py

Must run on the same host as whatever triggers these deployments:
PREFECT_API_URL reads 127.0.0.1:4200 on both the workstation and the prod host
while meaning different servers.
"""

import sys
from pathlib import Path

# Started directly as a script, which puts flow/ on sys.path rather than the
# repo root and breaks absolute `flow.*` imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.agents.drafting import drafting_agent
from flow.agents.research import research_agent

if __name__ == "__main__":
    serve(
        research_agent.to_deployment(name="research-agent"),
        drafting_agent.to_deployment(name="drafting-agent"),
    )
