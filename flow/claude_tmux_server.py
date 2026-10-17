"""
An OpenAI-compatible endpoint in front of the Claude Code tmux panes.

    $ python flow/claude_tmux_server.py            # 127.0.0.1:4100

    POST /v1/chat/completions   model 'claude-tmux/opus', 'opus', 'sonnet', ...
    GET  /v1/models
    (also without the /v1 prefix)

Any OpenAI client -- or the LiteLLM proxy, as an `openai/` model with
api_base http://127.0.0.1:4100/v1 -- can then use the panes. Each request goes
through flow.claude_tmux.complete: its own pane per model, /clear before every
request, the answer handed over in a file.

  response_format    json_schema -> JSON checked against the schema;
                     json_object -> any JSON; absent -> plain text
  stream             answered whole, then sent as server-sent events
  usage              always 0: the panes report no token counts per request

Errors follow OpenAI's shape. A pane that does not answer (timeout, stuck,
dead) is 503 and a usage limit 429, both of which clients retry; a model
Claude Code rejects is 404; an answer that never validates is 500.

Requests here go to Warmbly-enabled panes ('<pane>-warmbly'): Claude may
call the Warmbly API with curl, using WARMBLY_API_URL and WARMBLY_API_TOKEN
from this process's environment (see claude_tmux.warmbly_launch), read and
write. The pipeline's own panes have no such access.

Each request is a fresh pane conversation, so a multi-turn `messages` list
travels as a transcript in the prompt. Tools, images and sampling parameters
(temperature, max_tokens, ...) are not supported and are ignored.

Environment:
  CLAUDE_TMUX_HOST / CLAUDE_TMUX_PORT   bind address (127.0.0.1 / 4100)
  CLAUDE_TMUX_API_KEY  if set, required as 'Authorization: Bearer <key>' (or
                       x-api-key / x-litellm-api-key)
  CLAUDE_TMUX_MODELS   what /v1/models lists (default 'opus,sonnet,haiku')
  and everything flow/claude_tmux.py reads (CLAUDE_TMUX_CONFIG_DIR, ...).
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from flow import claude_tmux
from flow.llm import CLAUDE_TMUX_MODEL, LLMSchemaError, LLMTransportError

app = FastAPI(title='claude-tmux')


def pane_model(requested: str) -> Optional[str]:
    """
    The pane's model from a requested name: 'claude-tmux/opus', 'opus' and
    LiteLLM-style 'openai/claude-tmux/opus' -> 'opus'; 'claude-tmux' -> None,
    the plain pane on Claude's default model. Provider prefixes go: kept,
    'openai/claude-tmux/sonnet' reached `claude --model` and was rejected.
    """
    parts = [p for p in (requested or '').strip().split('/') if p]
    if CLAUDE_TMUX_MODEL in parts:
        parts = parts[parts.index(CLAUDE_TMUX_MODEL) + 1:]
    elif parts and parts[0] == 'openai':
        parts = parts[1:]
    return '/'.join(parts) or None


def _text(content: Any) -> str:
    """A message's content: a string, or OpenAI content parts."""
    if isinstance(content, str):
        return content
    return '\n'.join(part.get('text', '') for part in content or []
                     if isinstance(part, dict) and part.get('type') == 'text')


def flatten(messages: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    """
    (system prompt, prompt). One user message is the prompt itself; a
    conversation becomes a transcript, since each request is a fresh pane
    conversation and cannot see earlier turns otherwise.
    """
    system = '\n\n'.join(_text(m.get('content')) for m in messages
                         if m.get('role') in ('system', 'developer')) or None
    turns = [m for m in messages if m.get('role') not in ('system', 'developer')]
    if len(turns) == 1 and turns[0].get('role') == 'user':
        return system, _text(turns[0].get('content'))
    transcript = [f"## {m.get('role', 'user').capitalize()}\n\n{_text(m.get('content'))}"
                  for m in turns]
    return system, ('The conversation so far:\n\n' + '\n\n'.join(transcript)
                    + '\n\nReply as the assistant to the last user message.')


def _response_format(body: Dict[str, Any]) -> Tuple[str, Optional[dict], str]:
    """(fmt, schema, name) for claude_tmux.complete."""
    rf = body.get('response_format') or {}
    if rf.get('type') == 'json_schema':
        spec = rf.get('json_schema') or {}
        return 'json', spec.get('schema'), spec.get('name') or 'answer'
    if rf.get('type') == 'json_object':
        return 'json', None, 'answer'
    return 'text', None, 'answer'


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        'error': {'message': message, 'type': kind, 'code': status}})


def _authorized(request: Request) -> bool:
    key = os.environ.get('CLAUDE_TMUX_API_KEY', '').strip()
    if not key:
        return True
    given = [request.headers.get('authorization', ''),
             request.headers.get('x-api-key', ''),
             request.headers.get('x-litellm-api-key', '')]
    return any(g.removeprefix('Bearer ').strip() == key for g in given if g)


def _completion(model: str, content: str) -> Dict[str, Any]:
    return {
        'id': f'chatcmpl-{uuid.uuid4().hex}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    }


def _stream(model: str, content: str):
    """The whole answer as OpenAI chat.completion.chunk events."""
    base = {'id': f'chatcmpl-{uuid.uuid4().hex}', 'object': 'chat.completion.chunk',
            'created': int(time.time()), 'model': model}
    for delta, finish in (({'role': 'assistant'}, None),
                          ({'content': content}, None), ({}, 'stop')):
        chunk = {**base, 'choices': [{'index': 0, 'delta': delta,
                                      'finish_reason': finish}]}
        yield f'data: {json.dumps(chunk)}\n\n'
    yield 'data: [DONE]\n\n'


# Also without /v1: LiteLLM's api_base and OpenAI SDK base_urls are often
# configured without it, and then call /chat/completions.
@app.post('/v1/chat/completions')
@app.post('/chat/completions')
async def chat_completions(request: Request):
    if not _authorized(request):
        return _error(401, 'invalid or missing API key', 'authentication_error')
    try:
        body = await request.json()
    except ValueError:
        return _error(400, 'body is not JSON', 'invalid_request_error')
    messages = body.get('messages') or []
    if not messages:
        return _error(400, 'messages is required', 'invalid_request_error')

    model = body.get('model') or CLAUDE_TMUX_MODEL
    system, prompt = flatten(messages)
    fmt, schema, name = _response_format(body)
    try:
        # Blocking -- tmux and the answer wait -- so off the event loop; the
        # pane's own lock makes concurrent requests to one model take turns.
        content = await run_in_threadpool(
            claude_tmux.complete, prompt, system=system, model=pane_model(model),
            fmt=fmt, schema=schema, name=name,
            # Server requests may call the Warmbly API; the pipeline's never.
            warmbly=True)
    except claude_tmux.PaneModelError as exc:
        return _error(404, str(exc), 'model_not_found')
    except LLMTransportError as exc:
        if 'usage limit' in str(exc):
            return _error(429, str(exc), 'rate_limit_error')
        return _error(503, str(exc), 'service_unavailable')
    except LLMSchemaError as exc:
        return _error(500, str(exc), 'invalid_response')

    if body.get('stream'):
        return StreamingResponse(_stream(model, content),
                                 media_type='text/event-stream')
    return _completion(model, content)


@app.get('/v1/models')
@app.get('/models')
async def models():
    names = [m.strip() for m in
             (os.environ.get('CLAUDE_TMUX_MODELS') or 'opus,sonnet,haiku').split(',')
             if m.strip()]
    return {'object': 'list', 'data': [
        {'id': f'{CLAUDE_TMUX_MODEL}/{m}', 'object': 'model', 'created': 0,
         'owned_by': 'claude-tmux'} for m in names]}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=os.environ.get('CLAUDE_TMUX_HOST', '127.0.0.1'),
                port=int(os.environ.get('CLAUDE_TMUX_PORT', '4100')))
