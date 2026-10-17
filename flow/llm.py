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
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Dict, Iterator, Optional, Tuple, Type

import httpx
from pydantic import BaseModel, ValidationError

PROXY_URL = os.environ.get('LITELLM_BASE_URL', 'http://127.0.0.1:4000')
MODEL = os.environ.get('LLM_MODEL', 'gpt-4o-mini')
REQUEST_TIMEOUT = float(os.environ.get('LLM_REQUEST_TIMEOUT', '120'))
# httpx's timeout bounds each socket read, not the call, so a proxy that
# trickles bytes or holds the connection open can outlast it indefinitely.
# This caps the whole call.
CALL_TIMEOUT = float(os.environ.get('LLM_CALL_TIMEOUT', '180'))

# Statuses worth another try: the upstream was overloaded, rate-limited or
# unreachable. The free nemotron model 503s with "Upstream error from Nvidia:
# Service temporarily overloaded" often enough to cost a business a batch.
# A 400/401/403/404 is configuration and fails the same way every time.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Setting LLM_MODEL or an agent's own *_MODEL variable to this value sends its
# calls through the multi-agent graph (flow/multi_agent.py) instead of to one
# model: route, specialist, critic, evaluator, frontier on failure.
MULTI_AGENT_MODEL = 'multi-agent'

# Setting LLM_MODEL or an agent's own *_MODEL variable to this value has an
# interactive Claude Code session answer its calls, over tmux
# (flow/claude_tmux.py), instead of the proxy.
CLAUDE_TMUX_MODEL = 'claude-tmux'

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


def agent_setting(agent_model_env: Optional[str], name: str) -> Optional[str]:
    """
    An agent's own setting, named after its model variable:
    agent_setting('RESEARCH_MODEL', 'TIER') reads RESEARCH_TIER. Read per call.
    None when unset or empty, or when there is no agent variable.
    """
    if not agent_model_env:
        return None
    prefix = agent_model_env[:-len('_MODEL')] if agent_model_env.endswith('_MODEL') \
        else agent_model_env
    return os.environ.get(f'{prefix}_{name}', '').strip() or None


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

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class LLMSchemaError(ValueError):
    """The model answered, but never in the shape we asked for."""


# Call accounting. count_calls() opens a scope -- run_agents opens one per batch
# -- and every proxy call made inside it is tallied by provider (the model
# name's prefix: 'ollama/gpt-oss:20b' -> 'ollama') and by model. A ContextVar
# rather than a global, so two batches in one serve process keep separate
# counts; the dict is shared by reference into the threads Prefect copies the
# context into, hence the lock.
_call_stats: ContextVar[Optional[dict]] = ContextVar('llm_call_stats', default=None)
_call_stats_lock = threading.Lock()


def provider_of(model: str) -> str:
    return model.split('/', 1)[0] if '/' in model else model


TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'cached_tokens')


def usage_tokens(usage: Optional[dict]) -> Dict[str, int]:
    """The TOKEN_FIELDS of an OpenAI-style `usage` block; 0 for what is absent.
    Ollama reports no reasoning_tokens -- its thinking is inside completion_tokens."""
    usage = usage or {}
    completion_details = usage.get('completion_tokens_details') or {}
    prompt_details = usage.get('prompt_tokens_details') or {}
    return {
        'prompt_tokens': int(usage.get('prompt_tokens') or 0),
        'completion_tokens': int(usage.get('completion_tokens') or 0),
        'reasoning_tokens': int(completion_details.get('reasoning_tokens') or 0),
        'cached_tokens': int(prompt_details.get('cached_tokens') or 0),
    }


@contextmanager
def count_calls() -> Iterator[dict]:
    """Yield {'by_provider': {...}, 'by_model': {...}}, each name mapped to
    {'calls', 'failed', *TOKEN_FIELDS}, filled in by the calls made inside the block."""
    stats: dict = {'by_provider': {}, 'by_model': {}}
    token = _call_stats.set(stats)
    try:
        yield stats
    finally:
        _call_stats.reset(token)


def record_call(model: str, ok: bool, usage: Optional[dict] = None) -> None:
    """Tally one proxy call, and its token usage, into the open count_calls() scope, if any."""
    stats = _call_stats.get()
    if stats is None:
        return
    tokens = usage_tokens(usage)
    with _call_stats_lock:
        for key, name in (('by_provider', provider_of(model)), ('by_model', model)):
            entry = stats[key].setdefault(
                name, {'calls': 0, 'failed': 0, **{f: 0 for f in TOKEN_FIELDS}})
            entry['calls'] += 1
            if not ok:
                entry['failed'] += 1
            for field, count in tokens.items():
                entry[field] += count


def format_call_stats(stats: Dict[str, dict]) -> str:
    providers = stats.get('by_provider') or {}
    if not providers:
        return 'llm calls: none'
    return 'llm calls: ' + ', '.join(
        f"{name}={entry['calls']} ({entry['failed']} failed, "
        f"{entry.get('prompt_tokens', 0) / 1000:.1f}k in / "
        f"{entry.get('completion_tokens', 0) / 1000:.1f}k out)"
        for name, entry in sorted(providers.items()))


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


def _post_with_deadline(url: str, deadline: Optional[float] = None,
                        **kwargs: Any) -> httpx.Response:
    """
    httpx.post, abandoned after `deadline` seconds (default CALL_TIMEOUT).

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
    if not done.wait(CALL_TIMEOUT if deadline is None else deadline):
        raise FutureTimeout()
    if 'error' in outcome:
        raise outcome['error']
    return outcome['response']


def is_transient(exc: BaseException) -> bool:
    """True for a transport failure another attempt may not repeat: an
    overload, rate-limit or gateway status, or no status at all (the
    deadline passed or the connection dropped)."""
    if not isinstance(exc, LLMTransportError):
        return False
    return exc.status_code is None or exc.status_code in TRANSIENT_STATUSES


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '').strip() or default)
    except ValueError:
        return default


def retry_delays() -> list:
    """
    Seconds to wait before each retry: LLM_RETRY_DELAY (15) doubling up to
    LLM_RETRY_MAX_DELAY (300), LLM_RETRIES (6) of them -- about 13 minutes of
    waiting in all. Bounded on purpose: a free model can stay overloaded for
    hours, and a waiting business holds one of the batch's worker slots.
    Read per call.
    """
    retries = max(0, int(_env_number('LLM_RETRIES', 6)))
    base = _env_number('LLM_RETRY_DELAY', 15)
    cap = _env_number('LLM_RETRY_MAX_DELAY', 300)
    return [min(base * 2 ** i, cap) for i in range(retries)]


def attempt_limits(attempt: int, base_deadline: Optional[float] = None
                   ) -> Tuple[float, float]:
    """
    (call deadline, per-read timeout) for attempt `attempt`, 1-based. Both
    double on every retry -- a model that ran out of time under load runs
    out again at the same limit -- up to LLM_MAX_CALL_TIMEOUT (900s). The
    read timeout grows too: it alone would otherwise end every attempt at
    LLM_REQUEST_TIMEOUT however long the deadline.
    """
    cap = _env_number('LLM_MAX_CALL_TIMEOUT', 900)
    factor = 2 ** max(0, attempt - 1)
    deadline = min((base_deadline or CALL_TIMEOUT) * factor, cap)
    return deadline, min(REQUEST_TIMEOUT * factor, deadline)


def retry_condition(task: Any, task_run: Any, state: Any) -> bool:
    """Prefect's retry_condition_fn: retry only what is_transient allows."""
    try:
        state.result(raise_on_failure=True)
    except BaseException as exc:
        return is_transient(exc)
    return False


def _in_prefect_run() -> bool:
    """Whether a flow run is active. Outside one a task call tries to start
    an ephemeral server, which this deployment does not allow."""
    try:
        from prefect.context import FlowRunContext
    except ImportError:
        return False
    return FlowRunContext.get() is not None


def _run_count() -> int:
    from prefect.runtime import task_run
    return int(task_run.run_count or 1)


def _attempt(messages: list, model: str, response_format: Optional[dict],
             base_url: Optional[str], api_key: Optional[str],
             deadline: Optional[float], timeout_name: Optional[str],
             request_timeout: Optional[float] = None,
             **params: Any) -> Tuple[str, float, Optional[dict]]:
    """One proxy call, tallied for count_calls(); an empty reply counts as
    failed. Each retry is its own attempt and its own tally."""
    try:
        content, cost, usage = _chat_completion(
            messages, model, response_format=response_format,
            base_url=base_url, api_key=api_key, deadline=deadline,
            timeout_name=timeout_name, request_timeout=request_timeout,
            **params)
    except BaseException:
        record_call(model, ok=False)
        raise
    record_call(model, ok=bool(content and content.strip()), usage=usage)
    return content, cost, usage


def _llm_call(messages: list, model: str, response_format: Optional[dict],
              base_url: Optional[str], api_key: Optional[str],
              deadline: Optional[float], timeout_name: Optional[str],
              params: dict) -> Tuple[str, float, Optional[dict]]:
    """The body of the llm-call task: this attempt's limits, then the call."""
    limit, read = attempt_limits(_run_count(), base_deadline=deadline)
    return _attempt(messages, model, response_format, base_url, api_key,
                    deadline=limit, timeout_name=timeout_name,
                    request_timeout=read, **params)


try:
    from prefect import task as _prefect_task
    from prefect.cache_policies import NO_CACHE

    # A task, so each attempt and each wait shows on the flow run in the UI.
    # NO_CACHE: the default policy keys on inputs and the flow run, and the
    # same prompt twice in one run must be two calls. persist_result=False:
    # the reply is already in the agent's own artifacts.
    _llm_call_task = _prefect_task(
        _llm_call, name='llm-call', task_run_name='llm-call {model}',
        cache_policy=NO_CACHE, persist_result=False, log_prints=True)
except ImportError:  # pragma: no cover -- prefect is a dependency
    _llm_call_task = None


def chat_completion(
    messages: list,
    model: str,
    response_format: Optional[dict] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    deadline: Optional[float] = None,
    timeout_name: Optional[str] = None,
    retry: bool = True,
    **params: Any,
) -> Tuple[str, float]:
    """
    One chat completion through the proxy. Returns (content, cost in USD).

    `deadline` caps the whole call in seconds (default LLM_CALL_TIMEOUT);
    `timeout_name` is what a timeout error calls that limit.

    The cost is the proxy's x-litellm-response-cost header, 0.0 when absent.
    `params` (temperature, max_tokens, ...) go into the request body as-is.
    Every call is tallied for count_calls(); an empty reply counts as failed.

    Inside a flow run the call is the llm-call Prefect task, retried on a
    transient failure (is_transient) after retry_delays(), each attempt
    with longer limits (attempt_limits). Outside one it is a single attempt,
    and so is `retry=False`, for a caller with its own failover
    (flow.multi_agent tries the next model and holds a run-wide deadline).
    """
    if retry and _llm_call_task is not None and _in_prefect_run():
        delays = retry_delays()
        call = _llm_call_task.with_options(
            retries=len(delays), retry_delay_seconds=delays,
            retry_jitter_factor=0.2, retry_condition_fn=retry_condition)
        content, cost, _ = call(messages, model, response_format, base_url,
                                api_key, deadline, timeout_name, params)
        return content, cost
    content, cost, _ = _attempt(messages, model, response_format, base_url,
                                api_key, deadline, timeout_name, **params)
    return content, cost


def _chat_completion(
    messages: list,
    model: str,
    response_format: Optional[dict],
    base_url: Optional[str],
    api_key: Optional[str],
    deadline: Optional[float],
    timeout_name: Optional[str],
    request_timeout: Optional[float] = None,
    **params: Any,
) -> Tuple[str, float, Optional[dict]]:
    base_url = base_url or PROXY_URL
    if api_key is None:
        api_key = os.environ.get('LITELLM_API_KEY')

    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers[auth_header_name()] = f'Bearer {api_key}'

    body = {'model': model, 'messages': messages, **params}
    if response_format:
        body['response_format'] = response_format

    if _debug_enabled():
        if not api_key:
            print('LLM_DEBUG: LITELLM_API_KEY is NOT set; sending no auth header')
        print(f'LLM_DEBUG: POST {base_url}/v1/chat/completions '
              f'model={model!r} headers={_safe_headers(headers)}')

    limit = CALL_TIMEOUT if deadline is None else deadline
    try:
        response = _post_with_deadline(
            f'{base_url}/v1/chat/completions', deadline=deadline,
            json=body, headers=headers,
            timeout=request_timeout if request_timeout is not None
            else min(REQUEST_TIMEOUT, limit) if deadline is not None
            else REQUEST_TIMEOUT,
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
            f'{model!r}: {exc.response.text[:400]}',
            status_code=exc.response.status_code,
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMTransportError(
            f'{type(exc).__name__} calling proxy for model {model!r}: {exc}'
        ) from exc
    except FutureTimeout as exc:
        limit_name = timeout_name or ('LLM_CALL_TIMEOUT' if deadline is None
                                      else 'the call deadline')
        raise LLMTransportError(
            f'no reply from proxy for model {model!r} within '
            f'{limit_name}={limit:g}s'
        ) from exc

    payload = response.json()
    content = payload['choices'][0]['message']['content']
    try:
        headers = getattr(response, 'headers', None) or {}
        cost = float(headers.get('x-litellm-response-cost') or 0)
    except (TypeError, ValueError):
        cost = 0.0
    return content, cost, payload.get('usage') if isinstance(payload, dict) else None


def json_schema_format(schema_model: Type[BaseModel]) -> dict:
    """The response_format that asks for a strict instance of schema_model."""
    return {
        'type': 'json_schema',
        'json_schema': {
            'name': schema_model.__name__,
            'schema': strict_json_schema(schema_model),
            'strict': True,
        },
    }


def _request(
    messages: list,
    schema_model: Type[BaseModel],
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> str:
    """
    Make a request to the LLM proxy for a structured response.

    Args:
        messages: List of message dicts with 'role' and 'content'.
        schema_model: Pydantic model to validate the response against.
        model: Model name to use. If not provided, resolve_model() determines it.
        reasoning_effort: Sent as-is when given (low/medium/high).
    """
    content, _ = chat_completion(
        messages, model or resolve_model(),
        response_format=json_schema_format(schema_model),
        **({'reasoning_effort': reasoning_effort} if reasoning_effort else {}),
    )
    return content


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
                         Used only if model is not explicitly provided. Its
                         prefix also names the agent's other settings:
                         <PREFIX>_REASONING_EFFORT on every path, and
                         <PREFIX>_TIER on the multi-agent path.

    Returns:
        An instance of schema_model.
    """
    # Determine which model to use
    if not model:
        model = resolve_model(agent_model_env)

    reasoning_effort = agent_setting(agent_model_env, 'REASONING_EFFORT')

    if model == MULTI_AGENT_MODEL:
        from flow import multi_agent
        return multi_agent.complete_structured(
            prompt, schema_model, system=system, max_attempts=max_attempts,
            tier=agent_setting(agent_model_env, 'TIER'),
            reasoning_effort=reasoning_effort)

    if model == CLAUDE_TMUX_MODEL:
        from flow import claude_tmux
        # <PREFIX>_CLAUDE_MODEL picks the pane's model: RESEARCH_CLAUDE_MODEL=opus.
        return claude_tmux.complete_structured(
            prompt, schema_model, system=system, max_attempts=max_attempts,
            model=agent_setting(agent_model_env, 'CLAUDE_MODEL'))

    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    last_error = None
    for attempt in range(1, max_attempts + 1):
        content = _request(messages, schema_model, model=model,
                           reasoning_effort=reasoning_effort)

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
