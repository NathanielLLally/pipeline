import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow import claude_tmux, claude_tmux_server as server
from flow.llm import LLMSchemaError, LLMTransportError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('CLAUDE_TMUX_API_KEY', raising=False)
    return TestClient(server.app)


def _chat(client, **body):
    body = {'model': 'claude-tmux/sonnet',
            'messages': [{'role': 'user', 'content': 'hi'}], **body}
    return client.post('/v1/chat/completions', json=body)


class TestModelNames:
    @pytest.mark.parametrize('requested,pane', [
        ('claude-tmux/opus', 'opus'), ('opus', 'opus'), ('sonnet', 'sonnet'),
        ('claude-tmux', None), ('claude-tmux/claude-sonnet-5-5', 'claude-sonnet-5-5'),
        # LiteLLM-style clients keep the provider prefix: that launched
        # `claude --model openai/claude-tmux/sonnet`, which Claude rejected.
        ('openai/claude-tmux/sonnet', 'sonnet'), ('openai/sonnet', 'sonnet'),
        ('openai/claude-tmux', None),
    ])
    def test_the_model_picks_the_pane(self, requested, pane):
        assert server.pane_model(requested) == pane


class TestMessages:
    def test_system_and_one_user_message(self):
        system, prompt = server.flatten([
            {'role': 'system', 'content': 'Be brief.'},
            {'role': 'user', 'content': 'Say hi.'}])
        assert system == 'Be brief.' and prompt == 'Say hi.'

    def test_content_parts_are_joined(self):
        _, prompt = server.flatten([{'role': 'user', 'content': [
            {'type': 'text', 'text': 'one'}, {'type': 'text', 'text': 'two'}]}])
        assert 'one' in prompt and 'two' in prompt

    def test_a_conversation_becomes_a_transcript_ending_on_the_last_turn(self):
        """Each request is a fresh pane conversation (/clear), so earlier turns
        have to travel in the prompt."""
        _, prompt = server.flatten([
            {'role': 'user', 'content': 'My dog is Rex.'},
            {'role': 'assistant', 'content': 'Hi Rex!'},
            {'role': 'user', 'content': "What is my dog's name?"}])
        assert prompt.index('My dog is Rex.') < prompt.index('Hi Rex!') \
            < prompt.index("What is my dog's name?")
        assert 'last user message' in prompt


class TestChatCompletions:
    def test_returns_an_openai_chat_completion(self, client):
        with patch.object(claude_tmux, 'complete', return_value='Hello.') as pane:
            response = _chat(client)
        assert response.status_code == 200
        body = response.json()
        assert body['object'] == 'chat.completion'
        assert body['model'] == 'claude-tmux/sonnet'
        assert body['choices'][0]['message'] == {'role': 'assistant', 'content': 'Hello.'}
        assert body['choices'][0]['finish_reason'] == 'stop'
        assert set(body['usage']) >= {'prompt_tokens', 'completion_tokens', 'total_tokens'}
        assert pane.call_args.kwargs['model'] == 'sonnet'
        assert pane.call_args.kwargs['fmt'] == 'text'
        # Server requests get the Warmbly-enabled panes.
        assert pane.call_args.kwargs['warmbly'] is True

    def test_json_schema_response_format_is_passed_to_the_pane(self, client):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}}}
        with patch.object(claude_tmux, 'complete', return_value='{"n": 1}') as pane:
            _chat(client, response_format={'type': 'json_schema', 'json_schema': {
                'name': 'Count', 'schema': schema, 'strict': True}})
        kw = pane.call_args.kwargs
        assert kw['fmt'] == 'json' and kw['schema'] == schema and kw['name'] == 'Count'

    def test_json_object_response_format_asks_for_json(self, client):
        with patch.object(claude_tmux, 'complete', return_value='{}') as pane:
            _chat(client, response_format={'type': 'json_object'})
        assert pane.call_args.kwargs['fmt'] == 'json'
        assert pane.call_args.kwargs['schema'] is None

    def test_streaming_sends_server_sent_events(self, client):
        with patch.object(claude_tmux, 'complete', return_value='Hello.'):
            response = _chat(client, stream=True)
        lines = [l for l in response.text.splitlines() if l.startswith('data: ')]
        assert lines[-1] == 'data: [DONE]'
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        assert ''.join(c['choices'][0]['delta'].get('content', '') for c in chunks) == 'Hello.'
        assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'


class TestErrors:
    def test_a_pane_that_does_not_answer_is_503(self, client):
        """Retryable: LiteLLM and OpenAI clients retry 503."""
        with patch.object(claude_tmux, 'complete',
                          side_effect=LLMTransportError('no answer within 900s')):
            response = _chat(client)
        assert response.status_code == 503
        assert 'no answer' in response.json()['error']['message']

    def test_a_usage_limit_is_429(self, client):
        with patch.object(claude_tmux, 'complete', side_effect=LLMTransportError(
                "claude in tmux session 'x' hit a usage limit: You've hit your limit")):
            response = _chat(client)
        assert response.status_code == 429

    def test_an_answer_that_never_validates_is_500(self, client):
        with patch.object(claude_tmux, 'complete',
                          side_effect=LLMSchemaError('Count not produced')):
            response = _chat(client)
        assert response.status_code == 500

    def test_no_messages_is_400(self, client):
        response = client.post('/v1/chat/completions',
                               json={'model': 'opus', 'messages': []})
        assert response.status_code == 400


class TestUnknownModel:
    def test_a_model_claude_rejects_is_404_not_a_timeout(self, client):
        with patch.object(claude_tmux, 'complete', side_effect=claude_tmux.PaneModelError(
                "claude in tmux session 'x' rejected the model 'gpt-9'")):
            response = _chat(client, model='gpt-9')
        assert response.status_code == 404
        assert response.json()['error']['type'] == 'model_not_found'


class TestAuth:
    def test_a_key_when_configured_is_required(self, monkeypatch):
        monkeypatch.setenv('CLAUDE_TMUX_API_KEY', 'sk-pane')
        client = TestClient(server.app)
        with patch.object(claude_tmux, 'complete', return_value='ok'):
            assert _chat(client).status_code == 401
            ok = client.post('/v1/chat/completions',
                             headers={'Authorization': 'Bearer sk-pane'},
                             json={'model': 'opus',
                                   'messages': [{'role': 'user', 'content': 'hi'}]})
        assert ok.status_code == 200


class TestModels:
    def test_lists_the_pane_models(self, client, monkeypatch):
        monkeypatch.setenv('CLAUDE_TMUX_MODELS', 'opus,sonnet')
        ids = [m['id'] for m in client.get('/v1/models').json()['data']]
        assert ids == ['claude-tmux/opus', 'claude-tmux/sonnet']


class TestUnprefixedRoutes:
    """Clients given a base URL without /v1 (LiteLLM's api_base, an OpenAI SDK
    base_url) call /chat/completions and /models; that was a 404."""

    def test_chat_completions_without_v1(self, client):
        with patch.object(claude_tmux, 'complete', return_value='Hello.'):
            response = client.post('/chat/completions', json={
                'model': 'sonnet', 'messages': [{'role': 'user', 'content': 'hi'}]})
        assert response.status_code == 200
        assert response.json()['choices'][0]['message']['content'] == 'Hello.'

    def test_models_without_v1(self, client):
        assert client.get('/models').status_code == 200
