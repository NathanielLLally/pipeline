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

From the command line, with .env sourced:

    $ python flow/run_agents.py --batch-size 3
    $ python flow/run_agents.py --business-ids <id> <id> --research-tier frontier

Research runs at reasoning effort 'high' from here unless told otherwise; a
--research-tier also switches research to the multi-agent graph, the only
place a tier exists. Both are set as RESEARCH_* environment variables for
this process, so they reach research and nothing else.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow, tags

from flow.agents.analysis import analysis_agent
from flow.llm import MULTI_AGENT_MODEL, count_calls, format_call_stats
from flow.agents.drafting import drafting_agent
from flow.agents.import_contacts import import_contacts
from flow.agents.research import research_agent
from flow.jina_crawl import jina_crawl
from flow.mx_check import verify_business_emails
from flow.outreach_state import claim_businesses, record_outcome
from flow.agents.selector import (
    BATCH_PER_WORKER,
    DEFAULT_CONCURRENCY,
    needs_email_verification,
    needs_jina_crawl,
    DEFAULT_TIERS,
    fetch_candidates,
)


async def run_preflight(
    candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Fill the gaps the selector found, before any LLM call is made.

    Two gaps are worth closing first because research and drafting both
    reason about them:

      * emails with no verification verdict go through mx_check, so the
        agents are not choosing between addresses of unknown deliverability
      * businesses with no Jina-crawled page get crawled, so research has
        that text as context

    Both are best-effort. A verification or crawl outage degrades the quality
    of the run; it must not cost the batch, so each failure is reported and
    the pipeline continues with whatever data exists.

    Returns the candidates, re-fetched if anything ran -- mx_check writes
    verdicts and jina_crawl writes pages, and the in-memory candidates
    predate both.
    """
    unverified = [c['business']['id'] for c in candidates
                  if needs_email_verification(c)]
    uncrawled = [c['business']['id'] for c in candidates
                 if needs_jina_crawl(c)]

    if unverified:
        print(f'preflight: verifying emails for {len(unverified)} business(es)')
        try:
            await verify_business_emails(business_ids=unverified)
        except Exception as exc:
            print(f'preflight: mx_check failed, continuing without verdicts: '
                  f'{type(exc).__name__}: {exc}')

    if uncrawled:
        print(f'preflight: jina crawling {len(uncrawled)} business(es)')
        try:
            await jina_crawl(business_ids=uncrawled)
        except Exception as exc:
            print(f'preflight: jina_crawl failed, continuing without those '
                  f'pages: {type(exc).__name__}: {exc}')

    if not (unverified or uncrawled):
        return candidates

    ids = [c['business']['id'] for c in candidates]
    print(f'preflight: re-fetching {len(ids)} candidate(s) to pick up the '
          f'new verdicts and pages')
    refreshed = await fetch_candidates(include_ids=ids)
    return refreshed or candidates


@flow(log_prints=True)
async def run_agents(
    tiers: Optional[List[str]] = None,
    batch_size: Optional[int] = None,
    business_ids: Optional[List[str]] = None,
    template_slug: str = 'default',
    offer: Optional[str] = None,
    preflight: bool = True,
    concurrency: Optional[int] = None,
) -> dict:
    """
    Select businesses, research each, draft for the ones that pass, analyze
    each draft, and finally import each drafted business to Warmbly as contacts.

    Up to `concurrency` businesses (default DEFAULT_CONCURRENCY) go through
    that chain at once, each on its own worker thread.

    Imported contacts are not assigned to a campaign: that is the human review
    gate, so nothing sends until someone assigns them.

    The result's llm_calls counts every proxy call the batch made, by provider
    and by model, failures and tokens included -- see flow.llm.count_calls.
    """
    concurrency = max(1, concurrency or DEFAULT_CONCURRENCY)
    with count_calls() as llm_calls:
        result = await _run_batch(tiers, batch_size, business_ids,
                                  template_slug, offer, preflight, concurrency)
    print(format_call_stats(llm_calls))
    return {**result, 'concurrency': concurrency, 'llm_calls': llm_calls}


async def _run_batch(
    tiers: Optional[List[str]],
    batch_size: Optional[int],
    business_ids: Optional[List[str]],
    template_slug: str,
    offer: Optional[str],
    preflight: bool,
    concurrency: int,
) -> dict:
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
        # Sized to the workers, not fixed: a default batch is about an hour.
        batch_size = batch_size or concurrency * BATCH_PER_WORKER
        print(f'selecting up to {batch_size} from tiers {tiers}')
        candidates = await fetch_candidates(
            tiers=tiers, batch_size=batch_size, include_ids=None)
        not_found = []

    # Claim before any work, so a concurrent run cannot take the same
    # businesses. Named businesses are claimed whatever their state.
    selected_ids = [c['business']['id'] for c in candidates]
    claimed = set(await claim_businesses(selected_ids, _flow_run_id(),
                                         force=bool(business_ids)))
    if len(claimed) < len(selected_ids):
        print(f'{len(selected_ids) - len(claimed)} business(es) already claimed by '
              f'another run; skipped')
    candidates = [c for c in candidates if c['business']['id'] in claimed]

    print(f'selected {len(candidates)}; running {min(concurrency, len(candidates) or 1)} at a time')

    if preflight and candidates:
        candidates = await run_preflight(candidates)

    # The agents are sync flows; each business's chain runs on a worker
    # thread. asyncio.to_thread copies the context, so the parent flow run,
    # the per-business tags() and the count_calls() tally all carry over.
    # The semaphore is the number of businesses in flight -- and so, since a
    # business makes its model calls one after another, the number of
    # concurrent model calls the batch puts on the proxy.
    slots = asyncio.Semaphore(concurrency)

    async def worker(candidate: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        async with slots:
            try:
                result = await asyncio.to_thread(
                    _process_business, candidate, template_slug, offer)
            except Exception as exc:
                # _process_business handles each stage's failures; this is
                # for a bug in it, which must still cost only this business.
                business = candidate['business']
                print(f"worker errored for {business.get('business_name')}: "
                      f"{type(exc).__name__}: {exc}")
                result = ({'business_id': business.get('id'),
                           'business_name': business.get('business_name'),
                           'research_status': 'error', 'draft_status': None,
                           'error': f'{type(exc).__name__}: {exc}'}, ['errored'])
        # Recorded as each business finishes, so a batch that dies part way
        # leaves an accurate record of what it got through.
        await record_outcome(result[0])
        return result

    # gather returns in candidate order, and the tallies are applied here on
    # the event loop, so no thread ever touches `counts` or `outcomes`.
    results = await asyncio.gather(*(worker(c) for c in candidates))

    outcomes: List[Dict[str, Any]] = []
    counts = {'researched': 0, 'rejected': 0, 'drafted': 0, 'imported': 0,
              'import_failed': 0, 'analyzed': 0, 'errored': 0}
    for outcome, tallies in results:
        outcomes.append(outcome)
        for key in tallies:
            counts[key] += 1

    print(f"selected={len(candidates)} researched={counts['researched']} "
          f"rejected={counts['rejected']} drafted={counts['drafted']} "
          f"imported={counts['imported']} import_failed={counts['import_failed']} "
          f"analyzed={counts['analyzed']} errored={counts['errored']}")

    return {
        'selected': len(candidates),
        'not_found': not_found,
        'outcomes': outcomes,
        **counts,
    }


def _flow_run_id() -> Optional[str]:
    """This run's Prefect id, for the claim record; None outside a flow run."""
    try:
        from prefect.runtime import flow_run
        return str(flow_run.id) if flow_run.id else None
    except Exception:
        return None


def _process_business(
    candidate: Dict[str, Any],
    template_slug: str,
    offer: Optional[str],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    One business through research, drafting, analysis and import.

    Runs on a worker thread. Returns its outcome and the names of the
    counters it adds to; it touches no shared state.
    """
    business = candidate['business']
    emails = candidate['candidate_emails']
    tallies: List[str] = []
    outcome: Dict[str, Any] = {
        'business_id': business.get('id'),
        'business_name': business.get('business_name'),
        'research_status': None,
        'draft_status': None,
    }

    # The subflows name their artifacts from the ambient flow run tags
    # (flow/artifacts.py: artifact_filename). Untagged, every stage of
    # every business writes to 'unknown-input.json'/'unknown-output.json'
    # and overwrites whatever ran before it, so a 50-business batch leaves
    # two files behind. Tagging the call site -- which is what Prefect
    # reads, not the callee -- gives each one
    # '{business_id}-{stage}-{input|output}.json'.
    stage_tags = (str(business.get('id') or 'unknown'),)

    try:
        # Subflow: its own flow run, retryable on its own. The crawl text
        # goes through whole -- no truncation anywhere in this path.
        with tags(*stage_tags, 'research'):
            researched = research_agent(
                business, emails, candidate['crawl_excerpt'], offer)
    except Exception as exc:
        # One bad business must not cost the other 49 in the batch.
        print(f"research errored for {business.get('business_name')}: "
              f"{type(exc).__name__}: {exc}")
        outcome['research_status'] = 'error'
        outcome['error'] = f'{type(exc).__name__}: {exc}'
        return outcome, ['errored']

    outcome['research_status'] = researched['status']

    if researched['status'] != 'researched':
        outcome['rejection_reason'] = researched.get('reason')
        return outcome, ['rejected']

    tallies.append('researched')
    # Research drops pool addresses the site no longer shows; drafting
    # and import must not resurrect them from the original pool.
    emails = researched.get('verified_emails', emails)

    try:
        with tags(*stage_tags, 'drafting'):
            drafted = drafting_agent(
                researched['research'], emails, template_slug, offer,
                website=business.get('website'),
                business_id=business.get('id'))
    except Exception as exc:
        print(f"drafting errored for {business.get('business_name')}: "
              f"{type(exc).__name__}: {exc}")
        outcome['draft_status'] = 'error'
        outcome['error'] = f'{type(exc).__name__}: {exc}'
        return outcome, tallies + ['errored']

    outcome['draft_status'] = drafted['status']
    if drafted.get('reason'):
        outcome['draft_reason'] = drafted['reason']
    outcome['analysis_status'] = None
    if drafted['status'] != 'drafted':
        return outcome, tallies

    tallies.append('drafted')
    outcome['draft'] = drafted['draft']
    outcome['outside_known_pool'] = drafted.get('outside_known_pool', [])

    # Analyze before import, so the import is the last thing a
    # business does. Analysis is a quality report, not a gate: a slow
    # or failing LLM here is recorded and the import still runs. The
    # contact lands unassigned to any campaign.
    try:
        with tags(*stage_tags, 'analysis'):
            analyzed = analysis_agent(
                research=researched['research'],
                draft=drafted['draft'],
                business=business
            )
    except Exception as exc:
        print(f"analysis errored for {business.get('business_name')}: "
              f"{type(exc).__name__}: {exc}")
        outcome['analysis_status'] = 'error'
        outcome['analysis_error'] = f'{type(exc).__name__}: {exc}'
    else:
        outcome['analysis_status'] = analyzed.get('status')
        if analyzed.get('status') == 'analyzed':
            tallies.append('analyzed')
            outcome['analysis'] = analyzed.get('analysis')

    try:
        with tags(*stage_tags, 'import'):
            imported = import_contacts(
                business=business,
                research=researched['research'],
                draft=drafted['draft'],
                candidate_emails=emails,
            )
    except Exception as exc:
        print(f"import errored for {business.get('business_name')}: "
              f"{type(exc).__name__}: {exc}")
        outcome['import_status'] = 'error'
        outcome['import_error'] = f'{type(exc).__name__}: {exc}'
        tallies.append('import_failed')
    else:
        outcome['import_status'] = imported.get('status')
        if imported.get('status') == 'imported':
            tallies.append('imported')
            created = imported.get('created') or []
            outcome['contacts_created'] = len(created)
            outcome['warmbly_contact_ids'] = [c.get('id') for c in created if c.get('id')]
            outcome['warmbly_draft_ids'] = list(imported.get('drafts') or [])
        else:
            tallies.append('import_failed')
            outcome['import_reason'] = imported.get('reason')

    return outcome, tallies


EFFORTS = ['low', 'medium', 'high', 'none']


def _usage_rows() -> List[Tuple[str, str, str]]:
    from flow.multi_agent import TIERS
    return [
        ('--concurrency N', f'{DEFAULT_CONCURRENCY} (RUN_AGENTS_CONCURRENCY)',
         'Businesses in flight at once'),
        ('--batch-size N', f'concurrency x {BATCH_PER_WORKER}',
         'Businesses to select'),
        ('--tiers TIER ...', ' '.join(f'"{t}"' for t in DEFAULT_TIERS),
         'ICP tiers to select from'),
        ('--business-ids ID ...', '-',
         'Run exactly these; skips selection and claims them even if already processed'),
        ('--template-slug SLUG', 'default', 'Drafting template'),
        ('--offer TEXT', '-', 'Offer text passed to the drafter'),
        ('--no-preflight', 'off', 'Skip the MX check and the Jina crawl'),
        ('--research-reasoning-effort', 'high',
         f"Research answering-call effort: {', '.join(EFFORTS)}"),
        ('--research-tier', '-',
         f"Pin research to one multi-agent tier ({', '.join(TIERS)}); "
         'implies RESEARCH_MODEL=multi-agent'),
    ]


def print_usage(file=None) -> None:
    """The options table, printed for --help and under any command-line error."""
    file = file or sys.stdout
    rows = [('Option', 'Default', 'What it does')] + _usage_rows()
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    line = lambda r: '| ' + ' | '.join(c.ljust(w) for c, w in zip(r, widths)) + ' |'
    print('usage: python flow/run_agents.py [options]\n', file=file)
    print(line(rows[0]), file=file)
    print('|' + '|'.join('-' * (w + 2) for w in widths) + '|', file=file)
    for row in rows[1:]:
        print(line(row), file=file)


class _UsageParser(argparse.ArgumentParser):
    """argparse with its help and error output replaced by print_usage."""

    def print_help(self, file=None):
        print_usage(file)

    def error(self, message):
        print(f'{self.prog}: error: {message}\n', file=sys.stderr)
        print_usage(sys.stderr)
        self.exit(2)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f'must be at least 1, got {number}')
    return number


def build_parser() -> argparse.ArgumentParser:
    from flow.multi_agent import TIERS

    parser = _UsageParser(
        prog='run_agents', description='Research, draft, analyze and import a batch.')
    parser.add_argument('--batch-size', type=_positive_int,
                        help=f'businesses to select (default: {BATCH_PER_WORKER} per worker)')
    parser.add_argument('--tiers', nargs='+', metavar='TIER',
                        help=f'ICP tiers to select from (default: {DEFAULT_TIERS})')
    parser.add_argument('--business-ids', nargs='+', metavar='ID',
                        help='research exactly these businesses')
    parser.add_argument('--template-slug', default='default')
    parser.add_argument('--offer')
    parser.add_argument('--concurrency', type=_positive_int,
                        help=f'businesses in flight at once (default: {DEFAULT_CONCURRENCY})')
    parser.add_argument('--no-preflight', action='store_true',
                        help='skip mx_check and the Jina crawl')
    parser.add_argument('--research-reasoning-effort', default='high',
                        choices=EFFORTS,
                        help="reasoning effort for research's answering calls "
                             "(default: high, the strongest every model honors; "
                             "'none' sends none)")
    parser.add_argument('--research-tier', choices=TIERS,
                        help='answer research with this multi-agent tier, skipping '
                             'the router; implies RESEARCH_MODEL=multi-agent')
    return parser


def main(argv: Optional[List[str]] = None) -> dict:
    """Run one batch in this process. See the module docstring, or --help."""
    args = build_parser().parse_args(argv)

    if args.research_reasoning_effort == 'none':
        os.environ.pop('RESEARCH_REASONING_EFFORT', None)
    else:
        os.environ['RESEARCH_REASONING_EFFORT'] = args.research_reasoning_effort
    if args.research_tier:
        os.environ['RESEARCH_TIER'] = args.research_tier
        os.environ['RESEARCH_MODEL'] = MULTI_AGENT_MODEL

    print(f"research: model={os.environ.get('RESEARCH_MODEL') or os.environ.get('LLM_MODEL')} "
          f"tier={os.environ.get('RESEARCH_TIER') or 'router'} "
          f"effort={os.environ.get('RESEARCH_REASONING_EFFORT') or 'default'}")

    return asyncio.run(run_agents(
        tiers=args.tiers,
        batch_size=args.batch_size,
        business_ids=args.business_ids,
        template_slug=args.template_slug,
        offer=args.offer,
        preflight=not args.no_preflight,
        concurrency=args.concurrency,
    ))


if __name__ == '__main__':
    print(json.dumps(main(), indent=1, default=str))
