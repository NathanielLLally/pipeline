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
import html
import unicodedata
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
from flow.email_pool import deliverability, describe_address
from flow.offer import resolve_offer
from flow.schemas import (ResearchOutput, failure_reason, find_payment_terms,
                          primary_contact)

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

    Omission is weak evidence, though, so two omitted addresses are kept: one
    the mailbox accepted, and a person's own address whose local part is the
    first name of someone the pages name. Home Dog Training's site printed a
    typo'd shared inbox and another business's address, and not
    wyatt@ -- their own domain, for a trainer named three times.
    """
    on_page = emails_in(*texts)
    shown = [e for e in verified_emails if e['email'].lower() in on_page]
    if not shown or len(shown) == len(verified_emails):
        return list(verified_emails), []
    kept = [e for e in verified_emails
            if e['email'].lower() in on_page
            or deliverability(e) == 'valid'
            or _named_on_page(e, texts)]
    excluded = [e['email'] for e in verified_emails if e not in kept]
    return kept, excluded


def _named_on_page(entry: dict, texts: Tuple[Optional[str], ...]) -> bool:
    """
    True for a person's own address whose first local-part segment is a
    whole word on the pages: wyatt@ or wyatt.smith@ where the page says
    'Wyatt'. Only for is_role False -- 'info', 'hello' and 'training' are
    words on every page -- and not for entries that do not say.
    """
    if entry.get('is_role') is not False:
        return False
    first = re.split(r'[._+-]', entry['email'].split('@', 1)[0])[0]
    if len(first) < 3 or not first.isalpha():
        return False
    word = re.compile(rf'\b{re.escape(first)}\b', re.IGNORECASE)
    return any(word.search(t) for t in texts if t)


def split_undeliverable(pool: list) -> Tuple[list, List[str]]:
    """
    (usable, the addresses the email check proved dead).

    Only 'invalid' goes: a catch-all domain or a check that never finished
    says nothing against the mailbox. Done before live_pool, because a dead
    address printed on the page would otherwise count as evidence of which
    addresses are live.
    """
    usable = [e for e in pool if deliverability(e) != 'invalid']
    dead = [e['email'] for e in pool if deliverability(e) == 'invalid']
    return usable, dead


def build_research_prompt(
    business: dict,
    verified_emails: list,
    crawl_excerpt: str,
    extra_context: str = '',
    offer: Optional[str] = None,
) -> str:
    """Assemble the research prompt. Pure, so it can be asserted on."""
    pool = [f'- {describe_address(e, business)}' for e in verified_emails]

    parts = [
        f"What we are offering them: {resolve_offer(offer)}",
        '',
        f"Business: {business.get('business_name')}",
        f"Website: {business.get('website')}",
        'Email pool, with what our email check found:',
        *(pool or ['(none)']),
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
        'if the page ties an address to that person, or null; evidence, '
        'the one passage copied verbatim from the text that best shows '
        'their role, or null; and is_decision_maker true when the title or '
        'that passage shows they own or run the business -- owner, '
        'co-owner, founder, president, CEO, director, partner, general or '
        'practice manager, medical director, or in other words: they speak '
        'of starting it, or of it as their family business -- not merely '
        'that they work there; otherwise false. For a decision maker, the '
        'evidence must be the passage that shows the ownership (for '
        'example "the dog who started everything for Laura and me"), not '
        'one that only lists them among the staff. List decision makers '
        'first. '
        'Return pain_signals as a list of short strings, a '
        'personalization_hook, and inferred_tone as exactly one of: '
        'clinical, warm, premium, casual, sparse -- one of those five words '
        'and nothing else, not a description of the tone. Also a '
        'confidence between 0 and 1, evidence as a list of strings quoted '
        'verbatim from the text above, and '
        'suggested_email chosen from the email pool -- one the mailbox '
        'accepted over one the check could not confirm, and a decision '
        "maker's own address over a shared inbox. "
        'Also return outreach_angle: the one thing about their business '
        "itself -- not their customers' problems -- that our offer speaks "
        'to. Either a problem they state (lost marketing budget, slow '
        'months, empty classes, a closed location, trainers to keep busy) or '
        'a growth move (hiring, a new location or program, free lessons or '
        'classes that need filling). Give problem as one plain sentence; '
        'evidence as a quote copied verbatim from the text above that shows '
        'it; and caution as what the pitch must not trip over given what '
        'they have been through (for example, if paid ads burned them, say '
        'so), or null. The caution says what to avoid, not what to promise: '
        'never payment terms, pricing, guarantees or features we have not '
        'stated. Set outreach_angle to null when the text says '
        'nothing about the business itself. '
        'If confidence is low and another page on the site would help, set '
        'next_url_to_check.',
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
    # With no decision maker, the primary contact is just whoever was listed
    # first -- a trainer as easily as an owner. That guess must not replace
    # a name the business already has.
    if business.get('decision_maker_name') and not any(
            c.is_decision_maker for c in research.contacts):
        print(f'no decision maker found; keeping '
              f"{business['decision_maker_name']!r} over {name!r}")
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


def _norm(text: str) -> str:
    """Text for matching a quote against a page: entities decoded, curly
    quotes straightened, emoji and other pictographs removed (models drop
    them when quoting), whitespace collapsed, case folded."""
    text = unicodedata.normalize('NFKC', html.unescape(text or ''))
    text = text.translate({0x2018: "'", 0x2019: "'", 0x201c: '"', 0x201d: '"'})
    text = ''.join(ch for ch in text
                   if unicodedata.category(ch) not in _PICTOGRAPH)
    return re.sub(r'\s+', ' ', text).strip().casefold()


# Emoji, their joiners and variation selectors, and modifier symbols.
_PICTOGRAPH = {'So', 'Sk', 'Cf', 'Mn', 'Cs'}

# Words that show someone owns or runs the business, as opposed to working
# in it. An evidence quote has to carry one to make its person a decision
# maker: the model cited 'Charles & Wyatt will be leading all group classes'
# for that, and leading a class is not owning the business.
_OWNERSHIP = re.compile(
    r'(?<!\w)(own|owns|owned|owner|owners|co-owner|co-owners|founder|'
    r'founders|co-founder|founded|started|start|ceo|president|director|'
    r'partner|partners|manager|family business|our business|my business)'
    r'(?!\w)')


# How far from a person's name their title may sit and still be theirs.
TITLE_WINDOW = 80


def _title_stated(title: str, name: str, page: str) -> bool:
    """
    True if `title` appears as a whole phrase within TITLE_WINDOW characters
    of `name` (or its first word) on the normalised page. Anywhere on the
    page is not enough: every pet site says 'owner', as in 'dog owners'.
    """
    wanted = _norm(title)
    phrase = re.compile(rf'(?<!\w){re.escape(wanted)}(?!\w)')
    full = _norm(name)
    for who in dict.fromkeys([full, full.split(' ')[0]]):
        if not who:
            continue
        for hit in re.finditer(rf'(?<!\w){re.escape(who)}(?!\w)', page):
            nearby = page[max(0, hit.start() - TITLE_WINDOW):
                          hit.end() + TITLE_WINDOW]
            if phrase.search(nearby):
                return True
    return False


def _pieces(quote: str) -> List[str]:
    """
    The verbatim passages in a quote. Models join passages with '...' (or
    '\u2026', which _norm turns into '...'); each passage is on the page
    though the joined whole is not.
    """
    return [p for p in re.split(r'\s*\.\.\.\s*', _norm(quote)) if p]


def _on_page(quote: str, page: str) -> bool:
    """Every passage of the quote is on the normalised page."""
    pieces = _pieces(quote)
    return bool(pieces) and all(p in page for p in pieces)


def _shows_ownership(quote: str, name: str) -> bool:
    """One passage both names the person and carries an ownership word --
    in the same passage, so a name from one place and 'started' from
    another cannot be stitched into an owner."""
    return any(_names(p, name) and _OWNERSHIP.search(p) is not None
               for p in _pieces(quote))


def _names(quote: str, name: str) -> bool:
    """True if the quote names the person: their first name, whole word."""
    first = _norm(name).split(' ')[0]
    return bool(first) and re.search(
        rf'(?<!\w){re.escape(first)}(?!\w)', _norm(quote)) is not None


def _enforce_page(research: ResearchOutput,
                  *texts: Optional[str]) -> ResearchOutput:
    """
    Strip what the pages do not support, before anything is written back.

    A title must be stated beside the person's name, else it is nulled. A
    contact's evidence quote must be on the page, else it is nulled. A
    decision maker must keep one of the two -- a stated title, or a quote
    that names them -- because a family business shows ownership in other
    words ('started everything for Laura and me') far more often than it
    prints 'Owner' beside a name.
    An outreach_angle whose quote is not on the page is dropped whole: it
    would be an invented fact in the email's opening line.
    """
    page = _norm('\n'.join(t for t in texts if t))

    contacts = []
    for c in research.contacts:
        title = c.title if c.title and _title_stated(c.title, c.name, page) \
            else None
        if c.title and title is None:
            print(f'title {c.title!r} for {c.name!r} is not on the page; '
                  f'nulled')
        evidence = c.evidence if c.evidence and _on_page(c.evidence, page) \
            else None
        if c.evidence and evidence is None:
            print(f'evidence for {c.name!r} is not on the page; nulled')
        shown = title is not None or (
            evidence is not None and _shows_ownership(evidence, c.name))
        contacts.append(c.model_copy(update={
            'title': title, 'evidence': evidence,
            'is_decision_maker': c.is_decision_maker and shown}))

    angle = research.outreach_angle
    if angle is not None and not _on_page(angle.evidence, page):
        print(f'outreach_angle quote not on the page; dropped: '
              f'{angle.evidence[:120]!r}')
        angle = None
    # Told the caution is what to avoid, the model still wrote 'pay-per-lead
    # safety' into it, and the drafter copied that into the email. Customers
    # pay upfront; a caution carrying payment terms goes. Only terms: 'do
    # not pitch ad spend or guarantee results' is what a caution is for.
    if angle is not None and angle.caution and \
            find_payment_terms(angle.caution):
        print(f'outreach_angle caution carries payment terms; '
              f'dropped: {angle.caution[:120]!r}')
        angle = angle.model_copy(update={'caution': None})

    return research.model_copy(
        update={'contacts': contacts, 'outreach_angle': angle})


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
    usable, undeliverable = split_undeliverable(verified_emails)
    kept, excluded = live_pool(usable, crawl_excerpt)
    texts: Tuple[Optional[str], ...] = (crawl_excerpt,)

    def finish(result: dict) -> dict:
        """Enforce the live pool on the research, then persist and return."""
        if result['research'] is not None:
            result['research'] = _enforce_pool(
                result['research'], kept, *texts).model_dump(mode='json')
        result['verified_emails'] = kept
        result['excluded_emails'] = excluded
        result['undeliverable_emails'] = undeliverable
        if excluded:
            print(f'excluded verified emails absent from the site: {excluded}')
        if undeliverable:
            print(f'dropped addresses the email check found do not exist: '
                  f'{undeliverable}')
        _set_research_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
        return result

    # Every address is dead: drafting would have to invent one, so stop
    # before paying for a model call.
    if verified_emails and not usable:
        return finish({'status': 'rejected', 'reason': 'no_verified_emails',
                       'research': None, 'passes': 0})

    prompt = build_research_prompt(business, kept, crawl_excerpt, offer=offer)

    try:
        first = complete_structured(prompt, ResearchOutput,
                                    system=SYSTEM_PROMPT,
                                    agent_model_env='RESEARCH_MODEL')
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'pass 1 failed: {type(exc).__name__}: {exc}')
        return finish({'status': 'rejected', 'reason': failure_reason(exc),
                       'research': None, 'passes': 1})

    first = _enforce_page(first, *texts)
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
    kept, excluded = live_pool(usable, *texts)

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
        return finish({'status': 'rejected', 'reason': failure_reason(exc),
                       'research': first, 'passes': 2})

    # Pass 2 may name someone pass 1 missed, or name them better.
    second = _enforce_page(second, *texts)
    _write_back_person(business, second)

    print(f'pass 2 confidence {second.confidence}')

    if second.confidence >= CONFIDENCE_THRESHOLD:
        return finish({'status': 'researched', 'research': second,
                       'passes': 2})

    return finish({'status': 'rejected',
                   'reason': 'low_confidence_after_two_passes',
                   'research': second, 'passes': 2})
