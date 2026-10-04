"""
The research-agent deployment.

Its contract: a business record plus a verified email pool in, a
ResearchOutput or a rejection out. Pass 1, the confidence gate, deeper_fetch
and pass 2 all live inside, because escalation is this node's own business and
its caller should not have to drive it (spec section 2.3).
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow
from prefect.variables import Variable

from flow.fetch import fetch_html
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.offer import resolve_offer
from flow.schemas import ResearchOutput

CONFIDENCE_THRESHOLD = float(
    os.environ.get('CONFIDENCE_THRESHOLD', '0.7')
)

SYSTEM_PROMPT = (
    'You research small service businesses to find specific, evidenced '
    'reasons a lead-generation service would matter to them. Quote evidence '
    'from the supplied text. Never invent an email address.  Extract business '
    ' name, pain signals, personalization hook, and a confidence score.'
)


def build_research_prompt(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str,
    extra_context: str = '',
    offer: Optional[str] = None,
) -> str:
    """Assemble the research prompt. Pure, so it can be asserted on."""
    pool = ', '.join(e['email'] for e in verified_emails) or '(none)'

    parts = [
        f"What we are offering them: {resolve_offer(offer)}",
        '',
        f"Business: {business.get('business_name')}",
        f"Website: {business.get('website')}",
        f"Verified email pool: {pool}",
        '',
        'Site text:',
        crawl_excerpt or '(none available)',
    ]
    if extra_context:
        parts += ['', 'Additional page fetched for more detail:', extra_context]

    parts += [
        '',
        'Extract contact_name and contact_title if the pages actually name '
        'a person to write to (an owner, founder or trainer); leave them null '
        'rather than inventing one. '
        'Return pain_signals, a personalization_hook, inferred_tone, a '
        'confidence between 0 and 1, evidence quoted from the text above, and '
        'suggested_email chosen from the verified pool. If confidence is low '
        'and another page on the site would help, set next_url_to_check.',
    ]
    return '\n'.join(parts)


def _set_research_output(result: dict, business_id: str) -> dict:
    """Store research output as a Prefect variable and return it."""
    var_name = f'research-agent-output-{business_id}'
    try:
        Variable.set(var_name, result, overwrite=True)
    except Exception as e:
        # Variable storage is optional; don't fail the flow if it's unavailable
        # (e.g., during unit tests with ephemeral servers).
        print(f'Note: could not persist output variable {var_name}: {e}')
    return result


@flow(log_prints=True)
def research_agent(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str = '',
    offer: Optional[str] = None,
) -> dict:
    """Research one business, escalating once if confidence is low."""
    business_id = business.get('id', 'unknown')
    prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt, offer=offer)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': 'llm_schema_failure',
                  'research': None, 'passes': 1}
        return _set_research_output(result, business_id)

    print(f'pass 1 confidence {first.confidence} '
          f'(threshold {CONFIDENCE_THRESHOLD})')

    if first.confidence >= CONFIDENCE_THRESHOLD:
        result = {'status': 'researched', 'research': first.model_dump(mode='json'),
                  'passes': 1}
        return _set_research_output(result, business_id)

    if not first.next_url_to_check:
        print('below threshold and no next_url_to_check; nothing to escalate')
        result = {'status': 'rejected',
                  'reason': 'low_confidence_no_escalation_url',
                  'research': first.model_dump(mode='json'), 'passes': 1}
        return _set_research_output(result, business_id)

    print(f'escalating: fetching {first.next_url_to_check}')
    extra = fetch_html(first.next_url_to_check)

    second_prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt,
        extra_context=extra or '', offer=offer,
    )
    try:
        second = complete_structured(second_prompt, ResearchOutput,
                                     system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 2 failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': 'llm_schema_failure',
                  'research': first.model_dump(mode='json'), 'passes': 2}
        return _set_research_output(result, business_id)

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        result = {'status': 'researched',
                  'research': second.model_dump(mode='json'), 'passes': 2}
        return _set_research_output(result, business_id)

    result = {'status': 'rejected',
              'reason': 'low_confidence_after_two_passes',
              'research': second.model_dump(mode='json'), 'passes': 2}
    return _set_research_output(result, business_id)
