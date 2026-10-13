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
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

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
from flow.schemas import ResearchOutput, primary_contact

CONFIDENCE_THRESHOLD = float(
    os.environ.get('CONFIDENCE_THRESHOLD', '0.7')
)

SYSTEM_PROMPT = (
    'You research small service businesses to find specific, evidenced '
    'reasons a lead-generation service would matter to them. Quote evidence '
    'from the supplied text. Never invent a person or an email address. '
    'Extract the business name, the people the pages name and which of them '
    'make decisions, pain signals, a personalization hook, and a confidence '
    'score.'
)

# Deliberately loose: it only has to find the addresses a page states so they
# can be compared with the pool, not validate them. Whole-token matching is the
# point -- 'info@x.com' must not count as present inside 'myinfo@x.com'.
_EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def emails_in(*texts: Optional[str]) -> Set[str]:
    """Every address stated in the texts, lowercased."""
    found: Set[str] = set()
    for text in texts:
        if text:
            found.update(m.lower() for m in _EMAIL.findall(text))
    return found


def live_pool(
    verified_emails: list,
    *texts: Optional[str],
) -> Tuple[list, List[str]]:
    """
    Split the verified pool into (still live, presumed dead).

    Verification is a point-in-time MX check; the site is the current truth.
    If the pages show some of the pool, they are evidently listing the
    business's addresses, so a pool address they omit is presumed retired and
    excluded. If they show none, or all, there is nothing to conclude and the
    pool stands unchanged.
    """
    on_page = emails_in(*texts)
    shown = [e for e in verified_emails if e['email'].lower() in on_page]
    if not shown or len(shown) == len(verified_emails):
        return list(verified_emails), []
    excluded = [e['email'] for e in verified_emails
                if e['email'].lower() not in on_page]
    return shown, excluded


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
        'Return contacts as a list with one entry for every person the pages '
        'actually name (owner, founder, director, manager, trainer, groomer, '
        'vet). Leave the list empty rather than inventing anyone. For each, '
        'give name; title exactly as the page states it, or null; email only '
        'if the page ties an address to that person, or null; and '
        'is_decision_maker true when the title marks someone who decides on '
        'spending (owner, co-owner, founder, president, CEO, director, '
        'partner, general or practice manager, medical director), otherwise '
        'false. List decision makers first. '
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
    Persist the person this pass found, so the next run inherits it.

    leads.businesses has a single decision_maker column, so only the primary
    contact goes back: the first decision maker, else the first named person.

    Called as soon as a pass parses, not at the end: the name is true whether
    or not the confidence gate later rejects the business, and a rejected
    business still has a contactable human on its site.

    Failures are swallowed on purpose. The research output is already safe in
    its artifact and variable; a database problem must not turn a successful
    research pass into a failed one.
    """
    name, title = primary_contact(research.model_dump())
    if not name:
        return
    try:
        record_decision_maker_sync(
            business.get('id'),
            name,
            title,
            getattr(research, 'confidence', None),
            source_url=business.get('website'),
        )
    except Exception as exc:
        print(f'Note: decision maker writeback failed: '
              f'{type(exc).__name__}: {exc}')


def _enforce_pool(research: ResearchOutput, kept: list,
                  *texts: Optional[str]) -> ResearchOutput:
    """
    Strip addresses the output may not carry.

    suggested_email must come from the live pool; a stale or invented one is
    replaced by the first live address. A contact's email must be in the live
    pool or stated on the pages, else it is nulled -- an address attributed
    to a named person is exactly where a model guesses 'first@domain'.
    """
    live = {e['email'].lower() for e in kept}
    allowed = live | emails_in(*texts)

    suggested = research.suggested_email
    if kept and suggested.lower() not in live:
        print(f'suggested_email {suggested!r} not in the live pool; '
              f'using {kept[0]["email"]!r}')
        suggested = kept[0]['email']

    contacts = [
        c if c.email is None or c.email.lower() in allowed
        else c.model_copy(update={'email': None})
        for c in research.contacts
    ]
    return research.model_copy(
        update={'suggested_email': suggested, 'contacts': contacts})


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
    kept, excluded = live_pool(verified_emails, crawl_excerpt)
    texts: Tuple[Optional[str], ...] = (crawl_excerpt,)

    def finish(result: dict) -> dict:
        """Enforce the live pool on the research, then persist and return."""
        if result['research'] is not None:
            result['research'] = _enforce_pool(
                result['research'], kept, *texts).model_dump(mode='json')
        result['verified_emails'] = kept
        result['excluded_emails'] = excluded
        if excluded:
            print(f'excluded verified emails absent from the site: {excluded}')
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    prompt = build_research_prompt(business, kept, crawl_excerpt, offer=offer)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT,
                                    agent_model_env='RESEARCH_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        return finish({'status': 'rejected', 'reason': 'llm_schema_failure',
                       'research': None, 'passes': 1})

    _write_back_person(business, first)

    print(f'pass 1 confidence {first.confidence} '
          f'(threshold {CONFIDENCE_THRESHOLD})')

    if first.confidence >= CONFIDENCE_THRESHOLD:
        return finish({'status': 'researched', 'research': first,
                       'passes': 1})

    if not first.next_url_to_check:
        print('below threshold and no next_url_to_check; nothing to escalate')
        return finish({'status': 'rejected',
                       'reason': 'low_confidence_no_escalation_url',
                       'research': first, 'passes': 1})

    print(f'escalating: fetching {first.next_url_to_check}')
    extra = fetch_html(first.next_url_to_check)

    # The extra page is more evidence of which addresses are live, so the
    # pool is recomputed over both texts and pass 2 sees only that.
    texts = (crawl_excerpt, extra)
    kept, excluded = live_pool(verified_emails, *texts)

    second_prompt = build_research_prompt(
        business, kept, crawl_excerpt,
        extra_context=extra or '', offer=offer,
    )
    try:
        second = complete_structured(second_prompt, ResearchOutput,
                                     system=SYSTEM_PROMPT,
                                     agent_model_env='RESEARCH_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 2 failed: {type(exc).__name__}: {exc}')
        return finish({'status': 'rejected', 'reason': 'llm_schema_failure',
                       'research': first, 'passes': 2})

    # Pass 2 may name someone pass 1 missed, or name them better.
    _write_back_person(business, second)

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        return finish({'status': 'researched', 'research': second,
                       'passes': 2})

    return finish({'status': 'rejected',
                   'reason': 'low_confidence_after_two_passes',
                   'research': second, 'passes': 2})
