"""
Serve the pipeline deployments.

    $ set -a && . ./.env && set +a && python flow/serve_agents.py

Deployments:

  run-agents           (optional, controlled by SERVE_RUN_AGENTS env var)
                       orchestrates research + drafting across a batch.
                       Why not separate: Prefect keeps in-memory Python
                       objects (dicts, results) available only within the
                       same flow. Across flow runs, `run_deployment` hands
                       back a FlowRun, not data. Putting stages inside one
                       flow removes that boundary — candidates, research
                       output, drafts pass as ordinary dicts with no
                       serialization or result-storage overhead. Each
                       business is still a subflow (own run, own state,
                       individual retry in UI).

  research-agent      research one business, with escalation on low
                      confidence. Can be called standalone or as subflow.

  drafting-agent      draft one outreach email for a researched business.
                      Can be called standalone or as subflow.

  candidate-selector  batch query: what would the next batch be, without
                      spending a token.

Must run on the same host as whatever triggers these: PREFECT_API_URL reads
127.0.0.1:4200 on both the workstation and the prod host while meaning
different servers.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.agents.research import research_agent
from flow.agents.drafting import drafting_agent
from flow.agents.selector import candidate_selector
from flow.agents.import_contacts import import_contacts
from flow.run_agents import run_agents

if __name__ == "__main__":
    deployments = [
        research_agent.to_deployment(
            name="research-agent",
            triggers=[],  # manually triggered or called as subflow
        ),
        drafting_agent.to_deployment(
            name="drafting-agent",
            triggers=[],  # manually triggered or called as subflow
        ),
        import_contacts.to_deployment(
            name="import-contacts",
            triggers=[],  # manually triggered or called as subflow
        ),
        candidate_selector.to_deployment(name="candidate-selector"),
    ]

    # Optionally serve run_agents as the scheduled entry point
    if os.getenv("SERVE_RUN_AGENTS", "").lower() in ("1", "true", "yes"):
        deployments.insert(0, run_agents.to_deployment(name="run-agents"))

    serve(*deployments)
