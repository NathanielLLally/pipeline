"""
Write the person research found back to leads.businesses.

The research node extracts contact_name/contact_title from the live pages,
and that is the better source: decision_maker_name is populated for only 16%
of Tier 1 and 5% of Tier 2 businesses (measured 2026-10-06). Writing it back
means the next run -- and every other consumer of leads.businesses -- gets
the benefit, instead of the name existing only inside one flow run's output.

Kept in its own module so research_agent stays unit-testable without a
database: the tests patch _connect, not the agent.

Two existing column conventions are followed rather than invented over:
  * decision_maker_confidence is TEXT holding 'high'/'medium' (not a float)
  * decision_maker_source holds the URL a name came from (not a label like
    'research_agent'), so writing a label there would make the column mean
    two different things depending on who wrote the row
"""

import asyncio
import concurrent.futures
import os
from typing import Any, List, Optional, Tuple

import asyncpg

UPDATE_SQL = """
    update leads.businesses
       set decision_maker_name = $2,
           decision_maker_title = $3,
           decision_maker_confidence = $4,
           decision_maker_source = coalesce($5, decision_maker_source),
           date_updated = now()
     where id::text = $1
"""


def confidence_label(confidence: Optional[float]) -> str:
    """
    Map the research node's 0-1 confidence onto the column's vocabulary.

    The column is text and already holds 'high' and 'medium', so a float
    would be a third kind of value in a column that readers treat as an
    enum.
    """
    if confidence is None:
        return 'medium'
    if confidence >= 0.8:
        return 'high'
    if confidence >= 0.6:
        return 'medium'
    return 'low'


def build_decision_maker_update(
    business_id: str,
    name: str,
    title: Optional[str] = None,
    confidence: Optional[float] = None,
    source_url: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """The UPDATE and its bound parameters. Pure, so it is assertable."""
    return UPDATE_SQL, [
        str(business_id),
        name.strip(),
        (title or '').strip() or None,
        confidence_label(confidence),
        source_url,
    ]


def _dsn() -> str:
    dsn = os.environ.get('LEADS_DB_URL')
    if not dsn:
        raise RuntimeError('LEADS_DB_URL is not set')
    return dsn.replace('postgres://', 'postgresql://')


async def _connect() -> Any:
    return await asyncpg.connect(_dsn())


async def record_decision_maker(
    business_id: Optional[str],
    name: Optional[str],
    title: Optional[str] = None,
    confidence: Optional[float] = None,
    source_url: Optional[str] = None,
) -> bool:
    """
    Write the person back, returning whether anything was written.

    Never writes a blank name: an empty decision_maker_name is worse than
    leaving the earlier value alone, because it overwrites whatever a prior
    enrichment pass found with nothing. A database failure is reported and
    swallowed -- a failed writeback must not fail the research that produced
    it, since the research output is already safe in its artifact.
    """
    if not business_id or not name or not name.strip():
        return False

    sql, params = build_decision_maker_update(
        business_id, name, title, confidence, source_url)
    try:
        conn = await _connect()
    except Exception as exc:
        print(f'could not record decision maker: {type(exc).__name__}: {exc}')
        return False

    try:
        await conn.execute(sql, *params)
        print(f'recorded decision maker {name!r} for {business_id}')
        return True
    except Exception as exc:
        print(f'could not record decision maker: {type(exc).__name__}: {exc}')
        return False
    finally:
        try:
            await conn.close()
        except Exception:
            pass


def record_decision_maker_sync(
    business_id: Optional[str],
    name: Optional[str],
    title: Optional[str] = None,
    confidence: Optional[float] = None,
    source_url: Optional[str] = None,
) -> bool:
    """
    Call record_decision_maker from synchronous code, loop or no loop.

    research_agent is a sync flow and run_agents is an async one, so this is
    reached with an event loop already running on the thread. asyncio.run()
    refuses that ("cannot be called from a running event loop"), so the work
    goes to a one-shot worker thread with its own loop. With no loop running,
    asyncio.run() directly is fine and avoids the thread entirely.

    The cheap guard first: skipping a write needs no loop at all.
    """
    if not business_id or not name or not name.strip():
        return False

    def _go() -> bool:
        return asyncio.run(record_decision_maker(
            business_id, name, title, confidence, source_url))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _go()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_go).result()
