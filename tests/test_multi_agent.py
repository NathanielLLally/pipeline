"""
flow.multi_agent as a library, and complete_structured delegating to it.

Every model call is mocked at flow.llm.httpx.post, the same seam test_llm.py
uses, so these tests also pin down the transport: the proxy key header, the
response_format, and the cost header.
"""

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import httpx
import pytest

from flow import multi_agent
from flow.llm import (
    MULTI_AGENT_MODEL,
    LLMSchemaError,
    LLMTransportError,
    complete_structured,
)
from flow.schemas import ResearchOutput

VALID = {
    "business_name": "Happy Tails", "pain_signals": ["no booking"],
    "personalization_hook": "phone only", "inferred_tone": "warm",
    "confidence": 0.8, "evidence": ["Call us"],
    "suggested_email": "owner@happytails.com",
}
GOOD_SCORES = json.dumps({"scores": {c: 5 for c in multi_agent.RUBRIC},
                          "feedback": "fine"})
BAD_SCORES = json.dumps({"scores": {c: 1 for c in multi_agent.RUBRIC},
                         "feedback": "wrong"})


def _reply(content, cost=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.headers = {'x-litellm-response-cost': str(cost)} if cost else {}
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    r.raise_for_status.return_value = None
    return r


def _role(call):
    """Which graph node made this call, read from its prompt."""
    text = call.kwargs['json']['messages'][-1]['content']
    if text.startswith('Classify the user request'):
        return 'router'
    if 'You are a critical evaluator' in text:
        return 'critic'
    if text.startswith('You are the evaluator'):
        return 'evaluator'
    if 'You are a frontier AI advisor' in text:
        return 'frontier'
    if 'did not validate' in text:
        return 'repair'
    return 'specialist'


def _scripted(**replies):
    """httpx.post stand-in answering by role; a list is consumed in order."""
    def post(url, **kwargs):
        role = _role(MagicMock(kwargs=kwargs))
        reply = replies.get(role)
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, MagicMock) else _reply(reply)
    return post


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No config.yaml, no MLflow, fresh caches for every test."""
    monkeypatch.setenv('LITELLM_CONFIG', str(tmp_path / 'absent.yaml'))
    monkeypatch.delenv('MULTI_AGENT_MLFLOW', raising=False)
    multi_agent.reset_caches()


def _calls_by_role(post):
    return [_role(c) for c in post.call_args_list]


class TestImportable:
    def test_import_has_no_side_effects_without_a_config(self, tmp_path):
        """serve_agents imports this; a sys.exit at import kills the server."""
        env = {'LITELLM_CONFIG': str(tmp_path / 'absent.yaml'), 'PATH': ''}
        out = subprocess.run(
            [sys.executable, '-c',
             'import os, flow.multi_agent; '
             'print("MLFLOW_TRACKING_URI" in os.environ)'],
            capture_output=True, text=True, env=env, cwd=str(_repo_root()),
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == 'False'

    def test_missing_config_falls_back_to_the_preference_lists(self):
        roles = multi_agent.role_models()
        assert roles['cheap'] == multi_agent.ROLE_PREFERENCES['cheap']

    def test_config_filters_the_preference_lists(self, monkeypatch, tmp_path):
        config = tmp_path / 'config.yaml'
        config.write_text('model_list:\n  - model_name: ollama/gpt-oss:20b\n')
        monkeypatch.setenv('LITELLM_CONFIG', str(config))
        multi_agent.reset_caches()
        assert multi_agent.role_models()['cheap'] == ['ollama/gpt-oss:20b']


class TestRun:
    def test_passing_answer_is_the_specialist_output(self):
        post = _scripted(router='mid', specialist='The answer.',
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post):
            result = multi_agent.run('Explain dogs')

        assert result['final_answer'] == 'The answer.'
        assert result['escalated_to_frontier'] is False

    def test_failing_answer_escalates_to_frontier(self):
        post = _scripted(router='mid', specialist='Bad.', critic='NO ISSUES',
                         evaluator=BAD_SCORES, frontier='Better.')
        with patch('flow.llm.httpx.post', side_effect=post):
            result = multi_agent.run('Explain dogs')

        assert result['final_answer'] == 'Better.'

    def test_frontier_failure_keeps_the_specialist_answer(self):
        """An error string must never become the final answer."""
        post = _scripted(router='mid', specialist='Bad.', critic='NO ISSUES',
                         evaluator=BAD_SCORES,
                         frontier=httpx.ReadTimeout('slow'))
        with patch('flow.llm.httpx.post', side_effect=post):
            result = multi_agent.run('Explain dogs')

        assert result['final_answer'] == 'Bad.'

    def test_specialist_failure_raises_a_transport_error(self):
        post = _scripted(router='mid', specialist=httpx.ReadTimeout('slow'))
        with patch('flow.llm.httpx.post', side_effect=post):
            with pytest.raises(LLMTransportError):
                multi_agent.run('Explain dogs')

    def test_system_prompt_reaches_specialist_critic_and_evaluator(self):
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs', system='Never invent facts.')

        for call in mock.call_args_list:
            if _role(call) == 'specialist':
                assert call.kwargs['json']['messages'][0] == {
                    'role': 'system', 'content': 'Never invent facts.'}
            if _role(call) in ('critic', 'evaluator'):
                assert 'Never invent facts.' in str(call.kwargs['json'])

    def test_uses_the_proxy_key_header_not_authorization(self, monkeypatch):
        monkeypatch.setenv('LITELLM_PROXY_API_KEY', 'sk-ma')
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs')

        headers = mock.call_args.kwargs['headers']
        assert headers['x-litellm-api-key'] == 'Bearer sk-ma'
        assert 'Authorization' not in headers

    def test_prefers_the_multi_agent_proxy_url(self, monkeypatch):
        monkeypatch.setenv('LITELLM_PROXY_URL', 'http://ma-proxy:4000')
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs')

        assert mock.call_args.args[0] == 'http://ma-proxy:4000/v1/chat/completions'

    def test_cost_comes_from_the_proxy_header(self):
        post = _scripted(router='mid', specialist=_reply('x', cost=0.25),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post):
            result = multi_agent.run('Explain dogs')

        assert result['total_cost'] == pytest.approx(0.25)

    def test_402_model_is_skipped_on_later_calls(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 402
        resp.text = 'payment required'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            '402', request=MagicMock(), response=resp)

        models = ['a/one', 'b/two']
        tried = []

        def post(url, **kwargs):
            tried.append(kwargs['json']['model'])
            return resp if kwargs['json']['model'] == 'a/one' else _reply('ok')

        with patch('flow.llm.httpx.post', side_effect=post):
            multi_agent.chat(models, 'hi')
            multi_agent.chat(models, 'hi')

        assert tried == ['a/one', 'b/two', 'b/two']


class TestCompleteStructuredDelegation:
    def test_sentinel_model_runs_the_multi_agent_graph(self):
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            out = complete_structured('describe', ResearchOutput,
                                      model=MULTI_AGENT_MODEL)

        assert isinstance(out, ResearchOutput)
        assert out.confidence == 0.8
        assert {'router', 'specialist', 'critic'} <= set(_calls_by_role(mock))

    def test_agent_env_var_selects_the_multi_agent(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', MULTI_AGENT_MODEL)
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            complete_structured('describe', ResearchOutput,
                                agent_model_env='RESEARCH_MODEL')

        assert 'critic' in _calls_by_role(mock)

    def test_other_models_stay_on_the_single_call_path(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', 'gpt-4o-mini')
        with patch('flow.llm.httpx.post',
                   return_value=_reply(json.dumps(VALID))) as mock:
            complete_structured('describe', ResearchOutput,
                                agent_model_env='RESEARCH_MODEL')

        assert mock.call_count == 1

    def test_answering_calls_request_the_json_schema(self):
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            complete_structured('describe', ResearchOutput,
                                model=MULTI_AGENT_MODEL)

        for call in mock.call_args_list:
            body = call.kwargs['json']
            if _role(call) == 'specialist':
                assert body['response_format']['type'] == 'json_schema'
            if _role(call) == 'router':
                assert 'response_format' not in body

    def test_schema_is_spelled_out_in_the_prompt_text(self):
        """Some proxied models ignore response_format; the prompt must carry the schema."""
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            complete_structured('describe', ResearchOutput,
                                model=MULTI_AGENT_MODEL)

        specialist = next(c for c in mock.call_args_list
                          if _role(c) == 'specialist')
        text = specialist.kwargs['json']['messages'][-1]['content']
        assert text.startswith('describe')
        assert 'personalization_hook' in text

    def test_invalid_answer_gets_one_repair_call(self):
        post = _scripted(router='mid', specialist='not json',
                         critic='NO ISSUES', evaluator=GOOD_SCORES,
                         repair=json.dumps(VALID))
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            out = complete_structured('describe', ResearchOutput,
                                      model=MULTI_AGENT_MODEL)

        assert out.business_name == 'Happy Tails'
        assert _calls_by_role(mock).count('repair') == 1
        repair = next(c for c in mock.call_args_list if _role(c) == 'repair')
        assert 'personalization_hook' in str(repair.kwargs['json']['messages'])

    def test_unrepairable_answer_raises_a_schema_error(self):
        post = _scripted(router='mid', specialist='not json',
                         critic='NO ISSUES', evaluator=GOOD_SCORES,
                         repair='still not json')
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            with pytest.raises(LLMSchemaError):
                complete_structured('describe', ResearchOutput,
                                    model=MULTI_AGENT_MODEL, max_attempts=2)

        # The graph runs once; retries are cheap repairs, not whole re-runs.
        assert _calls_by_role(mock).count('specialist') == 1
        assert _calls_by_role(mock).count('repair') == 1

    def test_transport_failure_surfaces_as_a_transport_error(self):
        post = _scripted(router='mid', specialist=httpx.ReadTimeout('slow'))
        with patch('flow.llm.httpx.post', side_effect=post):
            with pytest.raises(LLMTransportError):
                complete_structured('describe', ResearchOutput,
                                    model=MULTI_AGENT_MODEL)

    def test_overall_deadline_stops_further_calls(self, monkeypatch):
        """Five sequential calls must not each get the full timeout."""
        monkeypatch.setenv('MULTI_AGENT_CALL_TIMEOUT', '0')
        with patch('flow.llm.httpx.post') as mock:
            with pytest.raises(LLMTransportError) as caught:
                multi_agent.run('Explain dogs')

        assert mock.call_count == 0
        assert 'MULTI_AGENT_CALL_TIMEOUT' in str(caught.value)


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent


class TestForcedTierAndEffort:
    def test_forced_tier_skips_the_router(self):
        post = _scripted(specialist='x', critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            state = multi_agent.run('Explain dogs', tier='frontier')

        assert 'router' not in _calls_by_role(mock)
        assert state['route_decision'] == 'frontier'
        assert state['route_method'] == 'forced'
        assert state['models_used']['specialist'] == multi_agent.ROLE_PREFERENCES['frontier'][0]

    def test_unknown_tier_is_refused(self):
        with pytest.raises(ValueError):
            multi_agent.run('Explain dogs', tier='ultra')

    def test_effort_goes_only_on_answering_calls(self):
        post = _scripted(router='mid', specialist='Bad.', critic='NO ISSUES',
                         evaluator=BAD_SCORES, frontier='Better.')
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs', reasoning_effort='high')

        for call in mock.call_args_list:
            body = call.kwargs['json']
            if _role(call) in ('specialist', 'frontier'):
                assert body['reasoning_effort'] == 'high'
                # Thinking counts against max_tokens; 4000 starves the answer.
                assert body['max_tokens'] >= 16000
            else:
                assert 'reasoning_effort' not in body

    def test_effort_reaches_the_repair_call(self):
        post = _scripted(router='mid', specialist='not json', critic='NO ISSUES',
                         evaluator=GOOD_SCORES, repair=json.dumps(VALID))
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.complete_structured('describe', ResearchOutput,
                                            reasoning_effort='high')

        repair = next(c for c in mock.call_args_list if _role(c) == 'repair')
        assert repair.kwargs['json']['reasoning_effort'] == 'high'

    def test_research_env_settings_apply_to_research(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', MULTI_AGENT_MODEL)
        monkeypatch.setenv('RESEARCH_TIER', 'frontier')
        monkeypatch.setenv('RESEARCH_REASONING_EFFORT', 'high')
        post = _scripted(specialist=json.dumps(VALID), critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            complete_structured('describe', ResearchOutput,
                                agent_model_env='RESEARCH_MODEL')

        assert 'router' not in _calls_by_role(mock)
        specialist = next(c for c in mock.call_args_list if _role(c) == 'specialist')
        assert specialist.kwargs['json']['reasoning_effort'] == 'high'

    def test_research_settings_do_not_leak_into_drafting(self, monkeypatch):
        monkeypatch.setenv('DRAFTING_MODEL', MULTI_AGENT_MODEL)
        monkeypatch.setenv('RESEARCH_TIER', 'frontier')
        monkeypatch.setenv('RESEARCH_REASONING_EFFORT', 'high')
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            complete_structured('describe', ResearchOutput,
                                agent_model_env='DRAFTING_MODEL')

        assert 'router' in _calls_by_role(mock)
        assert all('reasoning_effort' not in c.kwargs['json'] for c in mock.call_args_list)


class TestRunLogging:
    def test_each_run_logs_who_answered(self, capsys):
        post = _scripted(specialist='x', critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post):
            multi_agent.run('Explain dogs', tier='frontier', reasoning_effort='high')

        out = capsys.readouterr().out
        assert 'tier=frontier (forced)' in out
        assert 'effort=high' in out
        assert multi_agent.ROLE_PREFERENCES['frontier'][0] in out

    def test_a_model_falling_through_logs_why(self, capsys):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 429
        resp.text = 'rate limited'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            '429', request=MagicMock(), response=resp)

        def post(url, **kwargs):
            return resp if kwargs['json']['model'] == 'a/one' else _reply('ok')

        with patch('flow.llm.httpx.post', side_effect=post):
            multi_agent.chat(['a/one', 'b/two'], 'hi')

        out = capsys.readouterr().out
        assert 'a/one' in out and '429' in out


def _scores(value, unmet=None):
    reply = {"scores": {c: value for c in multi_agent.RUBRIC}, "feedback": "f"}
    if unmet is not None:
        reply["unmet_requirements"] = unmet
    return json.dumps(reply)


def _evaluator_prompts(mock):
    return [c.kwargs['json']['messages'][-1]['content']
            for c in mock.call_args_list if _role(c) == 'evaluator']


class TestFinalAnswerIsOnPoint:
    """The evaluator holds whatever is returned to the user's original request."""

    def test_an_escalated_answer_is_graded_too(self):
        post = _scripted(router='mid', specialist='Bad.', critic='NO ISSUES',
                         evaluator=[BAD_SCORES, GOOD_SCORES], frontier='Better.')
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            state = multi_agent.run('Explain dogs')

        prompts = _evaluator_prompts(mock)
        assert len(prompts) == 2
        assert 'Better.' in prompts[1]
        assert state['final_answer'] == 'Better.'
        assert state['answer_source'] == 'frontier'
        assert state['final_on_point'] is True

    def test_a_worse_rewrite_keeps_the_original_answer(self):
        post = _scripted(router='mid', specialist='Decent.', critic='NO ISSUES',
                         evaluator=[_scores(3), _scores(2)], frontier='Worse.')
        with patch('flow.llm.httpx.post', side_effect=post):
            state = multi_agent.run('Explain dogs')

        assert state['final_answer'] == 'Decent.'
        assert state['answer_source'] == 'specialist'

    def test_an_on_point_answer_beats_a_higher_scoring_off_point_one(self):
        post = _scripted(router='mid', specialist='Polished.', critic='NO ISSUES',
                         evaluator=[_scores(5, ['ignores the city']), _scores(3, [])],
                         frontier='On point.')
        with patch('flow.llm.httpx.post', side_effect=post):
            state = multi_agent.run('Explain dogs')

        assert state['final_answer'] == 'On point.'

    def test_an_unmet_requirement_fails_a_perfect_score(self):
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=[_scores(5, ['no business name']), GOOD_SCORES],
                         frontier='y')
        with patch('flow.llm.httpx.post', side_effect=post):
            state = multi_agent.run('Explain dogs')

        assert state['escalated_to_frontier'] is True
        assert state['unmet_requirements'] == ['no business name']

    def test_the_rewrite_is_told_what_was_missed(self):
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=[_scores(5, ['no business name']), GOOD_SCORES],
                         frontier='y')
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs')

        frontier = next(c for c in mock.call_args_list if _role(c) == 'frontier')
        assert 'no business name' in frontier.kwargs['json']['messages'][-1]['content']

    def test_evaluator_sees_the_original_request_and_instructions(self):
        post = _scripted(router='mid', specialist=json.dumps(VALID),
                         critic='NO ISSUES', evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.complete_structured('Research Happy Tails', ResearchOutput,
                                            system='Never invent facts.')

        prompt = _evaluator_prompts(mock)[0]
        assert 'Research Happy Tails' in prompt
        assert 'Never invent facts.' in prompt
        assert 'unmet_requirements' in prompt

    def test_cheap_answers_are_evaluated_too(self):
        post = _scripted(router='cheap', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            state = multi_agent.run('Define dog')

        assert 'evaluator' in _calls_by_role(mock)
        assert state['final_on_point'] is True

    def test_an_ungradable_answer_is_unchecked_not_on_point(self):
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator='I cannot grade this.')
        with patch('flow.llm.httpx.post', side_effect=post):
            state = multi_agent.run('Explain dogs')

        assert state['final_on_point'] is None
        assert state['evaluation_method'] == 'critic-severity'

    def test_log_line_reports_the_choice(self, capsys):
        post = _scripted(router='mid', specialist='Bad.', critic='NO ISSUES',
                         evaluator=[BAD_SCORES, GOOD_SCORES], frontier='Better.')
        with patch('flow.llm.httpx.post', side_effect=post):
            multi_agent.run('Explain dogs')

        out = capsys.readouterr().out
        assert 'chosen=frontier' in out and 'on_point=True' in out


class TestCheckerBudgets:
    """Live runs: 1500 tokens starved reasoning critics; 90s cut off nemotron-3-ultra."""

    def test_critic_and_evaluator_get_room_to_think(self):
        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post) as mock:
            multi_agent.run('Explain dogs')

        for call in mock.call_args_list:
            if _role(call) in ('critic', 'evaluator'):
                assert call.kwargs['json']['max_tokens'] >= 4000

    def test_answering_calls_get_a_longer_timeout_than_the_router(self, monkeypatch):
        monkeypatch.setenv('MULTI_AGENT_CALL_TIMEOUT', '5000')
        seen = {}
        real = multi_agent.chat_completion

        def spy(messages, model, **kwargs):
            seen.setdefault(messages[-1]['content'][:20], kwargs['deadline'])
            return real(messages, model, **kwargs)

        post = _scripted(router='mid', specialist='x', critic='NO ISSUES',
                         evaluator=GOOD_SCORES)
        with patch('flow.llm.httpx.post', side_effect=post), \
             patch.object(multi_agent, 'chat_completion', side_effect=spy):
            multi_agent.run('Explain dogs')

        assert seen['Explain dogs'] == pytest.approx(300, abs=1)
        assert seen['Classify the user re'] == pytest.approx(90, abs=1)

    def test_timeout_message_names_the_limit_that_fired(self):
        import threading
        from flow.llm import chat_completion

        def hang(*args, **kwargs):
            threading.Event().wait(5)

        with patch('flow.llm.httpx.post', side_effect=hang):
            with pytest.raises(LLMTransportError) as caught:
                chat_completion([], 'ollama/x', deadline=0.1,
                                timeout_name='ANSWER_REQUEST_TIMEOUT')

        assert 'ANSWER_REQUEST_TIMEOUT=0.1s' in str(caught.value)
        assert 'budget' not in str(caught.value)
