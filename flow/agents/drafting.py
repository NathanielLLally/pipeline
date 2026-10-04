"""
The drafting-agent deployment.

Its contract: a ResearchOutput plus the verified email pool in, a
DraftingOutput out. The agent chooses which verified address(es) to write to
based on the message's intent -- it is not bound by the researcher's
suggestion (spec section 2.1), and it is NOT bound to the verified pool
either: Warmbly refuses to send to anything it has not itself verified, so
enforcing that here would only discard usable drafts. Out-of-pool selections
are reported in `outside_known_pool` for observability.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from typing import Any, Dict, Optional

from prefect import flow
from prefect.variables import Variable

from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.offer import OUTREACH_GOAL, resolve_offer
from flow.schemas import DraftingOutput

SYSTEM_PROMPT = (
    'You write short, specific first-contact emails to small service business '
    'owners. One concrete observation, one offer, one ask. No flattery, no '
    'placeholders, no invented facts. Write only to addresses you were given.'
)


def build_drafting_prompt(
    research: dict,
    verified_emails: list,
    template_slug: str = 'default',
    offer: Optional[str] = None,
) -> str:
    """Assemble the drafting prompt. Pure, so it can be asserted on."""
    pool = ', '.join(e['email'] for e in verified_emails) or '(none)'

    return '\n'.join([
        f"What we are offering them: {resolve_offer(offer)}",
        '',
        OUTREACH_GOAL,
        '',
        f"Business: {research.get('business_name')}",
        f"Tone observed on their site: {research.get('inferred_tone')}",
        f"Why this matters to them: {research.get('personalization_hook')}",
        f"Pain signals: {'; '.join(research.get('pain_signals') or [])}",
        f"Evidence: {'; '.join(research.get('evidence') or [])}",
        f"Researcher suggested: {research.get('suggested_email')}",
        f"Template: {template_slug}",
        '',
        f"Verified addresses you may write to: {pool}",
        '',
        'Choose selected_emails from the verified addresses above and only '
        'those. Return subject, body, and a rationale for the framing.',
    ])


def _set_drafting_output(result: dict, business_id: str) -> dict:
    """Store drafting output as a Prefect variable and return it."""
    var_name = f'drafting-agent-output-{business_id}'
    try:
        Variable.set(var_name, result, overwrite=True)
    except Exception as e:
        # Variable storage is optional; don't fail the flow if it's unavailable
        # (e.g., during unit tests with ephemeral servers).
        print(f'Note: could not persist output variable {var_name}: {e}')
    return result


@flow(log_prints=True)
def drafting_agent(
    research: dict,
    verified_emails: list,
    template_slug: str = 'default',
    offer: Optional[str] = None,
) -> dict:
    """Draft one outreach email for a researched business."""
    business_id = research.get('business_id', 'unknown')
    pool = {e['email'] for e in verified_emails}
    prompt = build_drafting_prompt(
        research, verified_emails, template_slug, offer)

    try:
        draft = complete_structured(prompt, DraftingOutput,
                                    system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'drafting failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': 'llm_schema_failure',
                  'draft': None}
        return _set_drafting_output(result, business_id)

    # Warmbly will not send to an address it has not itself verified, and it
    # has its own verification. So the pool is an input and a prompt steer,
    # not an allowlist: rejecting here would discard a usable draft and throw
    # away an address Warmbly could have verified itself. Report, do not gate.
    outside_known_pool = [e for e in draft.selected_emails if e not in pool]
    if outside_known_pool:
        print(f'selected outside the known pool: {outside_known_pool} '
              f'(Warmbly verifies before sending)')

    print(f'drafted to {draft.selected_emails}: {draft.subject}')
    result = {'status': 'drafted', 'draft': draft.model_dump(mode='json'),
              'outside_known_pool': outside_known_pool}
    return _set_drafting_output(result, business_id)
