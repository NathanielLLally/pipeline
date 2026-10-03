"""
The only module that talks to the LiteLLM proxy.

Calls go over HTTP to the self-hosted proxy, which owns provider routing and
keys. The `litellm` Python package is deliberately not used: the proxy is the
integration point, and `httpx` is already a dependency.

Structured output is requested with OpenAI-compatible
`response_format={"type": "json_schema", ...}` and validated with Pydantic.
No `instructor` -- see spec section 3.2.
"""

import json
import os
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

PROXY_URL = os.environ.get('LITELLM_BASE_URL', 'http://127.0.0.1:4000')
MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
REQUEST_TIMEOUT = float(os.environ.get('LLM_REQUEST_TIMEOUT', '120'))


class LLMTransportError(RuntimeError):
    """The proxy could not be reached, refused us, or timed out."""


class LLMSchemaError(ValueError):
    """The model answered, but never in the shape we asked for."""


def _request(messages: list, schema_model: Type[BaseModel]) -> str:
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LITELLM_API_KEY')
    if key:
        headers['Authorization'] = f'Bearer {key}'

    body = {
        'model': MODEL,
        'messages': messages,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {
                'name': schema_model.__name__,
                'schema': schema_model.model_json_schema(),
                'strict': True,
            },
        },
    }

    try:
        response = httpx.post(
            f'{PROXY_URL}/v1/chat/completions',
            json=body, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LLMTransportError(
            f'proxy returned {exc.response.status_code}'
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransportError(f'{type(exc).__name__}: {exc}') from exc

    return response.json()['choices'][0]['message']['content']


def complete_structured(
    prompt: str,
    schema_model: Type[BaseModel],
    system: Optional[str] = None,
    max_attempts: int = 2,
) -> Any:
    """
    Ask the model for an instance of schema_model and validate it.

    On a validation failure the model is asked again with its own errors
    attached, up to max_attempts. Bounded deliberately: an unbounded retry
    against a model that cannot produce the shape spends money forever.
    """
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    last_error = None
    for attempt in range(1, max_attempts + 1):
        content = _request(messages, schema_model)

        try:
            return schema_model.model_validate_json(content)
        except ValidationError as exc:
            last_error = exc
            print(f'schema validation failed (attempt {attempt}): {exc}')
        except json.JSONDecodeError as exc:
            last_error = exc
            print(f'response was not JSON (attempt {attempt}): {content[:200]}')

        messages.append({'role': 'assistant', 'content': content})
        messages.append({
            'role': 'user',
            'content': (
                'That did not validate against the schema. Errors:\n'
                f'{last_error}\nReturn only JSON matching the schema.'
            ),
        })

    raise LLMSchemaError(
        f'{schema_model.__name__} not produced in {max_attempts} attempts: '
        f'{last_error}'
    )
