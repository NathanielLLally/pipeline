"""
Unit tests for "newest discord-bot run wins".

The deployment has no Prefect concurrency limit, so a start from the UI always
runs. Each run's first act is to stop every OLDER live run of the deployment:
kill its process when it is provably that run's, then force it to Cancelled.
Newer runs are left alone, so two near-simultaneous starts cannot cancel each
other and leave nothing running.

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


from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def flow_run(state_type, state_name=None, tags=(), run_id=None, minutes=0):
    started = T0 + timedelta(minutes=minutes)
    return SimpleNamespace(
        id=run_id or uuid4(),
        name=f'run-{state_type.lower()}-{minutes}',
        state_type=SimpleNamespace(value=state_type),
        state_name=state_name or state_type.title(),
        tags=list(tags),
        start_time=started if state_type != 'SCHEDULED' else None,
        expected_start_time=started,
    )


DEPLOYMENT_ID = uuid4()


def fake_client(runs):
    client = MagicMock()
    client.read_deployment_by_name = AsyncMock(
        return_value=SimpleNamespace(id=DEPLOYMENT_ID))
    client.read_flow_runs = AsyncMock(return_value=runs)
    client.set_flow_run_state = AsyncMock()
    return client


class TestWhichRunsAreBlockers:
    @pytest.mark.parametrize('state', ['RUNNING', 'PENDING', 'CANCELLING'])
    def test_live_states_are_blockers(self, state):
        assert reaper.is_blocker(flow_run(state))

    def test_a_scheduled_run_is_left_alone(self):
        """It has not started; when it does, it will stop its elders itself."""
        assert not reaper.is_blocker(flow_run('SCHEDULED'))
        assert not reaper.is_blocker(
            flow_run('SCHEDULED', state_name='AwaitingConcurrencySlot'))

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
    def _me(self, minutes=10):
        return flow_run('RUNNING', minutes=minutes)

    def _cancelled(self, client):
        return {c.args[0] for c in client.set_flow_run_state.await_args_list}

    def test_cancels_older_live_runs_but_never_itself(self):
        me = self._me()
        older_running = flow_run('RUNNING', minutes=1)
        older_pending = flow_run('PENDING', minutes=2)
        done = flow_run('COMPLETED', minutes=3)
        client = fake_client([older_running, older_pending, me, done])

        report = _run(reaper.reap(client, 'discord-bot/discord-bot', me))

        assert self._cancelled(client) == {older_running.id, older_pending.id}
        assert report.cancelled == 2

    def test_newer_runs_are_left_alone(self):
        """Two starts close together must not cancel each other."""
        me = self._me(minutes=10)
        newer = flow_run('RUNNING', minutes=11)
        client = fake_client([me, newer])
        report = _run(reaper.reap(client, 'discord-bot/discord-bot', me))
        client.set_flow_run_state.assert_not_awaited()
        assert report.cancelled == 0

    def test_same_start_time_breaks_ties_by_id(self):
        """Exactly one of two simultaneous runs survives."""
        a = flow_run('RUNNING', minutes=5)
        b = flow_run('RUNNING', minutes=5)
        client_a = fake_client([a, b])
        client_b = fake_client([a, b])
        _run(reaper.reap(client_a, 'discord-bot/discord-bot', a))
        _run(reaper.reap(client_b, 'discord-bot/discord-bot', b))
        cancelled = self._cancelled(client_a) | self._cancelled(client_b)
        assert len(cancelled) == 1

    def test_cancellation_is_forced(self):
        client = fake_client([flow_run('RUNNING', minutes=1), self._me()])
        _run(reaper.reap(client, 'discord-bot/discord-bot', self._me()))
        call = client.set_flow_run_state.await_args
        assert call.kwargs['force'] is True
        assert call.kwargs['state'].type.value == 'CANCELLED'

    def test_kills_a_local_process_only_when_it_owns_the_run(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:hawkeye'], minutes=1)
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=True), \
             patch('flow.discord_bot_reaper.terminate') as term:
            report = _run(reaper.reap(client, 'discord-bot/discord-bot',
                                      self._me()))
        term.assert_called_once_with(4321)
        assert report.killed == 1

    def test_never_kills_a_pid_that_is_not_the_runs(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:hawkeye'], minutes=1)
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=False), \
             patch('flow.discord_bot_reaper.terminate') as term:
            _run(reaper.reap(client, 'discord-bot/discord-bot', self._me()))
        term.assert_not_called()
        client.set_flow_run_state.assert_awaited()

    def test_never_kills_a_process_on_another_host(self):
        run = flow_run('RUNNING', tags=['pid:4321', 'host:elsewhere'], minutes=1)
        client = fake_client([run])
        with patch('flow.discord_bot_reaper.socket.gethostname',
                   return_value='hawkeye'), \
             patch('flow.discord_bot_reaper.pid_belongs_to_run',
                   return_value=True), \
             patch('flow.discord_bot_reaper.terminate') as term:
            _run(reaper.reap(client, 'discord-bot/discord-bot', self._me()))
        term.assert_not_called()

    def test_one_failed_cancel_does_not_stop_the_rest(self):
        a, b = flow_run('RUNNING', minutes=1), flow_run('PENDING', minutes=2)
        client = fake_client([a, b])
        client.set_flow_run_state = AsyncMock(
            side_effect=[RuntimeError('boom'), None])
        report = _run(reaper.reap(client, 'discord-bot/discord-bot',
                                  self._me()))
        assert client.set_flow_run_state.await_count == 2
        assert report.cancelled == 1
        assert report.errors


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
    def test_no_prefect_concurrency_limit(self):
        """
        A Prefect limit cancels or parks a UI start before the run's own code
        can stop its predecessor. One-at-a-time is enforced by the run itself.
        """
        from flow.serve_discord_bot import build_deployment
        deployment = build_deployment()
        assert deployment.concurrency_limit is None
        assert deployment.concurrency_options is None

    def test_launcher_is_gone(self):
        import importlib.util
        assert importlib.util.find_spec('flow.start_discord_bot') is None


class TestFlowReapsOnStart:
    def test_tags_itself_then_stops_older_runs(self):
        from flow import discord_bot as bot
        run_id = uuid4()
        client = MagicMock()
        client.read_flow_run = AsyncMock(
            return_value=SimpleNamespace(id=run_id, tags=['existing']))
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
        assert rp.await_args.args[2].id == run_id

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
