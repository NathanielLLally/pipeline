"""
Clear stale discord-bot runs so a new one never waits on a concurrency slot.

The deployment allows one run at a time (a second would double-post every
Warmbly event) with CANCEL_NEW: a run that cannot get the slot is cancelled
immediately instead of sitting in AwaitingConcurrencySlot forever. That makes a
leaked slot fatal to every later start, so starting the bot first reaps:

  1. every other discord-bot run that is Running, Pending, Cancelling, or
     Scheduled as AwaitingConcurrencySlot is a blocker;
  2. if a blocker's process is on this host and provably that run's (its
     /proc/<pid>/environ names the run id), it gets SIGTERM, then SIGKILL;
  3. the blocker is forced to Cancelled. Leaving Running/Pending/Cancelling is
     what makes the server release its slot (ReleaseFlowConcurrencySlots);
  4. if the limit still reads as held, active_slots is reset. From the
     launcher that means "to 0"; from inside a run, one slot is the run's own
     and is left alone.

Runs record where they live as tags (`pid:<n>`, `host:<name>`) because the
runner leaves flow_run.infrastructure_pid empty.
"""

import os
import signal
import socket
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from uuid import UUID

LIVE_STATES = frozenset({'RUNNING', 'PENDING', 'CANCELLING'})
AWAITING_SLOT = 'AwaitingConcurrencySlot'
RUN_ID_ENV = 'PREFECT__FLOW_RUN_ID'
TERM_GRACE = 10.0


@dataclass
class ReapReport:
    blockers: int = 0
    cancelled: int = 0
    killed: int = 0
    slots_reset: bool = False
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (f'blockers={self.blockers} cancelled={self.cancelled} '
                f'killed={self.killed} slots_reset={self.slots_reset}')
        return text + (f' errors={self.errors}' if self.errors else '')


def is_blocker(run: Any) -> bool:
    state = run.state_type.value if run.state_type else ''
    if state in LIVE_STATES:
        return True
    return state == 'SCHEDULED' and run.state_name == AWAITING_SLOT


def self_tags() -> List[str]:
    return [f'pid:{os.getpid()}', f'host:{socket.gethostname()}']


def run_process(run: Any) -> Tuple[Optional[str], Optional[int]]:
    host = pid = None
    for tag in run.tags or ():
        if tag.startswith('host:'):
            host = tag[len('host:'):]
        elif tag.startswith('pid:'):
            try:
                pid = int(tag[len('pid:'):])
            except ValueError:
                pid = None
    return host, pid


def _environ_path(pid: int) -> str:
    return f'/proc/{pid}/environ'


def pid_belongs_to_run(pid: int, run_id: Any) -> bool:
    """True only if that pid's environment names this flow run."""
    try:
        with open(_environ_path(pid), 'rb') as fh:
            entries = fh.read().split(b'\x00')
    except OSError:
        return False
    wanted = f'{RUN_ID_ENV}={run_id}'.encode()
    return wanted in entries


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pid: int, grace: float = TERM_GRACE) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def reap(
    client: Any,
    deployment_name: str,
    current_run_id: Optional[UUID] = None,
) -> ReapReport:
    """Release everything that would stop a new discord-bot run."""
    from prefect.client.schemas.actions import GlobalConcurrencyLimitUpdate
    from prefect.client.schemas.filters import (
        DeploymentFilter,
        DeploymentFilterId,
    )
    from prefect.states import Cancelled

    report = ReapReport()
    deployment = await client.read_deployment_by_name(deployment_name)
    runs = await client.read_flow_runs(
        deployment_filter=DeploymentFilter(
            id=DeploymentFilterId(any_=[deployment.id])),
        limit=200,
    )
    here = socket.gethostname()

    for run in runs:
        if current_run_id is not None and str(run.id) == str(current_run_id):
            continue
        if not is_blocker(run):
            continue
        report.blockers += 1

        host, pid = run_process(run)
        if host == here and pid and pid != os.getpid() \
                and pid_belongs_to_run(pid, run.id):
            try:
                terminate(pid)
                report.killed += 1
            except Exception as exc:
                report.errors.append(f'kill {run.name} pid {pid}: {exc}')

        try:
            await client.set_flow_run_state(
                run.id,
                state=Cancelled(message='Reaped by a new discord-bot start.'),
                force=True,
            )
            report.cancelled += 1
        except Exception as exc:
            report.errors.append(f'cancel {run.name}: {exc}')

    limit = getattr(deployment, 'global_concurrency_limit', None)
    if limit is None:
        return report

    current = await client.read_global_concurrency_limit_by_name(limit.name)
    # From inside a run, one slot is that run's own; from the launcher, none.
    allowed = 1 if current_run_id is not None else 0
    if current.active_slots > allowed:
        await client.update_global_concurrency_limit(
            name=limit.name,
            concurrency_limit=GlobalConcurrencyLimitUpdate(active_slots=allowed),
        )
        report.slots_reset = True
    return report
