"""
The drafting-agent deployment.

Its contract: a ResearchOutput plus the verified email pool in, a
DraftingOutput out. The agent chooses which verified address(es) to write to
based on the message's intent -- it is not bound by the researcher's
suggestion (spec section 2.1), and it is NOT bound to the verified pool
either: Warmbly refuses to send to anything it has not itself verified, so
enforcing that here would only discard usable drafts. Out-of-pool selections
are reported in `outside_known_pool` for observability.

Can be called with dict params or from a JSON input artifact:
  drafting_agent(research={...}, verified_emails=[...], ...)
  drafting_agent(json_input_file='biz-1-drafting-input.json')
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow
from prefect.variables import Variable

from flow.artifacts import (
    read_artifact,
    register_artifact,
    variable_safe_name,
    write_artifact,
)
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.email_pool import (address_rank, choose_recipient, describe_address,
                              greeting_name)
from flow.offer import build_outreach_goal, resolve_offer
from flow.schemas import DraftingOutput, failure_reason

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
    website: Optional[str] = None,
) -> str:
    """Assemble the drafting prompt. Pure, so it can be asserted on."""
    # Without the website the off-domain note is left out rather than
    # guessed: the researcher's pick can itself be on a sibling domain.
    business = {'website': website}
    contacts = research.get('contacts') or []
    recipient = choose_recipient(verified_emails, contacts)

    lines = [
        f"What we are offering them: {resolve_offer(offer)}",
        '',
        build_outreach_goal(research),
        '',
        f"Business: {research.get('business_name')}",
        f"Tone observed on their site: {research.get('inferred_tone')}",
    ]
    # With an outreach_angle the goal above already says what this matters
    # to them; the hook alongside it was a second, competing steer.
    if not research.get('outreach_angle'):
        lines.append(
            f"Why this matters to them: {research.get('personalization_hook')}")
    lines += [
        f"Pain signals: {'; '.join(research.get('pain_signals') or [])}",
        f"Evidence: {'; '.join(research.get('evidence') or [])}",
        f"People named on their site: {_people(contacts)}",
        f"Template: {template_slug}",
        '',
    ]
    # Who to write to and who to greet are decided here, not by the model:
    # told the rule in words, it wrote to a trainer while the owners were
    # known. drafting_agent holds the selection to it afterwards too.
    if recipient is None:
        lines += ['Addresses you may write to: (none)', '']
    else:
        others = [e for e in verified_emails if e is not recipient]
        name = greeting_name(recipient['email'], contacts)
        lines += [
            f"Write to: {describe_address(recipient, business)}",
            *(['Other addresses on file, for reference only:']
              + [f'- {describe_address(e, business)}' for e in others]
              if others else []),
            '',
            f'Greet {name} by first name.' if name else
            'Greet no one by name: no decision maker is named for this '
            'address.',
            '',
        ]
    lines.append(
        'Put that address in selected_emails, and only addresses given '
        'above. Return subject, email_text (the email body), and a '
        'rationale for the framing.')
    return '\n'.join(lines)


def hold_to_recipient(selected: list, verified_emails: list,
                      contacts: list) -> list:
    """
    The selection, held to the address build_drafting_prompt named.

    A pick from the pool ranked below that address is dropped, and if no
    pool address is left the named one is put first. Equally ranked picks
    and addresses outside the pool pass through -- the pool is not an
    allowlist (see drafting_agent).
    """
    recipient = choose_recipient(verified_emails, contacts)
    if recipient is None:
        return list(selected)
    best = address_rank(recipient, contacts)
    by_address = {e['email'].lower(): e for e in verified_emails}
    kept = [a for a in selected
            if a.lower() not in by_address
            or address_rank(by_address[a.lower()], contacts) <= best]
    if not any(a.lower() in by_address for a in kept):
        kept.insert(0, recipient['email'])
    return kept


def _people(contacts: Optional[list]) -> str:
    """'Charles (Owner; decides on spending), Laura (no title stated)'."""
    described = []
    for c in contacts or []:
        notes = [c.get('title') or 'no title stated']
        if c.get('is_decision_maker'):
            notes.append('decides on spending')
        if c.get('email'):
            notes.append(c['email'])
        described.append(f"{c.get('name')} ({'; '.join(notes)})")
    return ', '.join(described) or 'none'


def _set_drafting_output(result: dict, business_id: str) -> dict:
    """Store drafting output as a Prefect variable and return it."""
    var_name = variable_safe_name(f'drafting_agent_output_{business_id}')
    try:
        Variable.set(var_name, result, overwrite=True)
    except Exception as e:
        # Variable storage is optional; don't fail the flow if it's unavailable
        # (e.g., during unit tests with ephemeral servers).
        print(f'Note: could not persist output variable {var_name}: {e}')
    return result


@flow(log_prints=True)
def drafting_agent(
    research: Optional[dict] = None,
    verified_emails: Optional[list] = None,
    template_slug: str = 'default',
    offer: Optional[str] = None,
    json_input_file: Optional[str] = None,
    website: Optional[str] = None,
    business_id: Optional[str] = None,
) -> dict:
    """
    Draft one outreach email for a researched business.

    Can be called with explicit dict params or from a JSON input artifact:
      drafting_agent(research={...}, verified_emails=[...], ...)
      drafting_agent(json_input_file='biz-1-drafting-input.json')

    If json_input_file is provided, it takes precedence and dict params are ignored.
    Writes output and input artifacts to the current directory, tagged by flow run tags.
    """
    # Load from JSON if provided
    if json_input_file:
        try:
            input_data = read_artifact(json_input_file, suffix='input')
            research = input_data.get('research')
            verified_emails = input_data.get('verified_emails', [])
            template_slug = input_data.get('template_slug', 'default')
            offer = input_data.get('offer')
            website = input_data.get('website')
            business_id = input_data.get('business_id')
        except Exception as e:
            print(f'Failed to load input artifact: {e}')
            raise
    else:
        # Validate that required params are present
        if research is None or verified_emails is None:
            raise ValueError(
                'Either json_input_file or (research, verified_emails) required'
            )
        input_data = {
            'research': research,
            'verified_emails': verified_emails,
            'template_slug': template_slug,
            'offer': offer,
            'website': website,
            'business_id': business_id,
        }

    # Write input artifact
    try:
        write_artifact(input_data, suffix='input')
        register_artifact(input_data, suffix='input', description='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    # Given by the caller: research output carries no business id, and
    # keying on it alone stored every business under
    # 'drafting_agent_output_unknown', the last to finish winning.
    business_id = business_id or research.get('business_id') or 'unknown'
    pool = {e['email'] for e in verified_emails}
    prompt = build_drafting_prompt(
        research, verified_emails, template_slug, offer, website=website)

    try:
        draft = complete_structured(prompt, DraftingOutput,
                                    system=SYSTEM_PROMPT,
                                    agent_model_env='DRAFTING_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'drafting failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': failure_reason(exc),
                  'draft': None}
        _set_drafting_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    held = hold_to_recipient(draft.selected_emails, verified_emails,
                             research.get('contacts') or [])
    if held != list(draft.selected_emails):
        print(f'selection {draft.selected_emails} held to the chosen '
              f'recipient: {held}')
        draft = draft.model_copy(update={'selected_emails': held})

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
    _set_drafting_output(result, business_id)
    write_artifact(result, suffix='output')
    register_artifact(result, suffix='output', description='output')
    return result
