"""
SMTP-level email verification as a Prefect task, writing leads.email_verification.

Wraps scripts/mxCheck.pl. Import it from any flow:

    from flow.mx_check import verify_business_emails

    summary = await verify_business_emails(business_ids=[...])
    summary = await verify_business_emails(emails=['owner@example.com'])
    summary = await verify_business_emails(unverified_limit=200)

Addresses are piped to the script on stdin (`--file /dev/stdin`); no temp file
is written. Each address is checked once, and the result is written to every
leads.business_email row that holds it -- the same address often belongs to
several businesses -- as an upsert on business_email_id, so a re-check updates
the row and its verified_at instead of adding another.

A row with no result (the script dropped it) is not written: recording
verified=false would claim a check that never happened.

Port 25 must be reachable outbound from wherever this runs. The scraper hosts
block it; see the SOCKS5 notes in scripts/mxCheck.pl.

Environment:
  LEADS_DB_URL         Postgres DSN (required)
  MXCHECK_THREADS      concurrent SMTP checks (default 10)
  MXCHECK_RATE_LIMIT   checks started per second, 0 = unthrottled (default 0)
  MXCHECK_TIMEOUT      seconds before the script is killed (default 600)
  MXCHECK_FORCE_CHECK  1 to pass --force-check (no reverse DNS on this host)
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from prefect import task

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'mxCheck.pl'
PERL = os.environ.get('MXCHECK_PERL') or 'perl'
DEFAULT_THREADS = int(os.environ.get('MXCHECK_THREADS', '10'))
DEFAULT_RATE_LIMIT = float(os.environ.get('MXCHECK_RATE_LIMIT', '0'))
DEFAULT_TIMEOUT = float(os.environ.get('MXCHECK_TIMEOUT', '600'))
DEFAULT_FORCE_CHECK = os.environ.get('MXCHECK_FORCE_CHECK', '').lower() in (
    '1', 'true', 'yes')

# Postgres text cannot hold NUL; SMTP banners and errors occasionally do.
_C0 = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

UPSERT_SQL = """
insert into leads.email_verification
    (business_email_id, business_id, email, verified, mx_server, error,
     verified_at)
values ($1, $2, $3, $4, $5, $6, now())
on conflict (business_email_id) do update set
    business_id = excluded.business_id,
    email = excluded.email,
    verified = excluded.verified,
    mx_server = excluded.mx_server,
    error = excluded.error,
    verified_at = now()
"""


class MxCheckError(RuntimeError):
    """mxCheck.pl failed, timed out, or printed something that is not JSON."""


def _normalise(emails: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for e in emails:
        e = (e or '').strip().lower()
        if e:
            seen.setdefault(e, None)
    return list(seen)


def _clean(text: Optional[str]) -> Optional[str]:
    return _C0.sub('', text) if isinstance(text, str) else text


async def run_mxcheck(
    emails: Iterable[str],
    threads: int = DEFAULT_THREADS,
    rate_limit: float = DEFAULT_RATE_LIMIT,
    force_check: bool = DEFAULT_FORCE_CHECK,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Run mxCheck.pl over `emails`; return its results ({email, verified, ...})."""
    addresses = _normalise(emails)
    if not addresses:
        return []

    argv = [PERL, str(SCRIPT), '--file', '/dev/stdin',
            '--threads', str(threads)]
    if rate_limit:
        argv += ['--rate-limit', f'{rate_limit:g}']
    if force_check:
        argv.append('--force-check')

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = ('\n'.join(addresses) + '\n').encode()
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=payload), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MxCheckError(
            f'mxCheck.pl timed out after {timeout:g}s on {len(addresses)} '
            f'address(es)')

    if proc.returncode != 0:
        raise MxCheckError(
            f'mxCheck.pl exited {proc.returncode}: '
            f'{err.decode(errors="replace").strip()[:500]}')
    try:
        results = json.loads(out)
    except json.JSONDecodeError as exc:
        raise MxCheckError(
            f'mxCheck.pl output is not JSON ({exc}): '
            f'{out.decode(errors="replace")[:200]!r}') from exc
    if not isinstance(results, list):
        raise MxCheckError(f'mxCheck.pl returned {type(results).__name__}, '
                           f'expected a list')
    return results


def match_results(
    rows: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One record per business_email row whose address has a result."""
    by_email = {(r.get('email') or '').strip().lower(): r for r in results}
    records = []
    for row in rows:
        result = by_email.get((row['email'] or '').strip().lower())
        if result is None:
            continue
        records.append({
            'business_email_id': row['id'],
            'business_id': row['business_id'],
            'email': row['email'],
            'verified': bool(result.get('verified')),
            'mx_server': _clean(result.get('mx_server')),
            'error': _clean(result.get('error')),
        })
    return records


async def upsert_verifications(conn: Any, records: List[Dict[str, Any]]) -> int:
    if not records:
        return 0
    await conn.executemany(UPSERT_SQL, [
        (r['business_email_id'], r['business_id'], r['email'], r['verified'],
         r['mx_server'], r['error'])
        for r in records
    ])
    return len(records)


async def load_business_emails(
    conn: Any,
    emails: Optional[List[str]] = None,
    business_ids: Optional[List[str]] = None,
    unverified_limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """business_email rows selected by address, by business, or not yet checked."""
    chosen = [x is not None for x in (emails, business_ids, unverified_limit)]
    if sum(chosen) != 1:
        raise ValueError(
            'pass exactly one of emails, business_ids or unverified_limit')

    if emails is not None:
        rows = await conn.fetch(
            'select id, business_id, email from leads.business_email '
            'where lower(email) = any($1::text[])',
            _normalise(emails))
    elif business_ids is not None:
        rows = await conn.fetch(
            'select id, business_id, email from leads.business_email '
            'where business_id = any($1::uuid[])',
            list(business_ids))
    else:
        rows = await conn.fetch(
            'select e.id, e.business_id, e.email from leads.business_email e '
            'where not exists (select 1 from leads.email_verification v '
            '                  where v.business_email_id = e.id) '
            'order by e.confidence desc nulls last, e.extracted_at '
            'limit $1',
            unverified_limit)
    return [dict(r) for r in rows]


def _dsn() -> str:
    url = os.environ.get('LEADS_DB_URL')
    if not url:
        raise RuntimeError('LEADS_DB_URL is not set')
    return url.replace('postgres://', 'postgresql://', 1)


async def _connect() -> Any:
    return await asyncpg.connect(_dsn())


@task(name='verify-business-emails', log_prints=True)
async def verify_business_emails(
    emails: Optional[List[str]] = None,
    business_ids: Optional[List[str]] = None,
    unverified_limit: Optional[int] = None,
    threads: int = DEFAULT_THREADS,
    rate_limit: float = DEFAULT_RATE_LIMIT,
    force_check: bool = DEFAULT_FORCE_CHECK,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, int]:
    """
    Verify business_email addresses with mxCheck.pl and upsert the results
    into leads.email_verification. Pass exactly one of emails, business_ids
    or unverified_limit.
    """
    conn = await _connect()
    try:
        rows = await load_business_emails(
            conn, emails=emails, business_ids=business_ids,
            unverified_limit=unverified_limit)
        addresses = _normalise(r['email'] for r in rows)
        if not addresses:
            print('no business_email rows selected')
            return {'addresses': 0, 'rows': 0, 'written': 0,
                    'verified': 0, 'failed': 0, 'unchecked': 0}

        print(f'checking {len(addresses)} address(es) '
              f'across {len(rows)} business_email row(s)')
        results = await run_mxcheck(
            addresses, threads=threads, rate_limit=rate_limit,
            force_check=force_check, timeout=timeout)
        records = match_results(rows, results)
        written = await upsert_verifications(conn, records)
    finally:
        await conn.close()

    checked = {(r.get('email') or '').strip().lower(): bool(r.get('verified'))
               for r in results}
    summary = {
        'addresses': len(addresses),
        'rows': len(rows),
        'written': written,
        'verified': sum(1 for a in addresses if checked.get(a) is True),
        'failed': sum(1 for a in addresses if checked.get(a) is False),
        'unchecked': sum(1 for a in addresses if a not in checked),
    }
    print(f'mxCheck: {summary}')
    return summary
