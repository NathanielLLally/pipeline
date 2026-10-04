"""
Serve the pipeline deployments.

    $ set -a && . ./.env && set +a && python flow/serve_agents.py

Three deployments:

  research-agent    research one business, with escalation on low confidence.

  drafting-agent    draft one outreach email for a researched business.

  candidate-selector  batch query: what would the next batch be, without
                    spending a token.

Must run on the same host as whatever triggers these: PREFECT_API_URL reads
127.0.0.1:4200 on both the workstation and the prod host while meaning
different servers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.agents.research import research_agent
from flow.agents.drafting import drafting_agent
from flow.agents.selector import candidate_selector

if __name__ == "__main__":
    serve(
        research_agent.to_deployment(
            name="research-agent",
            triggers=[],  # manually triggered or called as subflow
        ),
        drafting_agent.to_deployment(
            name="drafting-agent",
            triggers=[],  # manually triggered or called as subflow
        ),
        candidate_selector.to_deployment(name="candidate-selector"),
    )
