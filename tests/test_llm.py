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
        monkeypatch.setenv('LITELLM_API_KEY', 'sk-test')
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))) as post:
            complete_structured("describe", ResearchOutput)

        assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer sk-test'

    def test_returns_a_validated_model(self):
        with patch('flow.llm.httpx.post', return_value=_reply(json.dumps(VALID))):
            out = complete_structured("describe", ResearchOutput)

        assert isinstance(out, ResearchOutput)
        assert out.confidence == 0.8
