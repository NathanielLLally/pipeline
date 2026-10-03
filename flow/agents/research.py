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

from flow.fetch import fetch_html
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.schemas import ResearchOutput

CONFIDENCE_THRESHOLD = float(
    os.environ.get('CONFIDENCE_THRESHOLD', '0.7')
)

SYSTEM_PROMPT = (
    'You research small service businesses to find one specific, evidenced '
    'reason a lead-generation offer would matter to them. Quote evidence '
    'from the supplied text. Never invent an email address.'
)


def build_research_prompt(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str,
    extra_context: str = '',
) -> str:
    """Assemble the research prompt. Pure, so it can be asserted on."""
    pool = ', '.join(e['email'] for e in verified_emails) or '(none)'

    parts = [
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
        'Return pain_signals, a personalization_hook, inferred_tone, a '
        'confidence between 0 and 1, evidence quoted from the text above, and '
        'suggested_email chosen from the verified pool. If confidence is low '
        'and another page on the site would help, set next_url_to_check.',
    ]
    return '\n'.join(parts)


@flow(log_prints=True)
def research_agent(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str = '',
) -> dict:
    """Research one business, escalating once if confidence is low."""
    prompt = build_research_prompt(business, verified_emails, crawl_excerpt)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        return {'status': 'rejected', 'reason': 'llm_schema_failure',
                'research': None, 'passes': 1}

    print(f'pass 1 confidence {first.confidence} '
          f'(threshold {CONFIDENCE_THRESHOLD})')

    if first.confidence >= CONFIDENCE_THRESHOLD:
        return {'status': 'researched', 'research': first.model_dump(mode='json'),
                'passes': 1}

    if not first.next_url_to_check:
        print('below threshold and no next_url_to_check; nothing to escalate')
        return {'status': 'rejected',
                'reason': 'low_confidence_no_escalation_url',
                'research': first.model_dump(mode='json'), 'passes': 1}

    print(f'escalating: fetching {first.next_url_to_check}')
    extra = fetch_html(first.next_url_to_check)

    second_prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt, extra_context=extra or '',
    )
    try:
        second = complete_structured(second_prompt, ResearchOutput,
                                     system=SYSTEM_PROMPT)
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 2 failed: {type(exc).__name__}: {exc}')
        return {'status': 'rejected', 'reason': 'llm_schema_failure',
                'research': first.model_dump(mode='json'), 'passes': 2}

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        return {'status': 'researched',
                'research': second.model_dump(mode='json'), 'passes': 2}

    return {'status': 'rejected',
            'reason': 'low_confidence_after_two_passes',
            'research': second.model_dump(mode='json'), 'passes': 2}
