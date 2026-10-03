"""
The candidate-selector deployment (spec stage 1).

Pulls a batch of businesses from leads.businesses that are worth researching:
they have an ICP tier we care about, at least one candidate email address, and
crawled site text for the research agent to quote from.

Deliberately says "candidate" and not "verified" throughout.
leads.email_verification is empty -- nothing in this database has been
MX-checked yet. Verification is stage 2's job, and Warmbly refuses to send to
anything it has not itself verified, so claiming verification here would be a
lie the research prompt would repeat.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import asyncpg
from prefect import flow

DEFAULT_TIERS = ['Tier 1', 'Tier 2']
DEFAULT_BATCH_SIZE = 50

# No cap on crawl text, deliberately and by instruction. The research agent
# reads everything available: a hook invented from half a page is worthless,
# and a wasted research pass costs the whole pipeline run -- selection, two LLM
# calls and a human review -- not just the input tokens it saved. There is no
# environment variable offering a cap either, because offering the knob implies
# the trade is reasonable.


def build_candidate_query(
    tiers: List[str],
    batch_size: int,
    exclude_ids: Optional[List[str]],
) -> tuple:
    """
    Build the selection SQL and its bound parameters.

    Returns (sql, params). Everything variable is a bound parameter: a batch
    size or id list spliced into the text would be an injection vector.
    """
    params: List[Any] = [tiers, batch_size]
    exclusion = ''

    if exclude_ids:
        params.append([str(i) for i in exclude_ids])
        exclusion = f'  and b.id::text != all(${len(params)})\n'

    sql = f"""
        select
            b.id::text as id, b.name, b.website, b.domain,
            b.city, b.state, b.icp_score, b.icp_tier,
            b.primary_category, b.service_category, b.description,
            b.rating, b.review_count,
            (
                select json_agg(json_build_object(
                    'email', e.email, 'source', e.source,
                    'confidence', e.confidence, 'is_role', e.is_role))
                from leads.business_email e
                where e.business_id = b.id
            ) as emails,
            (
                -- Every crawled page, not the longest one: the homepage plus
                -- about/services/contact are all context, and picking one
                -- discarded the rest.
                select json_agg(json_build_object(
                           'url', w.url, 'page_kind', w.page_kind,
                           'text', w.text_excerpt)
                       order by w.page_kind, w.fetched_at)
                from leads.website_crawl w
                where w.business_id = b.id
                  and w.text_excerpt is not null and w.text_excerpt <> ''
            ) as crawl_pages
        from leads.businesses b
        where b.icp_tier = any($1)
{exclusion}          and exists (
                select 1 from leads.business_email e
                where e.business_id = b.id
            )
          and exists (
                select 1 from leads.website_crawl w
                where w.business_id = b.id
                  and w.text_excerpt is not null and w.text_excerpt <> ''
            )
        order by b.icp_score desc nulls last, b.review_count desc nulls last
        limit $2
    """
    return sql, params


def join_crawl_pages(pages: Optional[List[Dict[str, Any]]]) -> str:
    """
    Concatenate every crawled page into one context block, untruncated.

    Each page is labelled with its url and kind so the model can attribute the
    evidence it quotes to a specific page.
    """
    if not pages:
        return ''

    blocks = []
    for page in pages:
        text = (page.get('text') or '').strip()
        if not text:
            continue
        blocks.append(
            f"--- {page.get('page_kind') or 'page'}: {page.get('url')} ---\n"
            f"{text}"
        )
    return '\n\n'.join(blocks)


def shape_candidate(row: Any) -> Dict[str, Any]:
    """
    Turn one selected row into the shape the research agent consumes.

    The `business` sub-dict uses `business_name`, matching what
    build_research_prompt reads, so a caller can hand it straight through.
    """
    emails = row['emails'] or []
    pages = row.get('crawl_pages') or []

    return {
        'business': {
            'id': row['id'],
            'business_name': row['name'],
            'website': row['website'],
            'domain': row['domain'],
            'city': row['city'],
            'state': row['state'],
            'icp_score': row['icp_score'],
            'icp_tier': row['icp_tier'],
            'primary_category': row['primary_category'],
            'service_category': row['service_category'],
            'description': row['description'],
            'rating': float(row['rating']) if row['rating'] is not None else None,
            'review_count': row['review_count'],
        },
        'candidate_emails': list(emails),
        'crawl_pages': list(pages),
        'crawl_excerpt': join_crawl_pages(pages),
    }


async def fetch_candidates(
    tiers: List[str],
    batch_size: int,
    exclude_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Run the selection query and shape the rows. Needs a live database."""
    url = os.environ.get('LEADS_DB_URL')
    if not url:
        raise RuntimeError('LEADS_DB_URL is not set')

    sql, params = build_candidate_query(tiers, batch_size, exclude_ids)
    conn = await asyncpg.connect(url.replace('postgres://', 'postgresql://'))
    try:
        rows = await conn.fetch(sql, *params)
    finally:
        await conn.close()

    # json_agg comes back as a JSON string over asyncpg; decode it here rather
    # than making every caller know that.
    import json
    shaped = []
    for r in rows:
        d = dict(r)
        for key in ('emails', 'crawl_pages'):
            if isinstance(d.get(key), str):
                d[key] = json.loads(d[key])
        shaped.append(shape_candidate(d))
    return shaped


@flow(log_prints=True)
async def candidate_selector(
    tiers: Optional[List[str]] = None,
    batch_size: Optional[int] = None,
    exclude_ids: Optional[List[str]] = None,
) -> dict:
    """Select a batch of businesses worth researching."""
    tiers = tiers or DEFAULT_TIERS
    batch_size = batch_size or DEFAULT_BATCH_SIZE

    print(f'selecting up to {batch_size} from tiers {tiers}')
    candidates = await fetch_candidates(tiers, batch_size, exclude_ids)

    print(f'selected {len(candidates)}')
    for c in candidates[:5]:
        b = c['business']
        print(f"  {b['icp_tier']} score={b['icp_score']} {b['business_name']} "
              f"({len(c['candidate_emails'])} emails, "
              f"{len(c['crawl_excerpt'])} chars of text)")

    return {'count': len(candidates), 'candidates': candidates}
