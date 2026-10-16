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
                       and Claude launched in it if absent
  CLAUDE_TMUX_TIMEOUT  seconds to wait for one answer (default 900)
  CLAUDE_TMUX_CWD      where Claude runs (default: the repo root). The handoff
                       directory must be inside it, for acceptEdits to cover
                       the answer file without a permission prompt.
  CLAUDE_TMUX_ARGS     extra arguments for `claude`, e.g. '--model opus'
"""

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Type

from pydantic import BaseModel, ValidationError

from flow.artifacts import REPO_ROOT, artifact_dir
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
# The plan's usage limit, reached. Past it every request would sit out the
# whole answer timeout, and a batch has hundreds. Not the warning before it --
# "You've used 90% of your session limit" -- after which Claude still answers.
USAGE_LIMIT = re.compile(r"hit your (usage |session |weekly )?limit|"
                         r"(usage|session|weekly) limit reached",
                         re.IGNORECASE)
TRUST_DIALOG = re.compile(r'trust (the files in )?this folder', re.IGNORECASE)
PERMISSION_PROMPT = re.compile(r'Do you want to (proceed|make this edit|create)',
                               re.IGNORECASE)


def session_name() -> str:
    return os.environ.get('CLAUDE_TMUX_SESSION') or 'pipeline-claude'


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


def launch_command(cwd: str) -> str:
    """acceptEdits: writing the answer file must not stop on a prompt."""
    extra = os.environ.get('CLAUDE_TMUX_ARGS', '').strip()
    return (f'cd {shlex.quote(cwd)} && claude --permission-mode acceptEdits '
            f"--disallowedTools {' '.join(DISALLOWED_TOOLS)}"
            + (f' {extra}' if extra else ''))


def _check_pane(session: str, pane: str) -> None:
    """Raise for a pane that will not answer: a permission prompt, or the
    plan's usage limit."""
    if PERMISSION_PROMPT.search(pane):
        raise LLMTransportError(
            f'claude in tmux session {session!r} is stopped on a '
            f'permission prompt')
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
        elif PERMISSION_PROMPT.search(pane) or USAGE_LIMIT.search(pane):
            _check_pane(session, pane)
        elif PROMPT_READY in pane and CLAUDE_FOOTER in pane and BUSY not in pane:
            return
        if _now() >= deadline:
            raise LLMTransportError(
                f'claude in tmux session {session!r} not ready within '
                f'{timeout:g}s')
        _sleep(POLL_SECONDS)


def ensure_session(session: str) -> None:
    """Start tmux and Claude in it unless the session already exists."""
    if has_session(session):
        return
    cwd = os.environ.get('CLAUDE_TMUX_CWD') or str(REPO_ROOT)
    _tmux('new-session', '-d', '-s', session, '-x', '200', '-y', '50')
    _tmux('send-keys', '-t', session, launch_command(cwd), 'Enter')
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
def pane_lock() -> Iterator[None]:
    directory = handoff_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / LOCK_FILE, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_request(prompt: str, schema_model: Type[BaseModel],
                  system: Optional[str], answer_path: Path,
                  errors: Optional[str] = None) -> str:
    """The request file's text. Pure, so it can be asserted on."""
    schema = json.dumps(strict_json_schema(schema_model), indent=2)
    parts = [
        f'# {schema_model.__name__} request',
        '',
        'You are answering for an automated pipeline. Follow the system '
        'prompt and the prompt below, then write ONLY a JSON object matching '
        f'the schema to `{answer_path}` -- no prose, no code fence. Use only '
        'the text given here: no web, no other files, no memory of earlier '
        'requests. Invent no person, email address or quote.',
        '',
        '## System prompt',
        '',
        system or '(none)',
        '',
        '## Prompt',
        '',
        prompt,
        '',
        '## JSON schema',
        '',
        '```json',
        schema,
        '```',
    ]
    if errors:
        parts += ['', '## Your previous answer did not validate', '', errors,
                  '', 'Write a corrected answer to the path above.']
    return '\n'.join(parts) + '\n'


def instruction(request_path: Path, answer_path: Path) -> str:
    """The one line typed into the pane."""
    return (f'Read {request_path} and do what it says: write only the JSON '
            f'answer to {answer_path} and nothing else.')


def _strip_fence(text: str) -> str:
    match = re.fullmatch(r'\s*```(?:json)?\s*(.*?)\s*```\s*', text, re.DOTALL)
    return match.group(1) if match else text


def _await_answer(session: str, answer_path: Path, timeout: float) -> str:
    """The answer file's text once it parses, or after PARTIAL_GRACE if not."""
    deadline = _now() + timeout
    unparsed_since: Optional[float] = None
    while True:
        if answer_path.exists():
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


def _ask(session: str, prompt: str, schema_model: Type[BaseModel],
         system: Optional[str], errors: Optional[str]) -> str:
    directory = handoff_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = f'{schema_model.__name__.lower()}-{uuid.uuid4().hex[:12]}'
    request_path = directory / f'{stem}.request.md'
    answer_path = directory / f'{stem}.answer.json'
    request_path.write_text(
        build_request(prompt, schema_model, system, answer_path, errors))

    ensure_session(session)
    _wait_ready(session, LAUNCH_TIMEOUT)
    send_line(session, '/clear')
    _wait_ready(session, LAUNCH_TIMEOUT)
    send_line(session, instruction(request_path, answer_path))
    try:
        text = _await_answer(session, answer_path, answer_timeout())
    except LLMTransportError:
        record_call(CLAUDE_TMUX_MODEL, ok=False)
        recover(session)
        raise
    record_call(CLAUDE_TMUX_MODEL, ok=bool(text.strip()))
    return text


def complete_structured(prompt: str, schema_model: Type[BaseModel],
                        system: Optional[str] = None,
                        max_attempts: int = 2) -> Any:
    """An instance of schema_model, answered by Claude in the tmux session."""
    session = session_name()
    errors: Optional[str] = None
    with pane_lock():
        for attempt in range(1, max_attempts + 1):
            text = _ask(session, prompt, schema_model, system, errors)
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
