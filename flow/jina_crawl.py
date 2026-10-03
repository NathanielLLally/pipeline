"""
Crawls prospect websites through r.jina.ai and stores the result in
leads.website_crawl_jina.

The counterpart to scripts/enrich-websites.mjs, which fetches raw HTML with
curl through the Webshare SOCKS5 pool. That pass left 30,713 of 54,150
eligible businesses with no usable page text at all -- blocked, challenged,
JS-only or simply unreachable from a datacenter ASN. Jina runs a real browser
from its own addresses and returns rendered markdown, which is exactly the
class of site curl cannot get. So the DEFAULT target set here is that
shortfall, not the whole database: businesses with no usable text_excerpt, or
whose crawl rows carry an error or a non-2xx/3xx status.

Fetching is flow/fetch.py's fetch_html, unchanged, which is why this module
stays this small. It is a Prefect flow for the same reason the rest of flow/
is: retries, logging and a deployment come free.

Why a separate table: see db/migrations/013_website_crawl_jina.sql. Jina's
markdown and the mjs crawler's tag-stripped HTML are not the same kind of
text, and merging them would quietly change what every downstream reader is
quoting from.

Usage:
  python flow/jina_crawl.py --limit 20 --dry-run
  python flow/jina_crawl.py --limit 500 --concurrency 8
  python flow/jina_crawl.py --tier "Tier 1,Tier 2" --service-category dog_training
  python flow/jina_crawl.py --mode all          # every eligible business
  python flow/jina_crawl.py --refetch           # ignore this crawler's own rows
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from prefect import flow

from flow.fetch import fetch_html

# Every row this crawler writes is a homepage. Sub-page discovery is
# enrich-websites.mjs's job (it reads hrefs out of the HTML it already has);
# here the point is reaching the site at all, and each extra page is another
# billed Jina request.
PAGE_KIND = 'home'

DEFAULT_LIMIT = 100
DEFAULT_CONCURRENCY = int(os.environ.get('JINA_CONCURRENCY', '6'))
FLUSH_EVERY = 50

# Carried over verbatim from buildWhere() in scripts/enrich-websites.mjs: a GBP
# "website" pointing at a Facebook page is not a site to crawl.
SOCIAL_RE = r'(facebook\.com|instagram\.com|nextdoor\.com|linktr\.ee)'

# Postgres text cannot store \u0000 -- jsonb rejects the escape and takes the
# whole transaction with it, which has silently discarded a completed crawl on
# this project once already. Newline and tab are kept: the markdown's structure
# is content.
C0_RE = re.compile(r'[\u0000-\u0008\u000b\u000c\u000e-\u001f]')

# A real business homepage has prose. Below this, a 200 is not a page: observed
# live, a GBP website pointing at an ad-click URL resolved through
# match.adsrvr.org and Jina rendered the tracker -- status 200, 37 characters,
# the redirect target echoed as the whole body. Stored as-is that reads
# downstream as usable page text. Set well under the ~10k characters the real
# pages in the same batch returned.
MIN_CONTENT_CHARS = 300


def build_target_query(
    limit: int,
    mode: str = 'gaps',
    tiers: Optional[List[str]] = None,
    service_categories: Optional[List[str]] = None,
    refetch: bool = False,
) -> tuple:
    """
    Build the selection SQL and its bound parameters.

    Returns (sql, params). Everything variable is a bound parameter.

    mode='gaps' (the default) selects what the mjs crawler failed to get:
    either no usable text at all, or a crawl row that errored or answered
    outside 2xx/3xx. mode='all' selects every eligible business, which is only
    useful for a deliberate side-by-side comparison of the two crawlers.

    Unless refetch is set, businesses this crawler has already got text for are
    excluded, so a re-run costs nothing for what it already has.
    """
    params: List[Any] = [limit]
    clauses: List[str] = []

    if mode != 'all':
        clauses.append("""          and (
                not exists (
                    select 1 from leads.website_crawl w
                    where w.business_id = b.id
                      and w.text_excerpt is not null and w.text_excerpt <> ''
                )
                or exists (
                    select 1 from leads.website_crawl w
                    where w.business_id = b.id
                      and (w.fetch_error is not null
                           or w.http_status is null
                           or w.http_status < 200
                           or w.http_status >= 400)
                )
            )""")

    if not refetch:
        clauses.append("""          and not exists (
                select 1 from leads.website_crawl_jina j
                where j.business_id = b.id
                  and j.text_excerpt is not null and j.text_excerpt <> ''
            )""")

    if tiers:
        params.append(list(tiers))
        clauses.append(f'          and b.icp_tier = any(${len(params)})')

    if service_categories:
        params.append(list(service_categories))
        clauses.append(f'          and b.service_category = any(${len(params)})')

    filters = ('\n' + '\n'.join(clauses)) if clauses else ''

    return f"""
        select b.id::text as id, b.name, b.website, b.icp_tier, b.icp_score
        from leads.businesses b
        where b.website is not null
          and b.website <> ''
          and b.website !~* '{SOCIAL_RE}'
          and b.qc_status <> 'REJECTED'{filters}
        order by b.icp_score desc nulls last, b.review_count desc nulls last
        limit $1
    """, params


def clean_text(text: Optional[str]) -> Optional[str]:
    """Strip the control characters Postgres cannot store. Newlines survive."""
    if text is None:
        return None
    return C0_RE.sub(' ', text)


def normalize_url(website: Optional[str]) -> Optional[str]:
    """
    Turn a stored `website` value into a fetchable absolute URL.

    GBP stores bare domains as often as full URLs. Returns None rather than
    raising when the value is not a URL at all, so one bad row cannot end a
    run.
    """
    if not website:
        return None
    raw = str(website).strip()
    if not raw:
        return None
    if not re.match(r'^https?://', raw, re.I):
        raw = f'https://{raw}'
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    # A hostname with no dot, or with whitespace in it, is not a domain.
    if not parsed.netloc or '.' not in parsed.netloc or ' ' in parsed.netloc:
        return None
    return urlunparse(parsed._replace(path=parsed.path or '/'))


def parse_jina_response(raw: str) -> Dict[str, Any]:
    """
    Turn one r.jina.ai response body into the fields the table stores.

    Three shapes have to be handled, and only the first is a crawled page:

      success    {"code":200,"data":{"content":"...","httpStatus":200,...}}
      error      {"code":422,"name":"AssertionFailureError","message":"..."}
      not JSON   a gateway's own HTML, when the request never reached Jina

    The status recorded is data.httpStatus -- the crawled SITE's status.
    Jina's own `code` is about the API call and would read as a successful page
    fetch for a site that answered 500.

    `content` is removed from the stored payload: it is the page text, which
    goes in text_excerpt, and keeping both doubles the table for nothing. The
    rest of the envelope is kept whole because `links` holds mailto: addresses
    the markdown body does not, and usage.tokens is the API budget.
    """
    try:
        body = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {'text_excerpt': None, 'payload': {}, 'final_url': None,
                'http_status': 0, 'fetch_error': f'unparseable response: {exc}'}

    data = body.get('data')
    if not isinstance(data, dict):
        message = body.get('message') or body.get('name') or 'no data in response'
        return {'text_excerpt': None, 'payload': {}, 'final_url': None,
                'http_status': body.get('code') or 0,
                'fetch_error': str(message)[:500]}

    payload = {k: v for k, v in data.items() if k != 'content'}
    content = (data.get('content') or '').strip()
    status = data.get('httpStatus') or body.get('code') or 0

    if not content:
        error = 'empty content'
    elif len(content) < MIN_CONTENT_CHARS:
        error = f'content too short ({len(content)} chars)'
    else:
        error = None

    return {
        'text_excerpt': content if error is None else None,
        'payload': payload,
        'final_url': data.get('url'),
        'http_status': status,
        'fetch_error': error,
    }


def shape_row(business: Dict[str, Any], url: str,
              parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    One leads.website_crawl_jina row.

    A failed fetch still produces a row. That is what lets a later pass tell
    "never tried" from "tried and the site is gone", which is the same contract
    website_crawl has.
    """
    return {
        'business_id': business['id'],
        'url': url,
        'final_url': parsed.get('final_url'),
        'page_kind': PAGE_KIND,
        'http_status': parsed.get('http_status') or 0,
        'fetch_error': clean_text(parsed.get('fetch_error')),
        'signals': parsed.get('payload') or {},
        'text_excerpt': clean_text(parsed.get('text_excerpt')),
    }


def extract_links(business: Dict[str, Any], url: str,
                  parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    One row per link in the Jina response.

    Links are keyed on the crawled page they came from, not on their targets.
    Only extracted when the crawl succeeded (text_excerpt is not None).
    """
    if not parsed.get('text_excerpt'):
        return []

    links_map = (parsed.get('payload') or {}).get('links') or {}
    if not isinstance(links_map, dict):
        return []

    rows = []
    for text, href in links_map.items():
        if not text or not href:
            continue
        rows.append({
            'business_id': business['id'],
            'url': url,
            'link_text': clean_text(str(text)) or text,
            'link_href': str(href),
        })
    return rows


def build_link_insert(rows: List[Dict[str, Any]]) -> tuple:
    """
    Build the upsert for links, or return empty strings if there are no rows.

    Like build_insert, rows travel as a single JSON parameter.
    """
    if not rows:
        return '', []

    sql = """
        insert into leads.website_crawl_jina_link
            (business_id, url, link_text, link_href)
        select (r->>'business_id')::uuid, r->>'url', r->>'link_text', r->>'link_href'
        from jsonb_array_elements($1::jsonb) r
        on conflict (business_id, url, link_href) do nothing
    """
    return sql, [json.dumps(rows)]


def build_insert(rows: List[Dict[str, Any]]) -> tuple:
    """
    Build the upsert and its one bound parameter.

    The batch travels as a single json parameter rather than as interpolated
    literals: page text spliced into SQL text is both an injection vector and
    the NUL hazard that jsonLiteral() in enrich-websites.mjs exists to work
    around. asyncpg binds it, so neither applies.

    Rows are deduplicated on (business_id, url) because Postgres refuses to
    update the same row twice in one statement -- "ON CONFLICT DO UPDATE cannot
    affect row a second time" aborts the whole batch.
    """
    seen: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        seen[(row['business_id'], row['url'])] = row

    sql = """
        insert into leads.website_crawl_jina
            (business_id, url, final_url, page_kind, http_status,
             fetch_error, signals, text_excerpt)
        select (r->>'business_id')::uuid, r->>'url', r->>'final_url',
               r->>'page_kind', (r->>'http_status')::int, r->>'fetch_error',
               coalesce(r->'signals', '{}'::jsonb), r->>'text_excerpt'
        from jsonb_array_elements($1::jsonb) r
        on conflict (business_id, url) do update set
            final_url    = excluded.final_url,
            page_kind    = excluded.page_kind,
            http_status  = excluded.http_status,
            fetch_error  = excluded.fetch_error,
            signals      = excluded.signals,
            text_excerpt = excluded.text_excerpt,
            fetched_at   = now()
    """
    return sql, [json.dumps(list(seen.values()))]


def _dsn() -> str:
    url = os.environ.get('LEADS_DB_URL')
    if not url:
        raise RuntimeError('LEADS_DB_URL is not set')
    return url.replace('postgres://', 'postgresql://')


async def select_targets(**kwargs) -> List[Dict[str, Any]]:
    """Run the selection query. Needs a live database."""
    sql, params = build_target_query(**kwargs)
    conn = await asyncpg.connect(_dsn())
    try:
        return [dict(r) for r in await conn.fetch(sql, *params)]
    finally:
        await conn.close()


async def persist(rows: List[Dict[str, Any]],
                  links: List[Dict[str, Any]] = None) -> tuple:
    """Write one batch of crawls and links. Returns (crawls_written, links_written)."""
    if not rows and not links:
        return 0, 0

    crawls_written = 0
    links_written = 0

    conn = await asyncpg.connect(_dsn())
    try:
        if rows:
            sql, params = build_insert(rows)
            await conn.execute(sql, *params)
            crawls_written = len(json.loads(params[0]))

        if links:
            sql, params = build_link_insert(links)
            if sql:  # build_link_insert may return empty string
                await conn.execute(sql, *params)
                links_written = len(json.loads(params[0]))
    finally:
        await conn.close()

    return crawls_written, links_written


@flow(log_prints=True)
async def jina_crawl(
    limit: int = DEFAULT_LIMIT,
    mode: str = 'gaps',
    tiers: Optional[List[str]] = None,
    service_categories: Optional[List[str]] = None,
    refetch: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
) -> dict:
    """Crawl business homepages through Jina and store the pages."""
    targets = await select_targets(limit=limit, mode=mode, tiers=tiers,
                                   service_categories=service_categories,
                                   refetch=refetch)
    print(f'{len(targets)} businesses selected (mode={mode}, limit={limit})')
    if not targets:
        return {'selected': 0, 'reachable': 0, 'failed': 0, 'written': 0}

    stats = {'selected': len(targets), 'reachable': 0, 'failed': 0,
             'skipped': 0, 'crawls_written': 0, 'links_written': 0, 'tokens': 0}
    pending_crawls: List[Dict[str, Any]] = []
    pending_links: List[Dict[str, Any]] = []
    gate = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def flush() -> None:
        nonlocal pending_crawls, pending_links
        crawls, pending_crawls = pending_crawls, []
        links, pending_links = pending_links, []
        if not crawls and not links:
            return
        try:
            crawls_w, links_w = await persist(crawls, links)
            stats['crawls_written'] += crawls_w
            stats['links_written'] += links_w
        except Exception as exc:
            # A bad batch costs one chunk, never the run: the table is keyed on
            # (business_id, url), so a re-run picks up from what landed.
            print(f'batch of {len(crawls)} crawls, {len(links)} links failed: '
                  f'{type(exc).__name__}: {exc}')

    async def one(business: Dict[str, Any]) -> None:
        url = normalize_url(business.get('website'))
        if not url:
            stats['skipped'] += 1
            print(f"skipped {business['name']}: unparseable website "
                  f"{business.get('website')!r}")
            return

        async with gate:
            try:
                # fetch_html is a sync Prefect task (requests); keep the event
                # loop free while it blocks.
                raw = await asyncio.to_thread(fetch_html, url)
                parsed = parse_jina_response(raw)
            except Exception as exc:
                parsed = {'text_excerpt': None, 'payload': {}, 'final_url': None,
                          'http_status': 0,
                          'fetch_error': f'{type(exc).__name__}: {exc}'[:500]}

        row = shape_row(business, url, parsed)
        if row['text_excerpt']:
            stats['reachable'] += 1
            stats['tokens'] += ((row['signals'].get('usage') or {})
                                .get('tokens') or 0)
        else:
            stats['failed'] += 1
            print(f"no content for {business['name']} ({url}): "
                  f"{row['fetch_error']}")

        if dry_run:
            return

        links = extract_links(business, url, parsed)

        async with lock:
            pending_crawls.append(row)
            pending_links.extend(links)
            if len(pending_crawls) >= FLUSH_EVERY:
                await flush()

    await asyncio.gather(*(one(b) for b in targets))
    if not dry_run:
        async with lock:
            await flush()

    print(f"reachable={stats['reachable']} failed={stats['failed']} "
          f"skipped={stats['skipped']} crawls_written={stats['crawls_written']} "
          f"links_written={stats['links_written']} jina_tokens={stats['tokens']}"
          + ('  (DRY RUN -- nothing written)' if dry_run else ''))
    return stats


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    p.add_argument('--mode', choices=['gaps', 'all'], default='gaps',
                   help="'gaps' (default): what enrich-websites.mjs could not "
                        "get. 'all': every eligible business.")
    p.add_argument('--tier', help='comma-separated, e.g. "Tier 1,Tier 2"')
    p.add_argument('--service-category', help='comma-separated')
    p.add_argument('--refetch', action='store_true',
                   help="ignore this crawler's own prior rows")
    p.add_argument('--concurrency', type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument('--dry-run', action='store_true')
    return p.parse_args(argv)


def _split(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [part.strip() for part in value.split(',') if part.strip()]


if __name__ == '__main__':
    args = _parse_args()
    asyncio.run(jina_crawl(
        limit=args.limit,
        mode=args.mode,
        tiers=_split(args.tier),
        service_categories=_split(args.service_category),
        refetch=args.refetch,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
    ))
