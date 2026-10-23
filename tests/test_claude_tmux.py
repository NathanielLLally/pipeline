import json
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow import claude_tmux

# Saved before the autouse fixture replaces subprocess.run for every test.
REAL_RUN = subprocess.run
from flow.llm import (CLAUDE_TMUX_MODEL, LLMSchemaError, LLMTransportError,
                      complete_structured)


class Answer(BaseModel):
    name: str
    score: float


FOOTER = '\n  ⏵⏵ accept edits on (shift+tab to cycle)'
READY_PANE = 'Claude Code\n❯ ' + FOOTER
BUSY_PANE = 'Thinking… (esc to interrupt)\n❯ ' + FOOTER
PERMISSION_PANE = 'Do you want to proceed?\n❯ 1. Yes\n  2. No'
SHELL_PANE = 'nathaniel@host ~/src ❯ '


class FakeTmux:
    """
    Stands in for subprocess.run on tmux commands. `on_submit` is called with
    the line typed before each Enter, so a test can play Claude and write the
    answer file the instruction names.
    """

    def __init__(self, panes=None, on_submit=None, has_session=True):
        self.calls: List[list] = []
        self.panes = list(panes or [])
        self.on_submit = on_submit
        self.has_session = has_session
        self.typed: List[str] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        result = type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        if cmd[:2] == ['tmux', 'has-session']:
            result.returncode = 0 if self.has_session else 1
        elif cmd[:2] == ['tmux', 'new-session']:
            self.has_session = True
        elif cmd[:2] == ['tmux', 'capture-pane']:
            result.stdout = self.panes.pop(0) if len(self.panes) > 1 \
                else (self.panes[0] if self.panes else READY_PANE)
        elif cmd[:2] == ['tmux', 'send-keys'] and '-l' in cmd:
            self.typed.append(cmd[-1])
        elif cmd[:2] == ['tmux', 'send-keys'] and cmd[-1] == 'Enter':
            if self.on_submit and self.typed:
                self.on_submit(self.typed[-1])
        return result


def answer_path_in(line: str) -> Path:
    """The answer path the instruction names: the token ending .answer.json."""
    return Path(next(t for t in line.split() if t.endswith('.answer.json')))


def writes(*answers):
    """on_submit that writes each answer in turn to the path the line names."""
    queue = list(answers)

    def submit(line):
        if '.answer.json' in line and queue:
            answer_path_in(line).write_text(queue.pop(0))
    return submit


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
    monkeypatch.setenv('CLAUDE_TMUX_SESSION', 'test-claude')
    monkeypatch.setenv('CLAUDE_TMUX_TIMEOUT', '30')
    # Fake clock: every sleep advances it, so deadlines pass without waiting.
    clock = {'now': 0.0}
    monkeypatch.setattr(claude_tmux, '_now', lambda: clock['now'])
    monkeypatch.setattr(claude_tmux, '_sleep',
                        lambda s: clock.__setitem__('now', clock['now'] + s))
    # A test that reaches real tmux would start a real Claude session.
    monkeypatch.setattr(claude_tmux.subprocess, 'run', _no_real_tmux)
    return clock


def S(*parts):
    """A session name as claude_tmux builds it: base, parts, checkout tag."""
    return '-'.join(['test-claude', *parts, claude_tmux.checkout_tag()])


def _no_real_tmux(cmd, **kwargs):
    raise AssertionError(f'real subprocess.run reached: {cmd}')


def _ask(**kwargs):
    return claude_tmux.complete_structured(
        'Business: Happy Tails\n' + 'crawl text ' * 500, Answer,
        system='You research businesses.', **kwargs)


def _run(tmux, **kwargs):
    with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
        return _ask(**kwargs)


class TestRequestFile:
    def test_prompt_system_and_schema_go_in_a_file_not_the_pane(self, tmp_path):
        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)

        requests = list((tmp_path / 'handoff').glob('*.request.md'))
        assert len(requests) == 1
        text = requests[0].read_text()
        assert 'You research businesses.' in text
        assert 'Business: Happy Tails' in text
        assert '"additionalProperties": false' in text
        # The pane gets one short line, never the crawl text.
        assert all('crawl text' not in t for t in tmux.typed)
        assert any(str(requests[0]) in t for t in tmux.typed)


class TestKeystrokes:
    def test_text_is_literal_and_enter_is_a_separate_call(self):
        """One send-keys carrying text and Enter is taken as a paste and
        never submits (found by the aiTeam tmux POC)."""
        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)

        sends = [c for c in tmux.calls if c[:2] == ['tmux', 'send-keys']]
        for cmd in sends:
            assert not ('-l' in cmd and 'Enter' in cmd[cmd.index('-l') + 2:])
        instruction = next(i for i, c in enumerate(sends)
                           if '-l' in c and '.answer.json' in c[-1])
        assert sends[instruction + 1][-1] == 'Enter'

    def test_context_is_cleared_before_each_request(self):
        """One business's details must not leak into the next one's research."""
        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        assert tmux.typed.index('/clear') < next(
            i for i, t in enumerate(tmux.typed) if '.answer.json' in t)


RULE = '─' * 80
UNSUBMITTED_PANE = '\n'.join([
    RULE,
    '❯ Read /home/x/data/handoff/smoke-8507cf6153f4.request.md and do what',
    '  it says: write only the JSON answer to /home/x/data/han',
    '  doff/smoke-8507cf6153f4.answer.json and nothing else.',
    RULE,
    '  ⏵⏵ accept edits on (shift+tab to cycle)',
])
TYPED = ('Read /home/x/data/handoff/smoke-8507cf6153f4.request.md and do what '
         'it says: write only the JSON answer to /home/x/data/handoff/'
         'smoke-8507cf6153f4.answer.json and nothing else.')


class TestSubmission:
    def test_text_left_in_the_input_box_is_unsubmitted(self):
        assert claude_tmux.unsubmitted(UNSUBMITTED_PANE, TYPED)

    def test_an_empty_box_or_a_placeholder_is_submitted(self):
        empty = UNSUBMITTED_PANE.replace(UNSUBMITTED_PANE.split(RULE)[1],
                                         '\n❯ Try "edit research.py to..."\n')
        assert not claude_tmux.unsubmitted(empty, TYPED)

    def test_the_message_shown_above_the_box_is_not_the_box(self):
        sent = f'❯ {TYPED}\n\n● Done.\n{RULE}\n❯ \n{RULE}\n  ⏵⏵ accept edits on'
        assert not claude_tmux.unsubmitted(sent, TYPED)

    def test_enter_is_pressed_again_until_the_box_empties(self):
        """Keys sent while Claude was still starting were typed but the
        Enter was lost, and the request sat in the box until the timeout."""
        tmux = FakeTmux(panes=[UNSUBMITTED_PANE, READY_PANE])
        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            claude_tmux.send_line(S(), TYPED)
        enters = [c for c in tmux.calls if c == ['tmux', 'send-keys', '-t',
                                                 S(), 'Enter']]
        assert len(enters) == 2


class TestSession:
    def test_starts_claude_in_a_new_session_when_none_exists(self, tmp_path):
        tmux = FakeTmux(has_session=False,
                        on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)

        new = next(c for c in tmux.calls if c[:2] == ['tmux', 'new-session'])
        assert S() in new
        launch = ' '.join(next(c for c in tmux.calls
                               if c[:2] == ['tmux', 'send-keys'] and
                               any('claude ' in str(x) for x in c)))
        assert '--permission-mode acceptEdits' in launch
        # The pane's Claude loads the repo CLAUDE.md, which says to run gh and
        # the tests; acceptEdits does not cover Bash, so it would stop there.
        assert '--disallowedTools' in launch
        for tool in ('Bash', 'WebFetch', 'WebSearch'):
            assert tool in launch

    def test_claude_runs_in_the_handoff_directory_by_default(self, tmp_path):
        """Outside the repo, the pane's context carries no CLAUDE.md or git
        status: ~23.5k tokens every request re-wrote to the prompt cache."""
        tmux = FakeTmux(has_session=False,
                        on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        launch = ' '.join(next(c for c in tmux.calls
                               if c[:2] == ['tmux', 'send-keys'] and
                               any('claude ' in str(x) for x in c)))
        assert launch.split('&&')[0].strip().endswith(str(tmp_path / 'handoff'))

    def test_launch_trims_what_the_prompt_cache_must_rewrite_per_session(self):
        """Measured: the first turn's cache write fell 23.5k -> 17.2k tokens.
        Not --disable-slash-commands: it disables /clear too, and then every
        business lands in one conversation."""
        launch = claude_tmux.launch_command('/x')
        assert '--exclude-dynamic-system-prompt-sections' in launch
        assert '--strict-mcp-config' in launch
        assert '--disable-slash-commands' not in launch

    def test_a_config_dir_is_set_on_the_claude_command_itself(self, monkeypatch):
        """tmux panes inherit the tmux server's environment, not ours, so the
        profile must be on the command line."""
        monkeypatch.setenv('CLAUDE_TMUX_CONFIG_DIR', '/home/x/.claude pane')
        launch = claude_tmux.launch_command('/x')
        assert "&& CLAUDE_CONFIG_DIR='/home/x/.claude pane' claude " in launch

    def test_no_config_dir_leaves_the_default_profile(self, monkeypatch):
        monkeypatch.delenv('CLAUDE_TMUX_CONFIG_DIR', raising=False)
        assert 'CLAUDE_CONFIG_DIR' not in claude_tmux.launch_command('/x')

    def test_claude_tmux_cwd_overrides_the_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv('CLAUDE_TMUX_CWD', '/somewhere/else')
        tmux = FakeTmux(has_session=False,
                        on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        assert any('cd /somewhere/else &&' in str(x)
                   for c in tmux.calls for x in c)

    def test_reuses_a_running_session(self):
        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        assert not any(c[:2] == ['tmux', 'new-session'] for c in tmux.calls)

    def test_trust_dialog_is_accepted_once(self):
        tmux = FakeTmux(
            has_session=False,
            panes=['Do you trust the files in this folder?\n❯ 1. Yes, I trust this folder',
                   READY_PANE],
            on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        launch = next(i for i, c in enumerate(tmux.calls)
                      if c[:2] == ['tmux', 'send-keys'] and len(c) > 5)
        first_typed = next(i for i, c in enumerate(tmux.calls) if '-l' in c)
        bare_enter = ['tmux', 'send-keys', '-t', S(), 'Enter']
        assert bare_enter in tmux.calls[launch + 1:first_typed]

    def test_a_shell_prompt_is_not_claude(self):
        """Claude exited, the session lives on: typing into the shell would
        cost the whole answer timeout per business."""
        tmux = FakeTmux(panes=[SHELL_PANE],
                        on_submit=writes('{"name": "x", "score": 1}'))
        with pytest.raises(LLMTransportError, match='not ready'):
            _run(tmux)
        assert tmux.typed == []

    def test_waits_while_claude_is_busy(self):
        tmux = FakeTmux(panes=[BUSY_PANE, BUSY_PANE, READY_PANE],
                        on_submit=writes('{"name": "x", "score": 1}'))
        assert _run(tmux).name == 'x'


class TestAnswer:
    def test_valid_answer_is_returned_as_the_schema(self):
        tmux = FakeTmux(on_submit=writes('{"name": "Laura", "score": 0.8}'))
        result = _run(tmux)
        assert isinstance(result, Answer)
        assert result.name == 'Laura'

    def test_answer_in_a_code_fence_is_accepted(self):
        tmux = FakeTmux(on_submit=writes(
            '```json\n{"name": "Laura", "score": 0.8}\n```'))
        assert _run(tmux).name == 'Laura'

    def test_invalid_answer_is_requested_again_with_the_errors(self, tmp_path):
        tmux = FakeTmux(on_submit=writes('{"name": "x"}',
                                         '{"name": "x", "score": 2}'))
        result = _run(tmux, max_attempts=2)

        assert result.score == 2
        retry = sorted((tmp_path / 'handoff').glob('*.request.md'))
        assert len(retry) == 2
        assert any('score' in r.read_text() and 'did not validate' in r.read_text()
                   for r in retry)

    def test_still_invalid_after_max_attempts_is_a_schema_error(self):
        tmux = FakeTmux(on_submit=writes('{"name": "x"}', 'not json'))
        with pytest.raises(LLMSchemaError):
            _run(tmux, max_attempts=2)

    def test_no_answer_by_the_deadline_is_a_transport_error(self):
        """Never answering is 'llm_unavailable' -- retryable -- not a
        rejection of the business."""
        tmux = FakeTmux(on_submit=None)
        with pytest.raises(LLMTransportError):
            _run(tmux)

    def test_a_permission_prompt_is_a_transport_error(self):
        tmux = FakeTmux(panes=[READY_PANE, READY_PANE, PERMISSION_PANE])
        with pytest.raises(LLMTransportError, match='permission'):
            _run(tmux)

    def test_a_usage_limit_fails_fast_instead_of_waiting_out_the_timeout(self, _env):
        """Past the plan limit every request would otherwise sit out
        CLAUDE_TMUX_TIMEOUT, and a batch has hundreds of them."""
        limit = "You've hit your session limit \u00b7 resets 1:50am\n\u276f " + FOOTER
        tmux = FakeTmux(panes=[READY_PANE] * 4 + [limit])
        with pytest.raises(LLMTransportError, match='limit'):
            _run(tmux)
        assert _env['now'] < 30

    def test_a_usage_warning_is_not_the_limit(self):
        """At 90% Claude warns and keeps answering; failing on the warning
        cost a 200-business batch its last 131 businesses."""
        warning = ("You've used 90% of your session limit \u00b7 resets 7am "
                   "(America/New_York) \u00b7 /upgrade\n\u276f " + FOOTER)
        tmux = FakeTmux(panes=[warning],
                        on_submit=writes('{"name": "x", "score": 1}'))
        assert _run(tmux).name == 'x'

    def test_a_stuck_request_does_not_block_the_next_one(self):
        """Left on its prompt, the pane would fail every later business."""
        submitted = []

        def submit(line):
            if '.answer.json' in line:
                submitted.append(line)
                if len(submitted) == 2:
                    answer_path_in(line).write_text('{"name": "y", "score": 1}')

        # ready, /clear went in, ready, instruction went in, then the prompt
        tmux = FakeTmux(panes=[READY_PANE] * 4 + [PERMISSION_PANE, READY_PANE],
                        on_submit=submit)
        with pytest.raises(LLMTransportError):
            _run(tmux)
        assert ['tmux', 'send-keys', '-t', S(), 'Escape'] in tmux.calls
        assert _run(tmux).name == 'y'


class TestSerialized:
    def test_concurrent_requests_take_turns_in_the_one_pane(self):
        """run_agents runs businesses on several threads; two at once in one
        pane would interleave their keystrokes."""
        inside = {'now': 0, 'max': 0}
        lock = threading.Lock()

        def submit(line):
            if '.answer.json' not in line:
                return
            with lock:
                inside['now'] += 1
                inside['max'] = max(inside['max'], inside['now'])
            time.sleep(0.05)  # real time: widen the window for an overlap
            answer_path_in(line).write_text('{"name": "x", "score": 1}')
            with lock:
                inside['now'] -= 1

        tmux = FakeTmux(on_submit=submit)
        results, failures = [], []

        def worker():
            try:
                results.append(_ask())
            except Exception as exc:
                failures.append(exc)

        # Patched once, here: a patch per thread would be undone by the
        # first thread to finish while the others still run.
        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert failures == []
        assert len(results) == 3
        assert inside['max'] == 1

    def test_the_lock_is_a_file_lock_so_it_spans_processes(self, tmp_path):
        """Served deployments run each flow in its own process; a thread
        lock would let research and drafting share the pane unguarded."""
        held = []
        real = claude_tmux.fcntl.flock

        def spy(fd, op):
            held.append(op)
            return real(fd, op)

        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        with patch.object(claude_tmux.fcntl, 'flock', side_effect=spy):
            _run(tmux)
        assert claude_tmux.fcntl.LOCK_EX in held


class TestPerModelPanes:
    def test_a_model_gets_its_own_pane_launched_with_that_model(self):
        tmux = FakeTmux(has_session=False,
                        on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux, model='sonnet')
        new = next(c for c in tmux.calls if c[:2] == ['tmux', 'new-session'])
        assert S('sonnet') in new
        launch = ' '.join(str(x) for c in tmux.calls for x in c
                          if 'claude ' in str(x))
        assert '--model sonnet' in launch
        assert all(c[c.index('-t') + 1] == S('sonnet')
                   for c in tmux.calls if '-t' in c)

    def test_no_model_keeps_the_plain_session(self):
        tmux = FakeTmux(has_session=False,
                        on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        launch = ' '.join(str(x) for c in tmux.calls for x in c
                          if 'claude ' in str(x))
        assert '--model' not in launch
        assert any(S() == c[c.index('-t') + 1]
                   for c in tmux.calls if '-t' in c)

    def test_panes_for_different_models_work_at_the_same_time(self):
        """Research on Opus need not wait for drafting on Sonnet: each pane
        has its own lock. Each fake Claude waits for the other to be
        mid-request; one shared lock would break the barrier."""
        both = threading.Barrier(2, timeout=5)

        def submit(line):
            if '.answer.json' in line:
                both.wait()
                answer_path_in(line).write_text('{"name": "x", "score": 1}')

        tmux = FakeTmux(on_submit=submit)
        failures = []

        def worker(model):
            try:
                _ask(model=model)
            except Exception as exc:
                failures.append(exc)

        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            threads = [threading.Thread(target=worker, args=(m,))
                       for m in ('opus', 'sonnet')]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert failures == []


class TestRouting:
    def test_the_agents_claude_model_picks_the_pane(self, monkeypatch):
        monkeypatch.setenv('DRAFTING_MODEL', CLAUDE_TMUX_MODEL)
        monkeypatch.setenv('DRAFTING_CLAUDE_MODEL', 'sonnet')
        with patch.object(claude_tmux, 'complete_structured',
                          return_value=Answer(name='x', score=1)) as handoff:
            complete_structured('p', Answer, agent_model_env='DRAFTING_MODEL')
        assert handoff.call_args.kwargs['model'] == 'sonnet'

    def test_no_claude_model_set_means_the_default_pane(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', CLAUDE_TMUX_MODEL)
        monkeypatch.delenv('RESEARCH_CLAUDE_MODEL', raising=False)
        with patch.object(claude_tmux, 'complete_structured',
                          return_value=Answer(name='x', score=1)) as handoff:
            complete_structured('p', Answer, agent_model_env='RESEARCH_MODEL')
        assert handoff.call_args.kwargs['model'] is None

    def test_llm_complete_structured_hands_claude_tmux_to_this_module(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', CLAUDE_TMUX_MODEL)
        with patch.object(claude_tmux, 'complete_structured',
                          return_value=Answer(name='x', score=1)) as handoff, \
             patch('flow.llm.chat_completion') as proxy:
            result = complete_structured('p', Answer, system='s',
                                         agent_model_env='RESEARCH_MODEL')
        assert result.name == 'x'
        proxy.assert_not_called()
        assert handoff.call_args.kwargs['system'] == 's'


class TestComplete:
    """claude_tmux.complete: the raw-text entry point the OpenAI-compatible
    server uses -- free text, any JSON, or JSON matching a schema dict."""

    def _complete(self, tmux, **kwargs):
        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            return claude_tmux.complete('Say hello.', system='Be brief.', **kwargs)

    def test_text_mode_returns_the_reply_as_written(self, tmp_path):
        def submit(line):
            if '.answer.' in line:
                path = Path(next(t for t in line.split() if '.answer.' in t))
                path.write_text('Hello there.\n')
        tmux = FakeTmux(on_submit=submit)
        assert self._complete(tmux) == 'Hello there.'
        request = next((tmp_path / 'handoff').glob('*.request.md')).read_text()
        assert 'Be brief.' in request and 'Say hello.' in request
        assert 'JSON' not in request.split('## System prompt')[0]

    def test_text_mode_answer_file_is_not_json(self, tmp_path):
        seen = []

        def submit(line):
            if '.answer.' in line:
                path = Path(next(t for t in line.split() if '.answer.' in t))
                seen.append(path.name)
                path.write_text('plain words')
        tmux = FakeTmux(on_submit=submit)
        self._complete(tmux)
        assert seen and not seen[0].endswith('.json')

    def test_json_schema_mode_validates_against_the_schema(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}},
                  'required': ['n'], 'additionalProperties': False}
        tmux = FakeTmux(on_submit=writes('{"n": "two"}', '{"n": 2}'))
        text = self._complete(tmux, fmt='json', schema=schema, name='Count')
        assert json.loads(text) == {'n': 2}

    def test_json_schema_mode_gives_up_with_a_schema_error(self):
        schema = {'type': 'object', 'properties': {'n': {'type': 'integer'}},
                  'required': ['n']}
        tmux = FakeTmux(on_submit=writes('{"n": "x"}', '{"m": 1}'))
        with pytest.raises(LLMSchemaError):
            self._complete(tmux, fmt='json', schema=schema, max_attempts=2)

    def test_json_mode_without_a_schema_needs_only_json(self):
        tmux = FakeTmux(on_submit=writes('{"anything": [1, 2]}'))
        assert json.loads(self._complete(tmux, fmt='json')) == {'anything': [1, 2]}


class TestRejectedModel:
    def test_a_model_claude_rejects_fails_fast(self, _env):
        """Claude Code answered 'There's an issue with the selected model' and
        the request sat out CLAUDE_TMUX_TIMEOUT (15 minutes)."""
        rejected = ("\u25cf There's an issue with the selected model "
                    "(openai/claude-tmux/sonnet). It may not exist or you may not "
                    "have access to it. Run /model to pick a different model.\n"
                    "\u276f " + FOOTER)
        tmux = FakeTmux(panes=[READY_PANE] * 4 + [rejected])
        with pytest.raises(claude_tmux.PaneModelError, match='model'):
            _run(tmux, model='bogus')
        assert _env['now'] < 30

    def test_it_is_a_transport_error_for_the_pipeline(self):
        """The agents catch LLMTransportError; this must not escape them."""
        assert issubclass(claude_tmux.PaneModelError, LLMTransportError)


class TestWarmblyPane:
    """Panes for the OpenAI-compatible server can call the Warmbly API: their
    own sessions, curl allowed, credentials in the environment. The
    pipeline's panes stay locked down."""

    @pytest.fixture(autouse=True)
    def _creds(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wmbly_secret_value')

    def test_its_own_session(self):
        assert claude_tmux.session_name('sonnet', warmbly=True) == S('sonnet', 'warmbly')
        assert claude_tmux.session_name('sonnet') == S('sonnet')

    def test_launch_allows_the_wrapper_and_keeps_the_token_off_the_command_line(self):
        launch = claude_tmux.launch_command('/x', 'sonnet', warmbly=True)
        assert 'wmbly_secret_value' not in launch
        # Claude Code will not auto-approve a command holding a $VARIABLE, so
        # the agent calls a wrapper that reads the credentials itself, and
        # raw curl -- able to send the token anywhere -- is not allowed.
        assert "Bash(warmbly-api:*)" in launch
        assert 'Bash(curl' not in launch
        disallowed = launch.split('--disallowedTools', 1)[1].split('--')[0]
        assert 'Bash' not in disallowed and 'WebFetch' in disallowed
        assert '--append-system-prompt-file' in launch
        assert '--add-dir' in launch

    def test_launch_sources_a_private_env_file_with_the_credentials(self, tmp_path):
        launch = claude_tmux.launch_command('/x', 'sonnet', warmbly=True)
        env_file = tmp_path / 'handoff' / '.warmbly.env'
        assert f'. {env_file}' in launch
        assert 'wmbly_secret_value' in env_file.read_text()
        assert 'WARMBLY_API_URL' in env_file.read_text()
        # warmbly-api is on the pane's PATH.
        assert f'PATH={claude_tmux.WARMBLY_BIN}:' in env_file.read_text().replace("'", '')
        assert (env_file.stat().st_mode & 0o777) == 0o600

    def test_the_guide_tells_it_where_the_api_and_credentials_are(self, tmp_path):
        launch = claude_tmux.launch_command('/x', 'sonnet', warmbly=True)
        guide_path = launch.split('--append-system-prompt-file', 1)[1].split()[0]
        guide = Path(guide_path.strip("'")).read_text()
        for needed in ('warmbly-api GET', 'warmbly-api POST', 'openapi.json',
                       '/v1', 'jq', 'never print'):
            assert needed in guide

    def test_missing_credentials_fail_clearly(self, monkeypatch):
        monkeypatch.delenv('WARMBLY_API_TOKEN')
        with pytest.raises(LLMTransportError, match='WARMBLY_API_TOKEN'):
            claude_tmux.launch_command('/x', 'sonnet', warmbly=True)

    def test_the_pipeline_pane_still_has_no_bash(self):
        launch = claude_tmux.launch_command('/x', 'sonnet')
        assert 'Bash' in launch.split('--disallowedTools', 1)[1].split('--')[0]
        assert 'curl' not in launch and 'warmbly' not in launch.lower()

    def test_the_request_permits_the_api_instead_of_forbidding_tools(self, tmp_path):
        text = claude_tmux.build_request('List my campaigns.', None,
                                         tmp_path / 'a.answer.txt', fmt='text',
                                         warmbly=True)
        assert 'Warmbly API' in text
        assert 'no web' not in text
        plain = claude_tmux.build_request('x', None, tmp_path / 'a.txt', fmt='text')
        assert 'no web' in plain

    def test_complete_uses_the_warmbly_pane(self):
        def submit(line):
            if '.answer.' in line:
                Path(next(t for t in line.split() if '.answer.' in t)).write_text('3 campaigns')
        tmux = FakeTmux(has_session=False, on_submit=submit)
        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            out = claude_tmux.complete('How many campaigns?', model='sonnet', warmbly=True)
        assert out == '3 campaigns'
        new = next(c for c in tmux.calls if c[:2] == ['tmux', 'new-session'])
        assert S('sonnet', 'warmbly') in new


class TestWarmblyApiScript:
    """scripts/warmbly-api: the one command a Warmbly pane may run (besides
    jq). In scripts/, which is tracked; bin/ is gitignored."""

    SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'warmbly-api'

    def test_the_pane_path_points_at_it(self):
        assert Path(claude_tmux.WARMBLY_BIN) == self.SCRIPT.parent

    def _run(self, tmp_path, *args):
        stub = tmp_path / 'stubbin'
        stub.mkdir()
        (stub / 'curl').write_text('#!/bin/sh\nfor a in "$@"; do echo "ARG:$a"; done\n')
        (stub / 'curl').chmod(0o755)
        env = {'PATH': f'{stub}:/usr/bin:/bin', 'WARMBLY_API_URL': 'https://api.example/',
               'WARMBLY_API_TOKEN': 'wmbly_x'}
        return REAL_RUN([str(self.SCRIPT), *args], env=env, capture_output=True, text=True)

    def test_get_goes_to_the_v1_path_with_the_bearer_token(self, tmp_path):
        out = self._run(tmp_path, 'GET', '/campaigns?limit=5').stdout
        assert 'ARG:https://api.example/v1/campaigns?limit=5' in out
        assert 'ARG:Authorization: Bearer wmbly_x' in out
        assert 'ARG:GET' in out

    def test_a_path_already_under_v1_is_not_doubled(self, tmp_path):
        out = self._run(tmp_path, 'GET', '/v1/campaigns').stdout
        assert 'ARG:https://api.example/v1/campaigns' in out

    def test_a_body_is_sent_as_json(self, tmp_path):
        out = self._run(tmp_path, 'POST', 'contacts', '{"email": "a@b.co"}').stdout
        assert 'ARG:https://api.example/v1/contacts' in out
        assert 'ARG:{"email": "a@b.co"}' in out
        assert 'ARG:Content-Type: application/json' in out

    def test_only_the_configured_api_host_is_reachable(self, tmp_path):
        """A full URL would let the token go to any host."""
        result = self._run(tmp_path, 'GET', 'https://evil.example/x')
        assert result.returncode != 0
        assert 'evil.example' not in result.stdout

    def test_usage_without_arguments(self, tmp_path):
        result = self._run(tmp_path)
        assert result.returncode != 0 and 'usage' in result.stderr.lower()


class TestPanePerCheckout:
    """A pane runs in one checkout's handoff directory and may write only
    there. A pane started from the agents worktree took research requests
    from the main checkout and stopped on a permission prompt for each."""

    def test_checkouts_with_different_handoff_dirs_get_different_panes(self, monkeypatch, tmp_path):
        monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'a'))
        a = claude_tmux.session_name('opus')
        monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path / 'b'))
        b = claude_tmux.session_name('opus')
        assert a != b
        assert a.startswith('test-claude-opus-') and b.startswith('test-claude-opus-')

    def test_the_same_checkout_keeps_its_pane(self):
        assert claude_tmux.session_name('opus') == claude_tmux.session_name('opus')


class TestStaleUsageLimit:
    """After a reset, the pane still shows the old 'Usage limit reached'
    line. Checked before the request was even sent, it failed a whole batch
    of 50 in under a minute with the limit long gone."""

    STALE = ("\u25cf Usage limit reached \u00b7 continuing automatically at "
             "10:30pm \u00b7 esc to cancel\n\u276f " + FOOTER)

    def test_an_old_limit_message_does_not_stop_a_new_request(self):
        tmux = FakeTmux(panes=[self.STALE, self.STALE, READY_PANE],
                        on_submit=writes('{"name": "x", "score": 1}'))
        assert _run(tmux).name == 'x'

    def test_history_is_cleared_before_the_request_is_typed(self):
        """So the answer wait only sees what this request produced."""
        tmux = FakeTmux(on_submit=writes('{"name": "x", "score": 1}'))
        _run(tmux)
        cleared = [i for i, c in enumerate(tmux.calls)
                   if c[:2] == ['tmux', 'clear-history']]
        typed = next(i for i, c in enumerate(tmux.calls)
                     if '-l' in c and '.answer.' in c[-1])
        assert cleared and cleared[-1] < typed

    def test_a_limit_hit_by_this_request_still_fails_fast(self, _env):
        limit = ("\u25cf Usage limit reached \u00b7 continuing automatically "
                 "at 11:30pm\n\u276f " + FOOTER)
        tmux = FakeTmux(panes=[READY_PANE] * 4 + [limit])
        with pytest.raises(LLMTransportError, match='usage limit'):
            _run(tmux)
        assert _env['now'] < 30


# What Codex 0.163 draws in tmux (captured 2026-10-22).
CODEX_READY = ('  >_ OpenAI Codex (v0.163.0)\n     ~/x/data/handoff\n'
               '› Ask Codex to do anything\n'
               '  GPT-6.1-Sol default · ~/x/data/handoff\n'
               '  ← for agents · ? for shortcuts')
CODEX_BUSY = ('• Ran printf ok > a.answer.txt\n◦ Working (5s • esc to interrupt)\n'
              '› Ask Codex to do anything\n  GPT-6.1-Sol default · ~/x\n'
              '  ← for agents · ? for shortcuts')


class TestCodexProfile:
    """The same handoff, answered by Codex CLI in tmux instead of Claude Code."""

    @pytest.fixture(autouse=True)
    def _codex_env(self, monkeypatch):
        monkeypatch.setenv('CODEX_TMUX_SESSION', 'test-codex')
        monkeypatch.delenv('CODEX_TMUX_HOME', raising=False)
        monkeypatch.delenv('CODEX_TMUX_ARGS', raising=False)

    def _run(self, tmux, **kwargs):
        with patch.object(claude_tmux.subprocess, 'run', side_effect=tmux):
            return claude_tmux.complete_structured(
                'Business: Happy Tails', Answer, system='s', cli='codex', **kwargs)

    def test_codex_panes_are_their_own_sessions(self):
        name = claude_tmux.session_name('gpt-6.1', cli='codex')
        assert name.startswith('test-codex-gpt-6-1-')
        assert name != claude_tmux.session_name('gpt-6.1')

    def test_launch_is_sandboxed_to_the_handoff_dir_without_approvals(self):
        launch = claude_tmux.launch_command('/x/handoff', 'gpt-6.1', cli='codex')
        assert 'codex -s workspace-write -a never -C /x/handoff' in launch
        assert '-m gpt-6.1' in launch
        for unwanted in ('claude ', '--search', 'danger', '--dangerously'):
            assert unwanted not in launch

    def test_a_separate_codex_home_is_set_on_the_command(self, monkeypatch):
        monkeypatch.setenv('CODEX_TMUX_HOME', '/home/x/.codex-pane')
        launch = claude_tmux.launch_command('/x', cli='codex')
        assert '&& CODEX_HOME=/home/x/.codex-pane codex ' in launch

    def test_answers_end_to_end_in_a_codex_pane(self):
        tmux = FakeTmux(has_session=False, panes=[CODEX_READY],
                        on_submit=writes('{"name": "x", "score": 1}'))
        assert self._run(tmux).name == 'x'
        new = next(c for c in tmux.calls if c[:2] == ['tmux', 'new-session'])
        assert any(str(x).startswith('test-codex-') for x in new)
        assert '/clear' in tmux.typed

    def test_waits_while_codex_is_working(self):
        tmux = FakeTmux(panes=[CODEX_BUSY, CODEX_BUSY, CODEX_READY],
                        on_submit=writes('{"name": "x", "score": 1}'))
        assert self._run(tmux).name == 'x'

    def test_a_claude_screen_is_not_a_ready_codex(self):
        """The profile's own footer decides readiness, not any prompt glyph."""
        tmux = FakeTmux(panes=[READY_PANE])
        with pytest.raises(LLMTransportError, match='not ready'):
            self._run(tmux)

    def test_text_left_in_the_codex_composer_is_unsubmitted(self):
        pane = ('• earlier output\n'
                '› Read /x/data/handoff/a-1.request.md and do what it says: write\n'
                '  only the JSON answer to /x/data/handoff/a-1.answer.json\n'
                '  GPT-6.1-Sol default · ~/x\n  ← for agents · ? for shortcuts')
        text = ('Read /x/data/handoff/a-1.request.md and do what it says: write '
                'only the JSON answer to /x/data/handoff/a-1.answer.json')
        assert claude_tmux.unsubmitted(pane, text, cli='codex')
        assert not claude_tmux.unsubmitted(CODEX_READY, text, cli='codex')

    def test_a_codex_usage_limit_fails_fast(self, _env):
        limit = ("■ You've hit your usage limit. Upgrade to Pro or try again at "
                 "9:40 PM.\n" + CODEX_READY)
        tmux = FakeTmux(panes=[CODEX_READY] * 4 + [limit])
        with pytest.raises(LLMTransportError, match='usage limit'):
            self._run(tmux)
        assert _env['now'] < 30

    def test_calls_are_tallied_under_codex(self):
        recorded = []
        tmux = FakeTmux(panes=[CODEX_READY],
                        on_submit=writes('{"name": "x", "score": 1}'))
        with patch.object(claude_tmux, 'record_call',
                          side_effect=lambda name, ok: recorded.append(name)):
            self._run(tmux, model='gpt-6.1')
        assert recorded == ['codex-tmux/gpt-6.1']

    def test_warmbly_panes_are_claude_only(self):
        with pytest.raises(ValueError, match='codex'):
            claude_tmux.launch_command('/x', cli='codex', warmbly=True)


class TestCodexRouting:
    def test_codex_tmux_model_hands_the_call_to_a_codex_pane(self, monkeypatch):
        from flow.llm import CODEX_TMUX_MODEL
        monkeypatch.setenv('DRAFTING_MODEL', CODEX_TMUX_MODEL)
        monkeypatch.setenv('DRAFTING_CODEX_MODEL', 'gpt-6.1')
        with patch.object(claude_tmux, 'complete_structured',
                          return_value=Answer(name='x', score=1)) as handoff, \
             patch('flow.llm.chat_completion') as proxy:
            complete_structured('p', Answer, agent_model_env='DRAFTING_MODEL')
        proxy.assert_not_called()
        assert handoff.call_args.kwargs['cli'] == 'codex'
        assert handoff.call_args.kwargs['model'] == 'gpt-6.1'

    def test_the_claude_route_still_says_claude(self, monkeypatch):
        monkeypatch.setenv('RESEARCH_MODEL', CLAUDE_TMUX_MODEL)
        with patch.object(claude_tmux, 'complete_structured',
                          return_value=Answer(name='x', score=1)) as handoff:
            complete_structured('p', Answer, agent_model_env='RESEARCH_MODEL')
        assert handoff.call_args.kwargs.get('cli', 'claude') == 'claude'
