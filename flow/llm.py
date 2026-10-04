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
import pathlib
import time
from typing import Any, Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

PROXY_URL = os.environ.get('LITELLM_BASE_URL', 'http://127.0.0.1:4000')
MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
REQUEST_TIMEOUT = float(os.environ.get('LLM_REQUEST_TIMEOUT', '120'))

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


# LiteLLM needs TWO credentials and they carry different things:
#   x-litellm-api-key  authenticates us to the proxy
#   Authorization      carries the Claude OAuth token the proxy forwards
#                      upstream to Anthropic
# Verified live: both together -> 200; the OAuth token alone -> rejected by the
# proxy; the proxy key alone -> "Missing Anthropic API Key". This is why
# Claude Code is configured with ANTHROPIC_CUSTOM_HEADERS: Authorization is
# already spoken for.
#
# The token is read per request, not cached, so a refresh performed by Claude
# Code is picked up without restarting a long-lived serving process.
DEFAULT_OAUTH_CREDENTIALS = str(
    pathlib.Path.home() / '.claude' / '.credentials.json'
)


def _oauth_credentials() -> dict:
    """The claudeAiOauth block, or {} if unreadable for any reason."""
    path = pathlib.Path(
        os.environ.get('CLAUDE_OAUTH_CREDENTIALS')
        or DEFAULT_OAUTH_CREDENTIALS
    ).expanduser()
    try:
        block = json.loads(path.read_text()).get('claudeAiOauth')
    except (OSError, ValueError):
        # Absent or malformed is a normal state, not an exception: a proxy may
        # hold the credential itself, in which case we send no Authorization.
        return {}
    return block if isinstance(block, dict) else {}


def oauth_access_token() -> Optional[str]:
    """The Claude OAuth access token, or None if there isn't one."""
    token = _oauth_credentials().get('accessToken')
    return token if isinstance(token, str) and token else None


def oauth_expires_in_seconds() -> Optional[int]:
    """Seconds until the token expires; negative if already expired."""
    expires_at = _oauth_credentials().get('expiresAt')
    if not isinstance(expires_at, (int, float)):
        return None
    return int(expires_at / 1000 - time.time())


def auth_header_name() -> str:
    """Which header carries the proxy key. Read per call, so it can be
    changed without restarting a long-lived serving process."""
    return os.environ.get('LITELLM_AUTH_HEADER') or LITELLM_AUTH_HEADER


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


def _request(messages: list, schema_model: Type[BaseModel]) -> str:
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get('LITELLM_API_KEY')
    if key:
        headers[auth_header_name()] = f'Bearer {key}'

    token = oauth_access_token()
    if token:
        headers['Authorization'] = f'Bearer {token}'
        remaining = oauth_expires_in_seconds()
        if remaining is not None and remaining <= 0:
            # Worth saying plainly: an expired token produces a 401 that looks
            # identical to a misconfiguration.
            print(f'WARNING: Claude OAuth token expired {-remaining}s ago; '
                  'refresh it (the proxy will reject this request)')

    body = {
        'model': MODEL,
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
              f'model={MODEL!r} headers={_safe_headers(headers)}')

    try:
        response = httpx.post(
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
            f'{MODEL!r}: {exc.response.text[:400]}'
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransportError(
            f'{type(exc).__name__} calling proxy for model {MODEL!r}: {exc}'
        ) from exc

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
