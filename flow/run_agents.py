"""
run_agents: the orchestrating flow, and the only deployment you schedule.

Why this exists rather than three separate deployments calling each other:
Prefect keeps in-memory Python objects available to downstream work only
within the same flow. Across flow runs in separate processes, a return value
does not cross -- `run_deployment` hands back a FlowRun, not a result, and the
result is readable only if the flow was configured to persist it. Putting the
stages inside one flow removes that boundary entirely: candidates, research
output and drafts pass as ordinary dicts, with no serialization, no result
storage and no database handoff.

Each business is still a subflow, so it gets its own flow run in the UI, its
own state, and can be retried on its own without re-running its batch.

Two modes:
  business_ids=None  -> select a batch (tiers, batch_size)
  business_ids=[...] -> research exactly those, bypassing the batch filters
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow

from flow.agents.drafting import drafting_agent
from flow.agents.research import research_agent
from flow.agents.selector import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIERS,
    fetch_candidates,
)


@flow(log_prints=True)
async def run_agents(
    tiers: Optional[List[str]] = None,
    batch_size: Optional[int] = None,
    business_ids: Optional[List[str]] = None,
    template_slug: str = 'default',
) -> dict:
    """Select businesses, research each, and draft for the ones that pass."""
    if business_ids:
        print(f'researching {len(business_ids)} named business(es)')
        candidates = await fetch_candidates(
            tiers=None, batch_size=None, include_ids=business_ids)
        found = {c['business']['id'] for c in candidates}
        not_found = [i for i in business_ids if i not in found]
        if not_found:
            # Named and not found is worth saying out loud: the caller decided
            # these were worth researching, so silently dropping them would
            # look like the pipeline simply did nothing.
            print(f'NOT FOUND, skipped: {not_found}')
    else:
        tiers = tiers or DEFAULT_TIERS
        batch_size = batch_size or DEFAULT_BATCH_SIZE
        print(f'selecting up to {batch_size} from tiers {tiers}')
        candidates = await fetch_candidates(
            tiers=tiers, batch_size=batch_size, include_ids=None)
        not_found = []

    print(f'selected {len(candidates)}')

    outcomes: List[Dict[str, Any]] = []
    counts = {'researched': 0, 'rejected': 0, 'drafted': 0, 'errored': 0}

    for candidate in candidates:
        business = candidate['business']
        emails = candidate['candidate_emails']
        outcome: Dict[str, Any] = {
            'business_id': business.get('id'),
            'business_name': business.get('business_name'),
            'research_status': None,
            'draft_status': None,
        }

        try:
            # Subflow: its own flow run, retryable on its own. The crawl text
            # goes through whole -- no truncation anywhere in this path.
            researched = research_agent(
                business, emails, candidate['crawl_excerpt'])
        except Exception as exc:
            # One bad business must not cost the other 49 in the batch.
            print(f"research errored for {business.get('business_name')}: "
                  f"{type(exc).__name__}: {exc}")
            outcome['research_status'] = 'error'
            outcome['error'] = f'{type(exc).__name__}: {exc}'
            counts['errored'] += 1
            outcomes.append(outcome)
            continue

        outcome['research_status'] = researched['status']

        if researched['status'] != 'researched':
            outcome['rejection_reason'] = researched.get('reason')
            counts['rejected'] += 1
            outcomes.append(outcome)
            continue

        counts['researched'] += 1

        try:
            drafted = drafting_agent(
                researched['research'], emails, template_slug)
        except Exception as exc:
            print(f"drafting errored for {business.get('business_name')}: "
                  f"{type(exc).__name__}: {exc}")
            outcome['draft_status'] = 'error'
            outcome['error'] = f'{type(exc).__name__}: {exc}'
            counts['errored'] += 1
            outcomes.append(outcome)
            continue

        outcome['draft_status'] = drafted['status']
        if drafted['status'] == 'drafted':
            counts['drafted'] += 1
            outcome['draft'] = drafted['draft']
            outcome['outside_known_pool'] = drafted.get('outside_known_pool', [])

        outcomes.append(outcome)

    print(f"selected={len(candidates)} researched={counts['researched']} "
          f"rejected={counts['rejected']} drafted={counts['drafted']} "
          f"errored={counts['errored']}")

    return {
        'selected': len(candidates),
        'not_found': not_found,
        'outcomes': outcomes,
        **counts,
    }
