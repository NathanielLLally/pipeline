"""
Newest discord-bot run wins: each run stops every older live run on start.

Only one bot may be connected at a time, or every Warmbly event is posted
twice. That is enforced here, by the run itself, rather than with a Prefect
deployment concurrency limit: a limit cancels (CANCEL_NEW) or parks (ENQUEUE)
a new run at the slot check, before its own code can clear a predecessor, so a
dead run holding the slot made every start from the UI fail.

On start, a run:

  1. finds every other run of the deployment in Running, Pending or
     Cancelling that started *before* it (ties broken by id, so exactly one of
     two simultaneous runs survives). Newer runs are left alone: they will do
     the same to this one, and only the newest is left;
  2. if an older run's process is on this host and provably that run's (its
     /proc/<pid>/environ names the run id), sends SIGTERM, then SIGKILL;
  3. forces it to Cancelled.

Runs record where they live as tags (`pid:<n>`, `host:<name>`) because the
runner leaves flow_run.infrastructure_pid empty.
"""

import os
import signal
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional, Tuple

LIVE_STATES = frozenset({'RUNNING', 'PENDING', 'CANCELLING'})
RUN_ID_ENV = 'PREFECT__FLOW_RUN_ID'
TERM_GRACE = 10.0


@dataclass
class ReapReport:
    older: int = 0
    cancelled: int = 0
    killed: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (f'older={self.older} cancelled={self.cancelled} '
                f'killed={self.killed}')
        return text + (f' errors={self.errors}' if self.errors else '')


def is_blocker(run: Any) -> bool:
    """A run that is, or is about to be, connected to Discord and Warmbly."""
    return (run.state_type.value if run.state_type else '') in LIVE_STATES


def _started(run: Any) -> datetime:
    return run.start_time or run.expected_start_time


def _order(run: Any) -> Tuple[datetime, str]:
    return _started(run), str(run.id)


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


async def reap(client: Any, deployment_name: str, current: Any) -> ReapReport:
    """Stop every live run of the deployment that is older than `current`."""
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
    mine = _order(current)

    for run in runs:
        if str(run.id) == str(current.id) or not is_blocker(run):
            continue
        if _order(run) > mine:
            continue
        report.older += 1

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
                state=Cancelled(message='Replaced by a newer discord-bot run.'),
                force=True,
            )
            report.cancelled += 1
        except Exception as exc:
            report.errors.append(f'cancel {run.name}: {exc}')

    return report
