"""
Retrying a proxy call the upstream could not serve.

The free nemotron model 503s with "Upstream error from Nvidia: Service
temporarily overloaded" often enough to cost a business per batch (flow runs
335c45ca, c6f184fc). The agents catch LLMTransportError and return a
rejection, so a Prefect flow retry never fires; the retry sits on the proxy
call itself, as a Prefect task, so each attempt shows in the UI.

The policy is plain functions so it is testable without a Prefect server:
which errors are worth another try, how long to wait, and how long each
attempt may take -- longer every time, because a model that timed out once
under load will time out again at the same limit.
"""

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from flow import llm
from flow.llm import LLMSchemaError, LLMTransportError


class TestIsTransient:
    @pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504, 529])
    def test_overload_and_gateway_statuses_retry(self, status):
        assert llm.is_transient(LLMTransportError('x', status_code=status))

    def test_a_timeout_or_dropped_connection_retries(self):
        """No status: the deadline passed, or the connection failed."""
        assert llm.is_transient(LLMTransportError('no reply within 180s'))

    @pytest.mark.parametrize('status', [400, 401, 403, 404, 422])
    def test_config_and_request_errors_do_not(self, status):
        """A wrong key or a bad schema fails the same way every time."""
        assert not llm.is_transient(LLMTransportError('x', status_code=status))

    def test_schema_and_other_errors_do_not(self):
        assert not llm.is_transient(LLMSchemaError('never validated'))
        assert not llm.is_transient(KeyError('choices'))


class TestRetryDelays:
    def test_default_doubles_up_to_a_cap(self, monkeypatch):
        for name in ('LLM_RETRIES', 'LLM_RETRY_DELAY', 'LLM_RETRY_MAX_DELAY'):
            monkeypatch.delenv(name, raising=False)

        assert llm.retry_delays() == [15, 30, 60, 120, 240, 300]

    def test_env_sets_count_base_and_cap(self, monkeypatch):
        monkeypatch.setenv('LLM_RETRIES', '4')
        monkeypatch.setenv('LLM_RETRY_DELAY', '10')
        monkeypatch.setenv('LLM_RETRY_MAX_DELAY', '50')

        assert llm.retry_delays() == [10, 20, 40, 50]

    def test_zero_retries_is_no_retry(self, monkeypatch):
        monkeypatch.setenv('LLM_RETRIES', '0')

        assert llm.retry_delays() == []


class TestAttemptLimits:
    """(call deadline, per-read timeout) for attempt n, 1-based."""

    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.delenv('LLM_MAX_CALL_TIMEOUT', raising=False)
        monkeypatch.setattr(llm, 'CALL_TIMEOUT', 180.0)
        monkeypatch.setattr(llm, 'REQUEST_TIMEOUT', 120.0)

    def test_first_attempt_is_todays_limits(self):
        assert llm.attempt_limits(1) == (180.0, 120.0)

    def test_each_retry_doubles_both(self):
        assert llm.attempt_limits(2) == (360.0, 240.0)
        assert llm.attempt_limits(3) == (720.0, 480.0)

    def test_capped(self, monkeypatch):
        """Both stop at LLM_MAX_CALL_TIMEOUT (default 900s); the per-read
        timeout never exceeds the call's own deadline."""
        assert llm.attempt_limits(6) == (900.0, 900.0)
        monkeypatch.setenv('LLM_MAX_CALL_TIMEOUT', '300')
        assert llm.attempt_limits(3) == (300.0, 300.0)

    def test_scales_an_explicit_deadline(self):
        """multi_agent passes its own deadline; retries grow that one."""
        assert llm.attempt_limits(2, base_deadline=60.0) == (120.0, 120.0)


def _status(code):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = code
    resp.text = 'Service temporarily overloaded'
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        str(code), request=MagicMock(), response=resp)
    return resp


def _ok(content='{}'):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {'choices': [{'message': {'content': content}}]}
    resp.raise_for_status.return_value = None
    return resp


class TestOutsideAFlow:
    """With no flow run there is no Prefect task: a bare task call here
    tries to start an ephemeral server and times out. One attempt, as
    before, so unit tests and scripts never sleep through a backoff."""

    def test_a_503_is_raised_after_one_attempt(self):
        with patch('flow.llm.httpx.post', return_value=_status(503)) as post:
            with pytest.raises(LLMTransportError) as caught:
                llm.chat_completion([{'role': 'user', 'content': 'hi'}], 'm')

        assert caught.value.status_code == 503
        assert post.call_count == 1


class TestInsideAFlow:
    """The call goes through the Prefect task, whose retry condition is
    is_transient and whose attempt number sets the limits."""

    def test_routes_through_the_task_when_a_flow_run_is_active(self):
        with patch('flow.llm._in_prefect_run', return_value=True), \
                patch('flow.llm._llm_call_task') as task:
            task.with_options.return_value.return_value = ('ok', 0.0, None)
            content, cost = llm.chat_completion(
                [{'role': 'user', 'content': 'hi'}], 'm')

        assert content == 'ok'
        options = task.with_options.call_args.kwargs
        assert options['retries'] == len(llm.retry_delays())
        assert options['retry_delay_seconds'] == llm.retry_delays()
        assert options['retry_condition_fn'] is llm.retry_condition

    def test_the_task_uses_the_attempts_limits(self):
        """Attempt 3 of the task gets attempt_limits(3)."""
        with patch('flow.llm._run_count', return_value=3), \
                patch('flow.llm._chat_completion',
                      return_value=('ok', 0.0, None)) as call:
            llm._llm_call_task.fn([{'role': 'user', 'content': 'hi'}], 'm',
                                  None, None, None, None, None, {})

        kwargs = call.call_args.kwargs
        assert (kwargs['deadline'], kwargs['request_timeout']) == \
            llm.attempt_limits(3)

    def test_task_does_not_cache(self):
        """Prefect's default cache keys on inputs and the flow run: the
        same prompt twice in one run must make two calls."""
        from prefect.cache_policies import NO_CACHE

        assert llm._llm_call_task.cache_policy is NO_CACHE


class TestRetryCondition:
    def _state(self, exc):
        state = MagicMock()
        state.result.side_effect = exc
        return state

    def test_retries_a_503(self):
        assert llm.retry_condition(
            None, None, self._state(LLMTransportError('x', status_code=503)))

    def test_not_a_401(self):
        assert not llm.retry_condition(
            None, None, self._state(LLMTransportError('x', status_code=401)))


@pytest.mark.skipif(not os.environ.get('PREFECT_API_URL'),
                    reason='needs a Prefect server (PREFECT_API_URL), like '
                           'tests/test_prefect_var.py')
class TestRealPrefectRetry:
    """End to end on the configured server: Prefect really reruns the task.
    (prefect_test_harness cannot start against this Prefect/Starlette.)"""

    def test_a_503_then_success_returns_the_content(self, monkeypatch):
        from prefect import flow

        monkeypatch.setenv('LLM_RETRY_DELAY', '0')

        @flow(name='test-llm-retry')
        def one_call():
            return llm.chat_completion([{'role': 'user', 'content': 'hi'}], 'm')

        with patch('flow.llm.httpx.post',
                   side_effect=[_status(503), _ok('done')]) as post:
            content, _ = one_call()

        assert content == 'done'
        assert post.call_count == 2
        first, second = (c.kwargs['timeout'] for c in post.call_args_list)
        assert (first, second) == (llm.attempt_limits(1)[1],
                                   llm.attempt_limits(2)[1])

    def test_a_401_is_not_retried(self, monkeypatch):
        from prefect import flow

        monkeypatch.setenv('LLM_RETRY_DELAY', '0')

        @flow(name='test-llm-retry')
        def one_call():
            return llm.chat_completion([{'role': 'user', 'content': 'hi'}], 'm')

        with patch('flow.llm.httpx.post', return_value=_status(401)) as post:
            with pytest.raises(LLMTransportError):
                one_call()

        assert post.call_count == 1
