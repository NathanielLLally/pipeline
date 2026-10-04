import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from flow.llm import (
    LLMSchemaError,
    LLMTransportError,
    MODEL,
    complete_structured,
)
from flow.schemas import ResearchOutput

VALID = {
    "business_name": "Happy Tails", "pain_signals": ["no booking"],
    "personalization_hook": "phone only", "inferred_tone": "warm",
    "confidence": 0.8, "evidence": ["Call us"],
    "suggested_email": "owner@happytails.com",
}


def _reply(content):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    r.raise_for_status.return_value = None
    return r


class TestRequestShape:
    def test_asks_the_proxy_for_a_json_schema(self):
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        body = post.call_args.kwargs['json']
        assert body['model'] == MODEL
        assert body['response_format']['type'] == 'json_schema'
        assert 'properties' in body['response_format']['json_schema']['schema']

    def test_sends_the_api_key(self, monkeypatch):
        """In x-litellm-api-key; see TestProxyAuthHeader for why not Authorization."""
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-test')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        headers = post.call_args.kwargs['headers']
        assert headers['x-litellm-api-key'] == 'Bearer sk-test'

    def test_returns_a_validated_model(self):
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            out = complete_structured("describe", ResearchOutput)

        assert isinstance(out, ResearchOutput)
        assert out.confidence == 0.8


class TestMalformedReplies:
    def test_prose_reply_is_retried_then_raises(self):
        with patch('flow.llm.httpx.post', return_value=_reply("Sure! Here you go:")) as post:
            with pytest.raises(LLMSchemaError):
                complete_structured("describe", ResearchOutput, max_attempts=2)

        assert post.call_count == 2

    def test_valid_json_wrong_shape_is_retried_with_the_errors(self):
        bad = json.dumps(dict(VALID, inferred_tone="enthusiastic"))
        with patch('flow.llm.httpx.post', return_value=_reply(bad)) as post:
            with pytest.raises(LLMSchemaError):
                complete_structured("describe", ResearchOutput, max_attempts=3)

        assert post.call_count == 3
        # The retry must tell the model what was wrong, or it will repeat it.
        final_messages = post.call_args.kwargs['json']['messages']
        assert any('inferred_tone' in str(m['content']) for m in final_messages)

    def test_recovers_when_the_second_attempt_validates(self):
        bad = _reply(json.dumps(dict(VALID, confidence=5.0)))
        good = _reply(json.dumps(VALID))
        with patch('flow.llm.httpx.post', side_effect=[bad, good]) as post:
            out = complete_structured("describe", ResearchOutput)

        assert out.confidence == 0.8
        assert post.call_count == 2


class TestTransportFailures:
    def test_401_becomes_a_transport_error_not_a_schema_error(self):
        """The proxy 401s without a key; that is config, not a bad model."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        err = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = err

        with patch('flow.llm.httpx.post', return_value=resp):
            with pytest.raises(LLMTransportError) as caught:
                complete_structured("describe", ResearchOutput)

        assert '401' in str(caught.value)

    def test_timeout_becomes_a_transport_error(self):
        with patch('flow.llm.httpx.post',
                   side_effect=httpx.ReadTimeout("too slow")):
            with pytest.raises(LLMTransportError):
                complete_structured("describe", ResearchOutput)

    def test_transport_failure_is_not_retried_as_a_schema_problem(self):
        """Retrying a 401 three times just 401s three times."""
        with patch('flow.llm.httpx.post',
                   side_effect=httpx.ReadTimeout("too slow")) as post:
            with pytest.raises(LLMTransportError):
                complete_structured("describe", ResearchOutput, max_attempts=3)

        assert post.call_count == 1


class TestTransportErrorDetail:
    """A bare status code is useless; the body holds the reason."""

    def test_error_includes_the_response_body(self):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.text = ('{"error":{"message":"litellm.AuthenticationError: '
                     'Missing Anthropic API Key"}}')
        err = httpx.HTTPStatusError("401", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = err

        with patch('flow.llm.httpx.post', return_value=resp):
            with pytest.raises(LLMTransportError) as caught:
                complete_structured("describe", ResearchOutput)

        assert 'Missing Anthropic API Key' in str(caught.value)

    def test_error_names_the_model_that_failed(self):
        """Which model was attempted is the other half of the diagnosis."""
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 401
        resp.text = 'no key'
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=resp)

        with patch('flow.llm.httpx.post', return_value=resp):
            with pytest.raises(LLMTransportError) as caught:
                complete_structured("describe", ResearchOutput)

        assert MODEL in str(caught.value)


class TestStrictSchema:
    """OpenAI strict structured outputs reject a plain pydantic schema.

    It requires additionalProperties:false on every object and every property
    listed in `required`. Pydantic emits neither, so the proxy returns
    400 "'additionalProperties' is required to be supplied and to be false".
    """

    def test_sets_additional_properties_false(self):
        from flow.llm import strict_json_schema

        schema = strict_json_schema(ResearchOutput)

        assert schema['additionalProperties'] is False

    def test_requires_every_property_including_optional_ones(self):
        """Strict mode has no notion of optional; nullable != absent."""
        from flow.llm import strict_json_schema

        schema = strict_json_schema(ResearchOutput)

        assert set(schema['required']) == set(schema['properties'])
        assert 'next_url_to_check' in schema['required']

    def test_recurses_into_nested_object_definitions(self):
        from flow.llm import strict_json_schema

        schema = strict_json_schema(ResearchOutput)
        for definition in (schema.get('$defs') or {}).values():
            if definition.get('type') == 'object':
                assert definition['additionalProperties'] is False

    def test_the_request_sends_the_strict_schema(self):
        from flow.llm import strict_json_schema

        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        sent = post.call_args.kwargs['json']['response_format']['json_schema']['schema']
        assert sent == strict_json_schema(ResearchOutput)
        assert sent['additionalProperties'] is False


class TestProxyAuthHeader:
    """The proxy key goes in x-litellm-api-key, not Authorization.

    Authorization is reserved for the upstream provider credential, which
    LiteLLM passes through. Sending the proxy key there makes LiteLLM offer it
    to Anthropic as an Anthropic key and the call 401s with 'Missing Anthropic
    API Key' -- verified against the live proxy.
    """

    def test_key_goes_in_the_litellm_header(self, monkeypatch):
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-proxy')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        headers = post.call_args.kwargs['headers']
        assert headers['x-litellm-api-key'] == 'Bearer sk-proxy'

    def test_authorization_is_not_sent(self, monkeypatch):
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-proxy')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        assert 'Authorization' not in post.call_args.kwargs['headers']

    def test_header_name_is_configurable(self, monkeypatch):
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-proxy')
        monkeypatch.setenv('LITELLM_AUTH_HEADER', 'x-custom-key')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        assert post.call_args.kwargs['headers']['x-custom-key'] == 'Bearer sk-proxy'

    def test_no_auth_header_when_no_key_configured(self, monkeypatch):
        monkeypatch.delenv('LITELLM_API_KEY', raising=False)
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        assert 'x-litellm-api-key' not in post.call_args.kwargs['headers']


class TestRequestDebugLogging:
    """httpx/httpcore never log request headers at any level.

    Verified: with httpcore at DEBUG, zero log lines contain the header name
    or the token -- it logs `send_request_headers.started request=<Request>`,
    the object, not its contents. Response headers are logged in full, request
    headers never are. So seeing the outbound auth header requires printing it
    ourselves.
    """

    def test_quiet_unless_enabled(self, monkeypatch, capsys):
        monkeypatch.delenv('LLM_DEBUG', raising=False)
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-secret-value-here')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            complete_structured("describe", ResearchOutput)

        assert 'x-litellm-api-key' not in capsys.readouterr().out

    def test_shows_the_header_name_and_model(self, monkeypatch, capsys):
        monkeypatch.setenv('LLM_DEBUG', '1')
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-secret-value-here')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            complete_structured("describe", ResearchOutput)

        out = capsys.readouterr().out
        assert 'x-litellm-api-key' in out
        assert MODEL in out

    def test_never_prints_the_whole_key(self, monkeypatch, capsys):
        """The point is to confirm the header is right, not to leak it."""
        monkeypatch.setenv('LLM_DEBUG', '1')
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-secret-value-here')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            complete_structured("describe", ResearchOutput)

        assert 'sk-secret-value-here' not in capsys.readouterr().out

    def test_shows_enough_to_diagnose_the_header(self, monkeypatch, capsys):
        """Bearer prefix and length are what distinguish the failure modes."""
        monkeypatch.setenv('LLM_DEBUG', '1')
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-secret-value-here')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            complete_structured("describe", ResearchOutput)

        out = capsys.readouterr().out
        assert 'Bearer' in out
        assert 'len ' in out

    def test_reports_a_missing_key_rather_than_omitting_the_line(self, monkeypatch, capsys):
        monkeypatch.setenv('LLM_DEBUG', '1')
        monkeypatch.delenv('LITELLM_API_KEY', raising=False)
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            complete_structured("describe", ResearchOutput)

        assert 'LITELLM_API_KEY' in capsys.readouterr().out
