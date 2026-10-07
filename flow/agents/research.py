"""
The research-agent deployment.

Its contract: a business record plus a verified email pool in, a
ResearchOutput or a rejection out. Pass 1, the confidence gate, deeper_fetch
and pass 2 all live inside, because escalation is this node's own business and
its caller should not have to drive it (spec section 2.3).

Can be called with dict params or from a JSON input artifact:
  research_agent(business=..., verified_emails=..., crawl_excerpt=...)
  research_agent(json_input_file='biz-1-research-input.json')
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow
from prefect.variables import Variable

from flow.artifacts import (
    read_artifact,
    register_artifact,
    variable_safe_name,
    write_artifact,
)
from flow.fetch import fetch_html
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.decision_maker import record_decision_maker_sync
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
        'Return pain_signals as a list of short strings, a '
        'personalization_hook, and inferred_tone as exactly one of: '
        'clinical, warm, premium, casual, sparse -- one of those five words '
        'and nothing else, not a description of the tone. Also a '
        'confidence between 0 and 1, evidence as a list of strings quoted '
        'verbatim from the text above, and '
        'suggested_email chosen from the verified pool. If confidence is low '
        'and another page on the site would help, set next_url_to_check.',
    ]
    return '\n'.join(parts)


def _write_back_person(business: dict, research: Any) -> None:
    """
    Persist a person this pass found, so the next run inherits it.

    Called as soon as a pass parses, not at the end: the name is true whether
    or not the confidence gate later rejects the business, and a rejected
    business still has a contactable human on its site.

    Failures are swallowed on purpose. The research output is already safe in
    its artifact and variable; a database problem must not turn a successful
    research pass into a failed one.
    """
    name = getattr(research, 'contact_name', None)
    if not name:
        return
    try:
        record_decision_maker_sync(
            business.get('id'),
            name,
            getattr(research, 'contact_title', None),
            getattr(research, 'confidence', None),
            source_url=business.get('website'),
        )
    except Exception as exc:
        print(f'Note: decision maker writeback failed: '
              f'{type(exc).__name__}: {exc}')


def _set_research_output(result: dict, business_id: str) -> dict:
    """Store research output as a Prefect variable and return it."""
    var_name = variable_safe_name(f'research_agent_output_{business_id}')
    try:
        Variable.set(var_name, result, overwrite=True)
    except Exception as e:
        # Variable storage is optional; don't fail the flow if it's unavailable
        # (e.g., during unit tests with ephemeral servers).
        print(f'Note: could not persist output variable {var_name}: {e}')
    return result


@flow(log_prints=True)
def research_agent(
    business: Optional[dict] = None,
    verified_emails: Optional[list] = None,
    crawl_excerpt: str = '',
    offer: Optional[str] = None,
    json_input_file: Optional[str] = None,
) -> dict:
    """
    Research one business, escalating once if confidence is low.

    Can be called with explicit dict params or from a JSON input artifact:
      research_agent(business={...}, verified_emails=[...], ...)
      research_agent(json_input_file='biz-1-research-input.json')

    If json_input_file is provided, it takes precedence and dict params are ignored.
    Writes output and input artifacts to the current directory, tagged by flow run tags.
    """
    # Load from JSON if provided
    if json_input_file:
        try:
            input_data = read_artifact(json_input_file, suffix='input')
            business = input_data.get('business')
            verified_emails = input_data.get('verified_emails', [])
            crawl_excerpt = input_data.get('crawl_excerpt', '')
            offer = input_data.get('offer')
        except Exception as e:
            print(f'Failed to load input artifact: {e}')
            raise
    else:
        # Validate that required params are present
        if business is None or verified_emails is None:
            raise ValueError(
                'Either json_input_file or (business, verified_emails) required'
            )
        input_data = {
            'business': business,
            'verified_emails': verified_emails,
            'crawl_excerpt': crawl_excerpt,
            'offer': offer,
        }

    # Write input artifact
    try:
        write_artifact(input_data, suffix='input')
        register_artifact(input_data, suffix='input', description='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    business_id = business.get('id', 'unknown')
    prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt, offer=offer)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT,
                                    agent_model_env='RESEARCH_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': 'llm_schema_failure',
                  'research': None, 'passes': 1}
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    _write_back_person(business, first)

    print(f'pass 1 confidence {first.confidence} '
          f'(threshold {CONFIDENCE_THRESHOLD})')

    if first.confidence >= CONFIDENCE_THRESHOLD:
        result = {'status': 'researched', 'research': first.model_dump(mode='json'),
                  'passes': 1}
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    if not first.next_url_to_check:
        print('below threshold and no next_url_to_check; nothing to escalate')
        result = {'status': 'rejected',
                  'reason': 'low_confidence_no_escalation_url',
                  'research': first.model_dump(mode='json'), 'passes': 1}
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    print(f'escalating: fetching {first.next_url_to_check}')
    extra = fetch_html(first.next_url_to_check)

    second_prompt = build_research_prompt(
        business, verified_emails, crawl_excerpt,
        extra_context=extra or '', offer=offer,
    )
    try:
        second = complete_structured(second_prompt, ResearchOutput,
                                     system=SYSTEM_PROMPT,
                                     agent_model_env='RESEARCH_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 2 failed: {type(exc).__name__}: {exc}')
        result = {'status': 'rejected', 'reason': 'llm_schema_failure',
                  'research': first.model_dump(mode='json'), 'passes': 2}
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    # Pass 2 may name someone pass 1 missed, or name them better.
    _write_back_person(business, second)

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        result = {'status': 'researched',
                  'research': second.model_dump(mode='json'), 'passes': 2}
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    result = {'status': 'rejected',
              'reason': 'low_confidence_after_two_passes',
              'research': second.model_dump(mode='json'), 'passes': 2}
    _set_research_output(result, business_id)
    write_artifact(result, suffix='output')
    register_artifact(result, suffix='output', description='output')
    return result
