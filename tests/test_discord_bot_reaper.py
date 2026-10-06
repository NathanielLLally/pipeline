"""
Unit tests for clearing stale discord-bot runs before a new one starts.

The deployment runs with concurrency_limit=1 and CANCEL_NEW, so a run left
holding the slot (killed process, lost worker) would block every later start.
The reaper releases it: kill the stale process if it is provably that run's,
force the run to Cancelled (which the server turns into a slot release), and
reset the counter if it still reads as held.

No Prefect server or real process is touched: the client and the process
probes are fakes.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from flow import discord_bot_reaper as reaper


def _run(coro):
    return asyncio.run(coro)


def flow_run(state_type, state_name=None, tags=(), run_id=None):
    return SimpleNamespace(
        id=run_id or uuid4(),
        name=f'run-{state_type.lower()}',
        state_type=SimpleNamespace(value=state_type),
        state_name=state_name or state_type.title(),
        tags=list(tags),
    )


DEPLOYMENT_ID = uuid4()


def fake_client(runs, active_slots=0,
                limit_name=f'deployment:{DEPLOYMENT_ID}'):
    client = MagicMock()
    client.read_deployment_by_name = AsyncMock(return_value=SimpleNamespace(
        id=DEPLOYMENT_ID,
        global_concurrency_limit=SimpleNamespace(
            name=limit_name, limit=1, active_slots=active_slots)))
    client.read_flow_runs = AsyncMock(return_value=runs)
    client.set_flow_run_state = AsyncMock()
    client.update_global_concurrency_limit = AsyncMock()
    client.read_global_concurrency_limit_by_name = AsyncMock(
        return_value=SimpleNamespace(active_slots=active_slots))
    return client


class TestWhichRunsAreBlockers:
    @pytest.mark.parametrize('state', ['RUNNING', 'PENDING', 'CANCELLING'])
    def test_live_states_are_blockers(self, state):
        assert reaper.is_blocker(flow_run(state))

    def test_a_run_waiting_for_the_slot_is_a_blocker(self):
        assert reaper.is_blocker(
            flow_run('SCHEDULED', state_name='AwaitingConcurrencySlot'))

    def test_an_ordinary_scheduled_run_is_left_alone(self):
        assert not reaper.is_blocker(flow_run('SCHEDULED', state_name='Scheduled'))

    @pytest.mark.parametrize('state',
                             ['COMPLETED', 'FAILED', 'CANCELLED', 'CRASHED'])
    def test_finished_runs_are_not_blockers(self, state):
        assert not reaper.is_blocker(flow_run(state))


class TestTags:
    def test_reads_pid_and_host_tags(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:hawkeye', 'other'])
        assert reaper.run_process(run) == ('hawkeye', 4321)

    def test_missing_or_bad_tags_give_none(self):
        assert reaper.run_process(flow_run('RUNNING')) == (None, None)
        assert reaper.run_process(
            flow_run('RUNNING', tags=['pid:abc', 'host:h'])) == ('h', None)

    def test_self_tags_name_this_process(self):
        with patch('flow.discord_bot_reaper.os.getpid', return_value=99), \
             patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'):
            assert reaper.self_tags() == ['pid:99', 'host:hawkeye']


class TestOwnershipCheck:
    def test_pid_owned_when_its_env_names_the_run(self, tmp_path):
        run_id = uuid4()
        environ = tmp_path / 'environ'
        environ.write_bytes(
            b'PATH=/bin\x00PREFECT__FLOW_RUN_ID=' + str(run_id).encode() + b'\x00')
        with patch('flow.discord_bot_reaper._environ_path',
                   return_value=str(environ)):
            assert reaper.pid_belongs_to_run(123, run_id)

    def test_a_recycled_pid_for_another_run_is_not_owned(self, tmp_path):
        environ = tmp_path / 'environ'
        environ.write_bytes(b'PREFECT__FLOW_RUN_ID=' + str(uuid4()).encode() + b'\x00')
        with patch('flow.discord_bot_reaper._environ_path',
                   return_value=str(environ)):
            assert not reaper.pid_belongs_to_run(123, uuid4())

    def test_a_vanished_or_unreadable_pid_is_not_owned(self):
        with patch('flow.discord_bot_reaper._environ_path',
                   return_value='/nonexistent/environ'):
            assert not reaper.pid_belongs_to_run(123, uuid4())


class TestReap:
    def test_cancels_every_blocker_but_never_itself(self):
        me = uuid4()
        stale = flow_run('RUNNING')
        waiting = flow_run('SCHEDULED', state_name='AwaitingConcurrencySlot')
        mine = flow_run('RUNNING', run_id=me)
        done = flow_run('COMPLETED')
        client = fake_client([stale, waiting, mine, done])

        report = _run(reaper.reap(client, 'discord-bot/discord-bot',
                                  current_run_id=me))

        cancelled = [c.args[0] if c.args else c.kwargs['flow_run_id']
                     for c in client.set_flow_run_state.await_args_list]
        assert set(cancelled) == {stale.id, waiting.id}
        assert me not in cancelled
        assert report.cancelled == 2

    def test_cancellation_is_forced(self):
        client = fake_client([flow_run('RUNNING')])
        _run(reaper.reap(client, 'discord-bot/discord-bot'))
        call = client.set_flow_run_state.await_args
        assert call.kwargs['force'] is True
        assert call.kwargs['state'].type.value == 'CANCELLED'

    def test_kills_a_local_process_only_when_it_owns_the_run(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:hawkeye'])
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=True), \
             patch('flow.discord_bot_reaper.terminate') as term:
            report = _run(reaper.reap(client, 'discord-bot/discord-bot'))
        term.assert_called_once_with(4321)
        assert report.killed == 1

    def test_never_kills_a_pid_that_is_not_the_runs(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:hawkeye'])
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=False), \
             patch('flow.discord_bot_reaper.terminate') as term:
            _run(reaper.reap(client, 'discord-bot/discord-bot'))
        term.assert_not_called()
        client.set_flow_run_state.assert_awaited()

    def test_never_kills_a_process_on_another_host(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:elsewhere'])
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=True), \
             patch('flow.discord_bot_reaper.terminate') as term:
            _run(reaper.reap(client, 'discord-bot/discord-bot'))
        term.assert_not_called()

    def test_resets_a_slot_still_held_after_cancelling(self):
        client = fake_client([flow_run('RUNNING')], active_slots=1)
        report = _run(reaper.reap(client, 'discord-bot/discord-bot'))
        update = client.update_global_concurrency_limit.await_args
        assert update.kwargs['name'] == f'deployment:{DEPLOYMENT_ID}'
        assert update.kwargs['concurrency_limit'].active_slots == 0
        assert report.slots_reset

    def test_keeps_its_own_slot_when_run_from_inside_the_flow(self):
        """Inside a running flow, one held slot is that flow's own."""
        client = fake_client([], active_slots=1)
        report = _run(reaper.reap(client, 'discord-bot/discord-bot',
                                  current_run_id=uuid4()))
        client.update_global_concurrency_limit.assert_not_awaited()
        assert not report.slots_reset

    def test_a_free_slot_is_not_touched(self):
        client = fake_client([], active_slots=0)
        _run(reaper.reap(client, 'discord-bot/discord-bot'))
        client.update_global_concurrency_limit.assert_not_awaited()

    def test_one_failed_cancel_does_not_stop_the_rest(self):
        a, b = flow_run('RUNNING'), flow_run('PENDING')
        client = fake_client([a, b])
        client.set_flow_run_state = AsyncMock(
            side_effect=[RuntimeError('boom'), None])
        report = _run(reaper.reap(client, 'discord-bot/discord-bot'))
        assert client.set_flow_run_state.await_count == 2
        assert report.cancelled == 1
        assert report.errors

    def test_deployment_without_a_limit_only_cancels(self):
        client = fake_client([flow_run('RUNNING')])
        client.read_deployment_by_name.return_value = SimpleNamespace(
            id=DEPLOYMENT_ID, global_concurrency_limit=None)
        _run(reaper.reap(client, 'discord-bot/discord-bot'))
        client.update_global_concurrency_limit.assert_not_awaited()
        client.set_flow_run_state.assert_awaited_once()


class TestTerminate:
    def test_sigterm_then_sigkill_if_it_does_not_exit(self):
        import signal
        sent = []
        with patch('flow.discord_bot_reaper.os.kill',
                   side_effect=lambda pid, sig: sent.append(sig)), \
             patch('flow.discord_bot_reaper._alive', return_value=True), \
             patch('flow.discord_bot_reaper.time.sleep'):
            reaper.terminate(4321, grace=0.01)
        assert sent[0] == signal.SIGTERM
        assert sent[-1] == signal.SIGKILL

    def test_no_sigkill_when_it_exits_on_sigterm(self):
        import signal
        sent = []
        with patch('flow.discord_bot_reaper.os.kill',
                   side_effect=lambda pid, sig: sent.append(sig)), \
             patch('flow.discord_bot_reaper._alive', return_value=False):
            reaper.terminate(4321, grace=0.01)
        assert sent == [signal.SIGTERM]

    def test_an_already_gone_process_is_fine(self):
        with patch('flow.discord_bot_reaper.os.kill',
                   side_effect=ProcessLookupError):
            reaper.terminate(4321, grace=0.01)


class TestDeploymentConfig:
    def test_new_runs_are_cancelled_not_queued(self):
        from prefect.client.schemas.objects import ConcurrencyLimitStrategy
        from flow.serve_discord_bot import build_deployment
        deployment = build_deployment()
        # to_deployment splits the config: the number, and the strategy.
        assert deployment.concurrency_limit == 1
        assert (deployment.concurrency_options.collision_strategy
                == ConcurrencyLimitStrategy.CANCEL_NEW)


class TestLauncher:
    def test_reaps_before_starting_a_run(self):
        from flow import start_discord_bot as launcher
        order = []

        async def fake_reap(client, name, current_run_id=None):
            order.append(('reap', name, current_run_id))
            return reaper.ReapReport()

        async def fake_run_deployment(**kwargs):
            order.append(('start', kwargs['name']))
            return SimpleNamespace(id='fr', name='new-run')

        client = MagicMock()
        with patch('flow.start_discord_bot.reap', side_effect=fake_reap):
            msg = _run(launcher.start(client, fake_run_deployment))

        assert order == [('reap', launcher.DEPLOYMENT, None),
                         ('start', launcher.DEPLOYMENT)]
        assert 'new-run' in msg

    def test_start_returns_immediately(self):
        from flow import start_discord_bot as launcher
        seen = {}

        async def fake_run_deployment(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(id='fr', name='r')

        with patch('flow.start_discord_bot.reap',
                   new=AsyncMock(return_value=reaper.ReapReport())):
            _run(launcher.start(MagicMock(), fake_run_deployment))
        assert seen['timeout'] == 0


class TestFlowReapsOnStart:
    def test_tags_itself_then_reaps_keeping_its_own_slot(self):
        from flow import discord_bot as bot
        run_id = uuid4()
        client = MagicMock()
        client.read_flow_run = AsyncMock(
            return_value=SimpleNamespace(tags=['existing']))
        client.update_flow_run = AsyncMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=client)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch('flow.discord_bot.get_client', return_value=ctx), \
             patch('flow.discord_bot.reap',
                   new=AsyncMock(return_value=reaper.ReapReport())) as rp, \
             patch('flow.discord_bot.self_tags',
                   return_value=['pid:7', 'host:h']):
            _run(bot.claim_and_reap(run_id))

        tags = client.update_flow_run.await_args.kwargs['tags']
        assert set(tags) == {'existing', 'pid:7', 'host:h'}
        assert rp.await_args.kwargs['current_run_id'] == run_id

    def test_reap_failure_does_not_stop_the_bot(self):
        from flow import discord_bot as bot
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError('api down'))
        ctx.__aexit__ = AsyncMock(return_value=False)
        with patch('flow.discord_bot.get_client', return_value=ctx):
            _run(bot.claim_and_reap(uuid4()))

    def test_outside_a_flow_run_it_does_nothing(self):
        from flow import discord_bot as bot
        with patch('flow.discord_bot.get_client') as gc:
            _run(bot.claim_and_reap(None))
        gc.assert_not_called()
