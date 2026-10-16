"""
leads.outreach_pipeline: the pipeline's memory of which businesses it has taken.

run_agents claims the businesses it selected before any model call, and
records each one's end state as it finishes. Selection
(flow/agents/selector.py) skips businesses with a row unless the row is
'failed' or a stale claim -- see db/migrations/015_outreach_pipeline.sql.

Everything here degrades to the old behaviour when the table does not exist
yet: claims grant every id and outcomes are not recorded. Tracking is
bookkeeping; a missing table or a failed write must never cost a batch.
"""

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncpg

TABLE = 'leads.outreach_pipeline'
# A claim older than this belongs to a run that died; another run may take it.
STALE_CLAIM = '6 hours'
# The model never answered (flow.schemas.failure_reason): retryable, unlike
# every other rejection.
UNAVAILABLE = 'llm_unavailable'

CLAIM_SQL = f"""
    insert into {TABLE} (business_id, status, flow_run_id)
    select unnest($1::uuid[]), 'claimed', $2
    on conflict (business_id) do update
        set status = 'claimed', claimed_at = now(), finished_at = null,
            reason = null, flow_run_id = excluded.flow_run_id, updated_at = now()
        where {TABLE}.status = 'failed'
           or ({TABLE}.status = 'claimed'
               and {TABLE}.claimed_at < now() - interval '{STALE_CLAIM}')
    returning business_id::text
"""

# Named businesses (run_agents(business_ids=...)): the caller decided, so the
# claim is taken whatever state the row is in.
FORCE_CLAIM_SQL = f"""
    insert into {TABLE} (business_id, status, flow_run_id)
    select unnest($1::uuid[]), 'claimed', $2
    on conflict (business_id) do update
        set status = 'claimed', claimed_at = now(), finished_at = null,
            reason = null, flow_run_id = excluded.flow_run_id, updated_at = now()
    returning business_id::text
"""

RECORD_SQL = f"""
    update {TABLE}
       set status = $2, reason = $3, finished_at = now(), updated_at = now(),
           warmbly_contact_ids = $4, warmbly_draft_ids = $5
     where business_id = $1::uuid
"""


def _dsn() -> str:
    url = os.environ.get('LEADS_DB_URL')
    if not url:
        raise RuntimeError('LEADS_DB_URL is not set')
    return url.replace('postgres://', 'postgresql://')


async def table_exists(conn: Any) -> bool:
    return bool(await conn.fetchval('select to_regclass($1) is not null', TABLE))


def outcome_status(outcome: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    The (status, reason) a run_agents outcome leaves in the table.

    'rejected' is final: the model looked and said no. Errors, failed
    imports and a model that never answered are 'failed', which selection
    offers again on a later batch.
    """
    if outcome.get('import_status') == 'imported':
        return 'imported', None
    if outcome.get('research_status') == 'error' or outcome.get('draft_status') == 'error':
        return 'failed', outcome.get('error')
    if outcome.get('research_status') not in (None, 'researched'):
        reason = outcome.get('rejection_reason') or outcome['research_status']
        return ('failed' if reason == UNAVAILABLE else 'rejected'), reason
    if outcome.get('draft_status') and outcome['draft_status'] != 'drafted':
        if outcome.get('draft_reason') == UNAVAILABLE:
            return 'failed', f'draft {UNAVAILABLE}'
        return 'rejected', f"draft {outcome['draft_status']}"
    if outcome.get('import_status') is not None:
        return 'failed', (outcome.get('import_error') or outcome.get('import_reason')
                          or 'import failed')
    return 'failed', 'did not finish'


async def claim_businesses(ids: Sequence[str], run_id: Optional[str] = None,
                           force: bool = False) -> List[str]:
    """
    Claim `ids` for this run; return the ones it got, in the order given.

    Without the table, every id is granted: the pipeline ran that way before
    tracking existed.
    """
    ids = [str(i) for i in ids]
    if not ids:
        return []
    conn = await asyncpg.connect(_dsn())
    try:
        if not await table_exists(conn):
            print(f'note: {TABLE} does not exist (db/migrations/015); '
                  f'not claiming, so batches can repeat businesses')
            return ids
        rows = await conn.fetch(FORCE_CLAIM_SQL if force else CLAIM_SQL, ids, run_id)
    finally:
        await conn.close()
    got = {r[0] for r in rows}
    return [i for i in ids if i in got]


async def record_outcome(outcome: Dict[str, Any]) -> None:
    """Write one business's end state. Best effort: a failure is printed, not raised."""
    business_id = outcome.get('business_id')
    if not business_id:
        return
    status, reason = outcome_status(outcome)
    try:
        conn = await asyncpg.connect(_dsn())
        try:
            if not await table_exists(conn):
                return
            await conn.execute(RECORD_SQL, str(business_id), status,
                               (reason or '')[:1000] or None,
                               outcome.get('warmbly_contact_ids'),
                               outcome.get('warmbly_draft_ids'))
        finally:
            await conn.close()
    except Exception as exc:
        print(f'could not record outcome for {business_id}: {type(exc).__name__}: {exc}')
