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
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

PROXY_URL = os.environ.get('LITELLM_BASE_URL', 'http://127.0.0.1:4000')
MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
REQUEST_TIMEOUT = float(os.environ.get('LLM_REQUEST_TIMEOUT', '120'))
# httpx's timeout bounds each socket read, not the call, so a proxy that
# trickles bytes or holds the connection open can outlast it indefinitely.
# This caps the whole call.
CALL_TIMEOUT = float(os.environ.get('LLM_CALL_TIMEOUT', '180'))

# The proxy key goes in its own header, not Authorization. LiteLLM reserves
# Authorization for the UPSTREAM provider credential and passes it through, so
# putting the proxy key there makes it offer our proxy key to Anthropic as an
# Anthropic key -- which fails with "Missing Anthropic API Key". This is the
# same header Claude Code is configured with via ANTHROPIC_CUSTOM_HEADERS.
LITELLM_AUTH_HEADER = os.environ.get(
    'LITELLM_AUTH_HEADER', 'x-litellm-api-key'
)


# Request-level debug, off unless LLM_DEBUG is truthy.
#
# httpx and httpcore never log request headers at any level -- httpcore emits
# `send_request_headers.started request=<Request [b'POST']>`, the object, not
# its contents. It logs RESPONSE headers in full, which makes the omission
# easy to mistake for a configuration problem. Verified: with httpcore at
# DEBUG, zero log lines contain the header name or the token. So seeing the
# outbound auth header means printing it here.
#
# Read at call time, not import time, so it can be switched on without an
# import-order dance.
_REDACT = ('authorization', 'x-litellm-api-key', 'x-api-key', 'api-key')


def auth_header_name() -> str:
    """Which header carries the proxy key. Read per call, so it can be
    changed without restarting a long-lived serving process."""
    return os.environ.get('LITELLM_AUTH_HEADER') or LITELLM_AUTH_HEADER


def resolve_model(agent_env_var: Optional[str] = None) -> str:
    """
    Resolve the model to use for an LLM call. Read per call, so agents
    can select their own model without restarting a long-lived serving
    process.

    Args:
        agent_env_var: Name of an agent-specific env var (e.g. 'ANALYSIS_MODEL',
                       'DRAFTING_MODEL'). If set and not null/empty, takes
                       precedence over LLM_MODEL.

    Returns:
        The model name to use.
    """
    if agent_env_var:
        agent_model = os.environ.get(agent_env_var, '').strip()
        if agent_model:
            return agent_model
    return os.environ.get('LLM_MODEL', MODEL)


def _debug_enabled() -> bool:
    return os.environ.get('LLM_DEBUG', '').strip().lower() in (
        '1', 'true', 'yes', 'on'
    )


def _safe_headers(headers: dict) -> dict:
    """Headers with credentials shown as prefix, suffix and length only.

    Enough to tell apart the failure modes that matter -- wrong header name,
    missing `Bearer ` prefix, empty or truncated value -- without writing a
    credential into a log.
    """
    safe = {}
    for name, value in headers.items():
        if name.lower() in _REDACT and len(value) > 18:
            safe[name] = f'{value[:11]}\u2026{value[-4:]} (len {len(value)})'
        elif name.lower() in _REDACT:
            safe[name] = f'<short value, len {len(value)}>'
        else:
            safe[name] = value
    return safe


class LLMTransportError(RuntimeError):
    """The proxy could not be reached, refused us, or timed out."""


class LLMSchemaError(ValueError):
    """The model answered, but never in the shape we asked for."""


def _strictify(node: Any) -> Any:
    """Recursively impose OpenAI strict-mode rules on a JSON schema node."""
    if isinstance(node, dict):
        node = {k: _strictify(v) for k, v in node.items()}
        if node.get('type') == 'object' or 'properties' in node:
            node['additionalProperties'] = False
            # Strict mode has no optional properties: every key must be listed
            # in `required`. Optionality is expressed by the value being
            # nullable, not by the key being absent -- which is why pydantic's
            # Optional[...] fields still appear here.
            node['required'] = list((node.get('properties') or {}).keys())
        return node
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def strict_json_schema(schema_model: Type[BaseModel]) -> dict:
    """
    Pydantic's schema, adjusted for OpenAI strict structured outputs.

    Without this the proxy rejects the request with
    400 "Invalid schema for response_format: 'additionalProperties' is
    required to be supplied and to be false". Pydantic has no reason to emit
    that -- it is an OpenAI constraint, not a JSON Schema one.
    """
    return _strictify(schema_model.model_json_schema())


def _post_with_deadline(url: str, **kwargs: Any) -> httpx.Response:
    """
    httpx.post, abandoned after CALL_TIMEOUT.

    The call runs on a daemon thread: the caller is released at the deadline,
    and an abandoned call cannot hold the process open at exit (a
    ThreadPoolExecutor worker would, until the stuck request ended).
    """
    outcome: dict = {}
    done = threading.Event()

    def call() -> None:
        try:
            outcome['response'] = httpx.post(url, **kwargs)
        except BaseException as exc:
            outcome['error'] = exc
        finally:
            done.set()

    threading.Thread(target=call, name='llm-call', daemon=True).start()
    if not done.wait(CALL_TIMEOUT):
        raise FutureTimeout()
    if 'error' in outcome:
        raise outcome['error']
    return outcome['response']


def _request(
    messages: list,
    schema_model: Type[BaseModel],
    model: Optional[str] = None,
) -> str:
    """
    Make a request to the LLM proxy for a structured response.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        schema_model: Pydantic model to validate the response against.
        model: Model name to use. If not provided, resolve_model() determines it.
    """
    if not model:
        model = resolve_model()

    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LITELLM_API_KEY')
    if key:
        headers[auth_header_name()] = f'Bearer {key}'

    body = {
        'model': model,
        'messages': messages,
        'response_format': {
            'type': 'json_schema',
            'json_schema': {
                'name': schema_model.__name__,
                'schema': strict_json_schema(schema_model),
                'strict': True,
            },
        },
    }

    if _debug_enabled():
        if not key:
            print('LLM_DEBUG: LITELLM_API_KEY is NOT set; sending no auth header')
        print(f'LLM_DEBUG: POST {PROXY_URL}/v1/chat/completions '
              f'model={model!r} headers={_safe_headers(headers)}')

    try:
        response = _post_with_deadline(
            f'{PROXY_URL}/v1/chat/completions',
            json=body, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # The body carries the actual reason and the status alone is useless:
        # a 401 from this proxy can mean "your key is wrong" or "the proxy has
        # no upstream key for the provider this model routes to", which are
        # entirely different problems. Naming the model matters for the same
        # reason -- one model can 401 while another on the same key works.
        raise LLMTransportError(
            f'proxy returned {exc.response.status_code} for model '
            f'{model!r}: {exc.response.text[:400]}'
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransportError(
            f'{type(exc).__name__} calling proxy for model {model!r}: {exc}'
        ) from exc
    except FutureTimeout as exc:
        raise LLMTransportError(
            f'no reply from proxy for model {model!r} within '
            f'LLM_CALL_TIMEOUT={CALL_TIMEOUT:g}s'
        ) from exc

    return response.json()['choices'][0]['message']['content']


def complete_structured(
    prompt: str,
    schema_model: Type[BaseModel],
    system: Optional[str] = None,
    max_attempts: int = 2,
    model: Optional[str] = None,
    agent_model_env: Optional[str] = None,
) -> Any:
    """
    Ask the model for an instance of schema_model and validate it.

    On a validation failure the model is asked again with its own errors
    attached, up to max_attempts. Bounded deliberately: an unbounded retry
    against a model that cannot produce the shape spends money forever.

    Args:
        prompt: The user prompt to send.
        schema_model: Pydantic model to validate the response against.
        system: Optional system prompt.
        max_attempts: Max retries on validation failure (default: 2).
        model: Explicit model name. If provided, takes precedence over
               agent_model_env and environment defaults.
        agent_model_env: Name of an agent-specific env var (e.g. 'ANALYSIS_MODEL').
                         Used only if model is not explicitly provided.

    Returns:
        An instance of schema_model.
    """
    # Determine which model to use
    if not model:
        model = resolve_model(agent_model_env)

    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    last_error = None
    for attempt in range(1, max_attempts + 1):
        content = _request(messages, schema_model, model=model)

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
