"""
Structured answers from an interactive Claude Code session, driven over tmux.

Set RESEARCH_MODEL / DRAFTING_MODEL (or LLM_MODEL) to flow.llm.CLAUDE_TMUX_MODEL
('claude-tmux') and flow.llm.complete_structured hands each call here instead
of to the proxy. Everything around the call -- research's escalation, the
page and pool checks, write-backs, hold_to_recipient -- runs unchanged.

One request:
  1. The system prompt, the prompt and the strict JSON schema go into
     data/handoff/<id>.request.md. Never into the pane: a research prompt
     carries the whole crawl, uncapped by policy.
  2. The pane gets '/clear' -- so one business's details cannot leak into
     the next one's research -- and then one line naming the request file
     and the answer path.
  3. The answer file is the completion signal, not the transcript. An answer
     that does not validate is asked for again with its errors, up to
     max_attempts.

Failures map onto the proxy's: no answer by the deadline, a dead session or a
stuck permission prompt is LLMTransportError (llm_unavailable, retryable); only
an answer that never validates is LLMSchemaError.

The tmux mechanics come from the aiTeam POC (hermes_claude_tmux_poc.py), minus
Hermes: the keystrokes are fixed, so an LLM driving tmux would add cost and
nondeterminism, and Hermes imports only under its own interpreter. The
keystrokes all go through send_line(), the one place to swap a driver in.

Environment, read per call:
  CLAUDE_TMUX_SESSION  tmux session name (default 'pipeline-claude'); created
                       and Claude launched in it if absent. With a model, the
                       pane is '<session>-<model>' and Claude runs with
                       --model <model>: one pane per model, each with its own
                       lock, so panes for different models work at once. Every
                       name ends in a tag for the checkout's handoff directory
                       (checkout_tag), e.g. 'pipeline-claude-opus-3fa2c1':
                       each checkout gets its own panes.
  <AGENT>_CLAUDE_MODEL the model an agent's pane runs, read via
                       flow.llm.agent_setting: RESEARCH_CLAUDE_MODEL=opus,
                       DRAFTING_CLAUDE_MODEL=sonnet. Unset: the plain pane.
  CLAUDE_TMUX_TIMEOUT  seconds to wait for one answer (default 900)
  CLAUDE_TMUX_CWD      where Claude runs (default: the handoff directory, so
                       no repo CLAUDE.md or git status is in its context).
                       The handoff directory must be inside it, for
                       acceptEdits to cover the answer file without a
                       permission prompt.
  CLAUDE_TMUX_ARGS     extra arguments for `claude`, e.g. '--model opus'
  CLAUDE_TMUX_CONFIG_DIR  a separate Claude profile for the panes (log in
                       once: CLAUDE_CONFIG_DIR=<dir> claude). Measured
                       2026-10-17: it synced 30 plugin skills, not 121, and
                       the first-turn cache write fell 17.2k -> 11.6-13.4k
                       tokens. Logged into another subscription, the batch
                       also draws on that account's limits.
"""

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Tuple, Type

import jsonschema
from pydantic import BaseModel, ValidationError

from flow.artifacts import artifact_dir
from flow.llm import (CLAUDE_TMUX_MODEL, LLMSchemaError, LLMTransportError,
                      record_call, strict_json_schema)

_now = time.monotonic
_sleep = time.sleep

# One pane, one conversation: requests take turns. A file lock, not a thread
# lock: served deployments run each flow in its own process. flock is per open
# file, so threads in one process, each opening the file, take turns too.
LOCK_FILE = '.pane.lock'

POLL_SECONDS = 3.0
LAUNCH_TIMEOUT = 60.0
# How long a file that is there but not yet JSON may stay that way before it
# counts as the answer: Claude may still be writing it.
PARTIAL_GRACE = 10.0
# Enters to try before giving up on a line; the answer wait then times out.
SUBMIT_TRIES = 3
# Claude draws its input box before it is done starting: keys sent then are
# typed and their Enter lost. The aiTeam POC waited ~8s too.
LAUNCH_SETTLE = 8.0

PROMPT_READY = '❯'
# The footer of a Claude launched by launch_command. The prompt glyph alone is
# not enough: many shell themes use it, and a session whose Claude exited
# would take keystrokes meant for Claude.
CLAUDE_FOOTER = 'accept edits on'
BUSY = 'esc to interrupt'
# The pane's Claude loads the repo's CLAUDE.md, which says to run gh and the
# tests. acceptEdits does not cover those, so it would stop on a prompt.
DISALLOWED_TOOLS = ('Bash', 'WebFetch', 'WebSearch')
# Every /clear starts a conversation whose per-session context the prompt
# cache must write again. These shrink it: the system prompt's per-machine
# sections move out so it caches across sessions, and no MCP servers load --
# the pane needs none. Measured 2026-10-17: first-turn cache write 23.5k ->
# 17.2k tokens. Not --disable-slash-commands: it disables /clear as well.
CACHE_FLAGS = ('--exclude-dynamic-system-prompt-sections', '--strict-mcp-config')
# The plan's usage limit, reached. Past it every request would sit out the
# whole answer timeout, and a batch has hundreds. Not the warning before it --
# "You've used 90% of your session limit" -- after which Claude still answers.
USAGE_LIMIT = re.compile(r"hit your (usage |session |weekly )?limit|"
                         r"(usage|session|weekly) limit reached",
                         re.IGNORECASE)
TRUST_DIALOG = re.compile(r'trust (the files in )?this folder', re.IGNORECASE)
# Panes for the OpenAI-compatible server may call the Warmbly API, through
# scripts/warmbly-api and jq only -- anything else stops on a prompt and fails
# the request. Not curl: Claude Code will not auto-approve a command holding
# a $VARIABLE, so the agent could not pass the token, and raw curl could send
# it anywhere. The wrapper reads the credentials, sourced into Claude's
# environment, itself. The pipeline's panes never get this: website text in
# a research prompt must not be able to steer API calls.
WARMBLY_ENV = ('WARMBLY_API_URL', 'WARMBLY_API_TOKEN')
WARMBLY_BIN = str(Path(__file__).resolve().parent.parent / 'scripts')
WARMBLY_TOOLS = ('Bash(warmbly-api:*)', 'Bash(jq:*)')
WARMBLY_SPEC = os.environ.get(
    'WARMBLY_OPENAPI_SPEC',
    str(Path.home() / 'src' / 'git' / 'warmbly' / 'docs' / 'public' / 'openapi.json'))
WARMBLY_GUIDE = """# Warmbly API access

You can call the Warmbly API for this user's account with the
`warmbly-api` command in Bash. It is already authenticated:

    warmbly-api GET /campaigns
    warmbly-api GET '/contacts?limit=5'
    warmbly-api POST /contacts '{{"email": "a@b.co"}}'
    warmbly-api PATCH /campaigns/<id> '{{"name": "New name"}}'

It prints the response body, then `HTTP <status>` on its own line. Paths are
under /v1 (it adds the prefix). Pipe into jq to pick fields:
`warmbly-api GET /campaigns | jq '.data[] | {{name, status}}'`.

Reference: the OpenAPI spec at `{spec}`. It is large (1.2 MB): Grep it for
the path or schema you need, then Read just that part. Its paths are relative
to `/v1`.

Rules:
- Only `warmbly-api` and `jq` are permitted in Bash. Any other command --
  curl, shell variables, scripts -- stops the request. Write JSON bodies
  literally in single quotes.
- The credentials are handled for you; never print or look for the token.
- Reading and changing data are both allowed. Delete things, start or resume
  campaigns, or send email only when the request explicitly asks for it.
- When done, write your reply to the answer file as the request says: what
  you did and what the API returned, in plain words.
"""

# Claude Code's reply to a --model it does not know. It answers nothing else,
# so without this the request sat out the whole answer timeout.
MODEL_REJECTED = re.compile(r"issue with the selected model \(([^)]*)\)",
                            re.IGNORECASE)
PERMISSION_PROMPT = re.compile(r'Do you want to (proceed|make this edit|create)',
                               re.IGNORECASE)


class PaneModelError(LLMTransportError):
    """Claude Code rejected the pane's --model. Retrying cannot help; it
    is a transport error so the agents' existing handling still applies."""


def checkout_tag() -> str:
    """
    Six hex characters naming this checkout's handoff directory. A pane runs
    in one handoff directory and may write answers only there, so two
    checkouts must not share one: a pane started from a worktree stopped on a
    permission prompt for every request from the main checkout.
    """
    return hashlib.sha1(str(handoff_dir().resolve()).encode()).hexdigest()[:6]


def session_name(model: Optional[str] = None, warmbly: bool = False) -> str:
    """The pane for `model`: one per model, so research on one model need
    not wait for drafting on another. No model: the plain session. Warmbly
    panes (see warmbly_launch) are separate sessions again. Each checkout
    has its own panes (checkout_tag)."""
    base = os.environ.get('CLAUDE_TMUX_SESSION') or 'pipeline-claude'
    name = f"{base}-{re.sub(r'[^A-Za-z0-9]+', '-', model)}" if model else base
    if warmbly:
        name = f'{name}-warmbly'
    return f'{name}-{checkout_tag()}'


def answer_timeout() -> float:
    try:
        return float(os.environ.get('CLAUDE_TMUX_TIMEOUT') or 900)
    except ValueError:
        return 900.0


def handoff_dir() -> Path:
    return artifact_dir() / 'handoff'


def _tmux(*args: str) -> Any:
    return subprocess.run(['tmux', *args], capture_output=True, text=True)


def has_session(session: str) -> bool:
    return _tmux('has-session', '-t', session).returncode == 0


def capture(session: str) -> str:
    return _tmux('capture-pane', '-t', session, '-p', '-S', '-40').stdout or ''


def input_box(pane: str) -> str:
    """The text between the last two horizontal rules: Claude's input box."""
    rules = [i for i, line in enumerate(pane.splitlines())
             if line.strip().startswith('\u2500' * 10)]
    if len(rules) < 2:
        return ''
    lines = pane.splitlines()[rules[-2] + 1:rules[-1]]
    return '\n'.join(lines)


def unsubmitted(pane: str, text: str) -> bool:
    """
    True if `text` still sits in the input box. Whitespace is ignored on
    both sides: the box wraps long lines mid-word. A placeholder suggestion
    in an empty box does not match the text.
    """
    box = re.sub(r'\s+', '', input_box(pane))
    head = re.sub(r'\s+', '', text)[:40]
    return bool(head) and head in box


def send_line(session: str, text: str) -> None:
    """
    Type `text` and submit it. The text is sent literally (-l) and Enter in a
    separate call: one send-keys carrying both is taken as a paste and never
    submits. An Enter can still be lost -- keys sent while Claude was busy
    starting were typed and never submitted -- so Enter is pressed again
    while the text sits in the input box.
    """
    _tmux('send-keys', '-t', session, '-l', text)
    for _ in range(SUBMIT_TRIES):
        _sleep(1)
        _tmux('send-keys', '-t', session, 'Enter')
        _sleep(1)
        if not unsubmitted(capture(session), text):
            return
    print(f'claude-tmux: {text[:60]!r} still unsubmitted in {session!r}')


def warmbly_launch() -> Tuple[str, str]:
    """
    The extra launch arguments for a pane that may call the Warmbly API, and
    the files they name: the credentials in a mode-600 env file the shell
    sources -- never on the command line, where tmux would show the token --
    and the guide appended to Claude's system prompt.
    """
    missing = [v for v in WARMBLY_ENV if not os.environ.get(v)]
    if missing:
        raise LLMTransportError(
            f"Warmbly pane needs {', '.join(missing)} in the environment")
    directory = handoff_dir()
    directory.mkdir(parents=True, exist_ok=True)
    env_file = directory / '.warmbly.env'
    env_file.touch(mode=0o600)
    env_file.chmod(0o600)
    env_file.write_text(''.join(f'export {v}={shlex.quote(os.environ[v])}\n'
                                for v in WARMBLY_ENV)
                        + f'export PATH={shlex.quote(WARMBLY_BIN)}:"$PATH"\n')
    guide = directory / '.warmbly-guide.md'
    guide.write_text(WARMBLY_GUIDE.format(spec=WARMBLY_SPEC))
    return (f". {shlex.quote(str(env_file))}",
            f"--allowedTools {' '.join(shlex.quote(t) for t in WARMBLY_TOOLS)} "
            f"--add-dir {shlex.quote(str(Path(WARMBLY_SPEC).parent))} "
            f"--append-system-prompt-file {shlex.quote(str(guide))}")


def launch_command(cwd: str, model: Optional[str] = None,
                   warmbly: bool = False) -> str:
    """acceptEdits: writing the answer file must not stop on a prompt."""
    extra = os.environ.get('CLAUDE_TMUX_ARGS', '').strip()
    # On the command itself: a tmux pane has the tmux server's environment,
    # not this process's.
    config = os.environ.get('CLAUDE_TMUX_CONFIG_DIR', '').strip()
    profile = f'CLAUDE_CONFIG_DIR={shlex.quote(config)} ' if config else ''
    source, tools = warmbly_launch() if warmbly else ('', '')
    # A Warmbly pane runs curl through Bash; the pipeline's never needs it.
    disallowed = [t for t in DISALLOWED_TOOLS if not (warmbly and t == 'Bash')]
    return (f'cd {shlex.quote(cwd)} && '
            + (f'{source} && ' if source else '')
            + f'{profile}claude --permission-mode acceptEdits '
            f"--disallowedTools {' '.join(disallowed)} "
            f"{' '.join(CACHE_FLAGS)}"
            + (f' {tools}' if tools else '')
            + (f' --model {shlex.quote(model)}' if model else '')
            + (f' {extra}' if extra else ''))


def _check_pane(session: str, pane: str) -> None:
    """Raise for a pane that will not answer: a permission prompt, or the
    plan's usage limit."""
    if PERMISSION_PROMPT.search(pane):
        raise LLMTransportError(
            f'claude in tmux session {session!r} is stopped on a '
            f'permission prompt')
    rejected = MODEL_REJECTED.search(pane)
    if rejected:
        raise PaneModelError(
            f'claude in tmux session {session!r} rejected the model '
            f'{rejected.group(1)!r}')
    match = USAGE_LIMIT.search(pane)
    if match:
        line = next(l for l in pane.splitlines() if match.group(0) in l)
        raise LLMTransportError(
            f'claude in tmux session {session!r} hit a usage limit: '
            f'{line.strip()[:200]}')


def _wait_ready(session: str, timeout: float) -> None:
    """Until the input box is idle; accepts the workspace-trust dialog once."""
    deadline = _now() + timeout
    trusted = False
    while True:
        pane = capture(session)
        if TRUST_DIALOG.search(pane) and not trusted:
            _tmux('send-keys', '-t', session, 'Enter')
            trusted = True
        elif PERMISSION_PROMPT.search(pane) or USAGE_LIMIT.search(pane) \
                or MODEL_REJECTED.search(pane):
            _check_pane(session, pane)
        elif PROMPT_READY in pane and CLAUDE_FOOTER in pane and BUSY not in pane:
            return
        if _now() >= deadline:
            raise LLMTransportError(
                f'claude in tmux session {session!r} not ready within '
                f'{timeout:g}s')
        _sleep(POLL_SECONDS)


def ensure_session(session: str, model: Optional[str] = None,
                   warmbly: bool = False) -> None:
    """Start tmux and Claude in it unless the session already exists."""
    if has_session(session):
        return
    # The handoff directory, not the repo: there the pane's context has no
    # CLAUDE.md or git status, which made every request after /clear re-write
    # ~23.5k tokens to the prompt cache, and no instructions to run gh.
    # (_ask has already created it.)
    cwd = os.environ.get('CLAUDE_TMUX_CWD') or str(handoff_dir())
    launch = launch_command(cwd, model, warmbly)
    _tmux('new-session', '-d', '-s', session, '-x', '200', '-y', '50')
    _tmux('send-keys', '-t', session, launch, 'Enter')
    _sleep(LAUNCH_SETTLE)
    _wait_ready(session, LAUNCH_TIMEOUT)


def recover(session: str) -> None:
    """
    After a failed request: Escape declines a permission prompt or interrupts
    a turn still running, so the next request does not inherit the pane
    stuck. Best effort -- the failure being reported matters more.
    """
    for _ in range(3):
        _tmux('send-keys', '-t', session, 'Escape')
        _sleep(1)
        pane = capture(session)
        if PROMPT_READY in pane and CLAUDE_FOOTER in pane and BUSY not in pane \
                and not PERMISSION_PROMPT.search(pane):
            return
    print(f'claude-tmux: pane {session!r} still not idle after Escape')


@contextmanager
def pane_lock(session: str) -> Iterator[None]:
    """One lock per pane: requests to the same pane take turns."""
    directory = handoff_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f'.{session}{LOCK_FILE}', 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_request(prompt: str, system: Optional[str], answer_path: Path,
                  errors: Optional[str] = None, fmt: str = 'json',
                  schema: Optional[dict] = None,
                  name: str = 'answer', warmbly: bool = False) -> str:
    """
    The request file's text. Pure, so it can be asserted on.

    fmt 'json' asks for a JSON object, matching `schema` when one is given;
    fmt 'text' asks for the reply itself, as plain text.
    """
    if fmt == 'text':
        what = (f'write ONLY your reply, as plain text, to `{answer_path}` -- '
                'no preamble, no notes about the request.')
    else:
        what = (f'write ONLY a JSON object{" matching the schema" if schema else ""} '
                f'to `{answer_path}` -- no prose, no code fence.')
    parts = [
        f'# {name} request',
        '',
        'You are answering for an automated pipeline. Follow the system '
        f'prompt and the prompt below, then {what} '
        + ('You may call the Warmbly API with curl, as your system prompt '
           'describes; otherwise use only the text given here and no memory '
           'of earlier requests. ' if warmbly else
           'Use only the text given here: no web, no other files, no memory '
           'of earlier requests. ')
        + 'Invent no person, email address or quote.',
        '',
        '## System prompt',
        '',
        system or '(none)',
        '',
        '## Prompt',
        '',
        prompt,
    ]
    if schema is not None:
        parts += ['', '## JSON schema', '', '```json',
                  json.dumps(schema, indent=2), '```']
    if errors:
        parts += ['', '## Your previous answer did not validate', '', errors,
                  '', 'Write a corrected answer to the path above.']
    return '\n'.join(parts) + '\n'


def instruction(request_path: Path, answer_path: Path,
                fmt: str = 'json') -> str:
    """The one line typed into the pane."""
    what = 'the JSON answer' if fmt == 'json' else 'your reply'
    return (f'Read {request_path} and do what it says: write only {what} '
            f'to {answer_path} and nothing else.')


def _strip_fence(text: str) -> str:
    match = re.fullmatch(r'\s*```(?:json)?\s*(.*?)\s*```\s*', text, re.DOTALL)
    return match.group(1) if match else text


def _await_answer(session: str, answer_path: Path, timeout: float,
                  fmt: str = 'json') -> str:
    """
    The answer file's text. JSON: once it parses, or after PARTIAL_GRACE if
    it does not. Text: as soon as the file is there -- Claude's Write tool
    writes it whole.
    """
    deadline = _now() + timeout
    unparsed_since: Optional[float] = None
    while True:
        if answer_path.exists():
            if fmt == 'text':
                return answer_path.read_text().strip()
            text = _strip_fence(answer_path.read_text())
            try:
                json.loads(text)
                return text
            except json.JSONDecodeError:
                unparsed_since = unparsed_since if unparsed_since is not None \
                    else _now()
                if _now() - unparsed_since >= PARTIAL_GRACE:
                    return text
        else:
            _check_pane(session, capture(session))
        if _now() >= deadline:
            raise LLMTransportError(
                f'no answer from claude in tmux session {session!r} within '
                f'CLAUDE_TMUX_TIMEOUT={timeout:g}s ({answer_path})')
        _sleep(POLL_SECONDS)


def _ask(session: str, prompt: str, system: Optional[str],
         errors: Optional[str], model: Optional[str] = None,
         fmt: str = 'json', schema: Optional[dict] = None,
         name: str = 'answer', warmbly: bool = False) -> str:
    """One request to the pane and its answer's text. Caller holds the lock."""
    directory = handoff_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{re.sub(r'[^a-z0-9]+', '-', name.lower())}-{uuid.uuid4().hex[:12]}"
    request_path = directory / f'{stem}.request.md'
    answer_path = directory / f"{stem}.answer.{'json' if fmt == 'json' else 'txt'}"
    request_path.write_text(build_request(prompt, system, answer_path, errors,
                                          fmt=fmt, schema=schema, name=name,
                                          warmbly=warmbly))

    ensure_session(session, model, warmbly)
    _wait_ready(session, LAUNCH_TIMEOUT)
    send_line(session, '/clear')
    _wait_ready(session, LAUNCH_TIMEOUT)
    send_line(session, instruction(request_path, answer_path, fmt))
    # Tallied as 'claude-tmux/<model>': one provider, a line per model.
    tally = f'{CLAUDE_TMUX_MODEL}/{model}' if model else CLAUDE_TMUX_MODEL
    try:
        text = _await_answer(session, answer_path, answer_timeout(), fmt)
    except LLMTransportError:
        record_call(tally, ok=False)
        recover(session)
        raise
    record_call(tally, ok=bool(text.strip()))
    return text


def _schema_errors(text: str, schema: Optional[dict]) -> Optional[str]:
    """Why `text` is not a JSON answer (matching `schema`), or None."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return f'That was not JSON: {text[:200]!r}'
    if schema is None:
        return None
    problems = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value),
                      key=lambda e: list(e.path))
    if not problems:
        return None
    return 'That did not validate against the schema:\n' + '\n'.join(
        f"- {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
        for e in problems[:20])


def complete(prompt: str, system: Optional[str] = None,
             model: Optional[str] = None, fmt: str = 'text',
             schema: Optional[dict] = None, name: str = 'answer',
             max_attempts: int = 2, warmbly: bool = False) -> str:
    """
    Claude's answer, as text, from the pane for `model`.

    fmt 'text': the reply as written. fmt 'json': a JSON object, checked
    against `schema` when given and asked for again with the errors, up to
    max_attempts. The entry point for flow/claude_tmux_server.py.

    warmbly: answer in a pane that may call the Warmbly API (warmbly_launch).
    """
    session = session_name(model, warmbly)
    errors: Optional[str] = None
    with pane_lock(session):
        for attempt in range(1, max_attempts + 1):
            text = _ask(session, prompt, system, errors, model,
                        fmt=fmt, schema=schema, name=name, warmbly=warmbly)
            if fmt == 'text':
                return text
            errors = _schema_errors(text, schema)
            if errors is None:
                return text
            print(f'claude-tmux answer did not validate (attempt {attempt}): '
                  f'{errors}')
    raise LLMSchemaError(
        f'{name} not produced in {max_attempts} attempts: {errors}')


def complete_structured(prompt: str, schema_model: Type[BaseModel],
                        system: Optional[str] = None,
                        max_attempts: int = 2,
                        model: Optional[str] = None) -> Any:
    """
    An instance of schema_model, answered by Claude in the tmux session for
    `model` (e.g. 'opus', 'sonnet'); with no model, the plain session and
    Claude's default model. Validated by the model itself, so its own
    validators (placeholders, payment terms) are enforced too.
    """
    session = session_name(model)
    schema = strict_json_schema(schema_model)
    errors: Optional[str] = None
    with pane_lock(session):
        for attempt in range(1, max_attempts + 1):
            text = _ask(session, prompt, system, errors, model, fmt='json',
                        schema=schema, name=schema_model.__name__)
            try:
                return schema_model.model_validate_json(text)
            except ValidationError as exc:
                errors = f'That did not validate against the schema:\n{exc}'
            except json.JSONDecodeError:
                errors = f'That was not JSON: {text[:200]!r}'
            print(f'claude-tmux answer did not validate (attempt {attempt}): '
                  f'{errors}')
    raise LLMSchemaError(
        f'{schema_model.__name__} not produced in {max_attempts} attempts: '
        f'{errors}')
