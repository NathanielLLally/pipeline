"""
Unit tests for the Discord bot flow.

The Discord and Warmbly connections are never opened here: the command logic
lives in plain async functions that take their collaborators as arguments, and
those are what is tested. The discord.py wiring around them is thin.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow import discord_bot as bot

HELP = """\
Work with Warmbly from your terminal.

Usage:
  warmbly <command> <subcommand> [flags]

Getting started
  auth           Sign in, sign out, and see who you are
  browse         Open the dashboard in a browser
  status         What is happening in your workspace right now

Doing the work
  campaign       Create, run and inspect campaigns
  contact        Add, find and update contacts

Your data
  warmup-routing Rules deciding which mailboxes warm with which

Setting up the CLI
  completion     Generate the autocompletion script for the specified shell
  help           Help about any command
  upgrade        Update the CLI to the newest release

Flags:
      --debug             Print each request to stderr
  -h, --help              help for warmbly
"""


@pytest.fixture(autouse=True)
def _event_store(tmp_path, monkeypatch):
    """Events go to a temporary data/discord, not the repo's."""
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))


def _run(coro):
    return asyncio.run(coro)


async def _block():
    await asyncio.sleep(3600)


def make_cfg(**overrides):
    values = dict(
        discord_token='t', channel_id=1, event_channel_id=2, guild_id=None,
        admin_role='CRMadmin', warmbly_token='w', gateway_url='wss://h',
        org_id='o', batchdraft_deployment='run-agents/run-agents',
        warmbly_api_url='https://api.h')
    values.update(overrides)
    return bot.BotConfig(**values)


class TestCliGroupDiscovery:
    def test_parses_command_groups_from_help(self):
        groups = bot.parse_cli_groups(HELP)
        assert 'campaign' in groups
        assert 'contact' in groups
        assert 'status' in groups
        assert 'warmup-routing' in groups

    def test_excludes_auth_and_local_only_commands(self):
        groups = bot.parse_cli_groups(HELP)
        for excluded in ('auth', 'browse', 'completion', 'help', 'upgrade'):
            assert excluded not in groups

    def test_flags_and_usage_lines_are_not_groups(self):
        groups = bot.parse_cli_groups(HELP)
        assert not any(g.startswith('-') for g in groups)
        assert 'warmbly' not in groups


class TestRunCli:
    def test_args_are_split_and_never_given_to_a_shell(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b'ok\n', b''))
        proc.returncode = 0
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as exec_:
            code, out = _run(bot.run_cli(
                'campaign', 'list --fields "name,status" ; rm -rf /'))

        argv = exec_.await_args.args
        assert argv[0] == bot.WARMBLY_BIN
        assert argv[1] == 'campaign'
        assert 'list' in argv
        assert 'name,status' in argv
        # The shell metacharacter arrives as a literal argument, not a command.
        assert 'rm' in argv and ';' not in ' '.join(argv[:3])
        assert code == 0 and out == 'ok\n'

    def test_stdin_is_closed_so_prompts_fail_instead_of_hanging(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b'', b''))
        proc.returncode = 0
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as exec_:
            _run(bot.run_cli('status', ''))
        assert exec_.await_args.kwargs['stdin'] == asyncio.subprocess.DEVNULL

    def test_unbalanced_quotes_are_reported_not_raised(self):
        code, out = _run(bot.run_cli('contact', 'find "unterminated'))
        assert code != 0
        assert 'quot' in out.lower()

    def test_stderr_is_included_in_output(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b'', b'x not found\n'))
        proc.returncode = 1
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)):
            code, out = _run(bot.run_cli('contact', 'get nope'))
        assert code == 1
        assert 'not found' in out


class TestBatchDraft:
    def test_defaults_to_fifty(self):
        run = AsyncMock(return_value=SimpleNamespace(id='fr-1', name='happy-run'))
        _run(bot.trigger_batchdraft(None, run_deployment=run,
                                    deployment='run-agents/run-agents'))
        assert run.await_args.kwargs['parameters'] == {'batch_size': 50}

    def test_passes_n_as_batch_size(self):
        run = AsyncMock(return_value=SimpleNamespace(id='fr-1', name='r'))
        _run(bot.trigger_batchdraft(7, run_deployment=run,
                                    deployment='run-agents/run-agents'))
        assert run.await_args.kwargs['parameters'] == {'batch_size': 7}

    def test_triggers_the_configured_deployment_without_waiting(self):
        run = AsyncMock(return_value=SimpleNamespace(id='fr-1', name='r'))
        _run(bot.trigger_batchdraft(3, run_deployment=run, deployment='x/y'))
        kwargs = run.await_args.kwargs
        assert kwargs['name'] == 'x/y'
        assert kwargs['timeout'] == 0
        # Not a child of the long-running bot flow: each batch stands alone.
        assert kwargs['as_subflow'] is False

    def test_reply_names_the_flow_run(self):
        run = AsyncMock(return_value=SimpleNamespace(id='fr-1', name='happy-run'))
        msg = _run(bot.trigger_batchdraft(5, run_deployment=run,
                                          deployment='run-agents/run-agents'))
        assert 'happy-run' in msg and '5' in msg


class TestAuthDevicePrompt:
    def test_extracts_code_and_url(self):
        assert bot.parse_device_line('  Your code: YH5C-CBFF') == ('code', 'YH5C-CBFF')
        assert bot.parse_device_line(
            '  Approve at: https://crm.example.com/cli?code=YH5C-CBFF'
        ) == ('url', 'https://crm.example.com/cli?code=YH5C-CBFF')

    def test_other_lines_are_ignored(self):
        assert bot.parse_device_line('… Waiting for approval') is None
        assert bot.parse_device_line('') is None

    def test_login_relays_url_then_reports_success(self):
        lines = [b'\n', b'  Your code: AB12-CD34\n',
                 b'  Approve at: https://h/cli?code=AB12-CD34\n',
                 b'Waiting for approval\n', b'Signed in as me@x\n', b'']
        proc = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=lines)
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        prompts = []

        async def on_prompt(url, code):
            prompts.append((url, code))

        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as exec_:
            ok, out = _run(bot.auth_login('crm.example.com', on_prompt))

        argv = exec_.await_args.args
        assert argv[:3] == (bot.WARMBLY_BIN, 'auth', 'login')
        assert '--web' in argv
        assert argv[argv.index('--hostname') + 1] == 'crm.example.com'
        assert prompts == [('https://h/cli?code=AB12-CD34', 'AB12-CD34')]
        assert ok is True
        assert 'Signed in' in out

    def test_login_failure_is_reported(self):
        proc = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b'x expired\n', b''])
        proc.wait = AsyncMock(return_value=1)
        proc.returncode = 1
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)):
            ok, out = _run(bot.auth_login('h', AsyncMock()))
        assert ok is False and 'expired' in out


class TestGatewayWiring:
    def test_base_url_strips_socket_path(self):
        assert bot.gateway_base_url(
            'wss://ws.example.com/socket/websocket') == 'wss://ws.example.com'
        assert bot.gateway_base_url('wss://ws.example.com/') == 'wss://ws.example.com'

    def _registered(self):
        gw = MagicMock()
        registered = []
        gw.on_event.side_effect = lambda name: (
            lambda handler: registered.append(name) or handler)
        bot.register_event_handlers(gw, AsyncMock())
        return set(registered)

    def test_every_business_event_is_published(self):
        from warmbly.gateway import GatewayEvent
        business = {v for k, v in vars(GatewayEvent).items()
                    if k.isupper() and isinstance(v, str) and v.isupper()}
        assert business - bot.SUPPRESSED_EVENTS <= self._registered()

    def test_email_updated_is_not_published(self):
        """Flag/folder churn on every message; it drowned out the feed."""
        assert 'EMAIL_UPDATED' not in self._registered()

    def test_control_frames_are_not_published(self):
        """Presence, rate-limit and resume frames are transport, not CRM events."""
        registered = self._registered()
        for frame in ('presence_state', 'presence_diff', 'rate_limited',
                      'resumed', 'resume_failed'):
            assert frame not in registered

    def test_handler_publishes_formatted_event(self):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(gw, publish)

        _run(handlers['EMAIL_REPLIED']('org:1', {'contact_id': 'c1'}))
        publish.assert_awaited_once()
        # (summary, filename, json bytes): the payload is attached as a file
        # rather than inlined as a fenced code block.
        summary, filename, data = publish.await_args.args[0]
        assert 'EMAIL_REPLIED' in summary
        assert filename.endswith('.json')
        assert json.loads(data.decode())['contact_id'] == 'c1'


class TestListFlows:
    def test_groups_deployments_under_their_flow(self):
        flows = [SimpleNamespace(id='f1', name='run-agents'),
                 SimpleNamespace(id='f2', name='discord-bot')]
        deps = [SimpleNamespace(flow_id='f1', name='run-agents', paused=False),
                SimpleNamespace(flow_id='f2', name='discord-bot', paused=True)]
        client = MagicMock()
        client.read_flows = AsyncMock(return_value=flows)
        client.read_deployments = AsyncMock(return_value=deps)

        msg = _run(bot.list_flows(client))
        assert 'run-agents/run-agents' in msg
        assert 'discord-bot/discord-bot' in msg
        assert 'paused' in msg

    def test_reports_flows_with_no_deployment(self):
        client = MagicMock()
        client.read_flows = AsyncMock(
            return_value=[SimpleNamespace(id='f1', name='orphan')])
        client.read_deployments = AsyncMock(return_value=[])
        msg = _run(bot.list_flows(client))
        assert 'orphan' in msg and 'no deployment' in msg


class TestChunking:
    def test_long_output_is_split_under_the_limit(self):
        parts = bot.chunk_output('line\n' * 2000)
        assert len(parts) > 1
        assert all(len(p) <= bot.DISCORD_MESSAGE_LIMIT for p in parts)

    def test_empty_output_still_says_something(self):
        assert bot.chunk_output('') == ['```\n(no output)\n```']


class TestConfig:
    def test_required_vars_are_named_when_missing(self, monkeypatch):
        for k in ('DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID',
                  'DISCORD_EVENT_CHANNEL_ID', 'DISCORD_ADMIN_ROLE',
                  'WARMBLY_API_TOKEN', 'WARMBLY_WEBSOCKET_URL',
                  'WARMBLY_ORG_ID'):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError) as exc:
            bot.BotConfig.from_env()
        assert 'DISCORD_BOT_TOKEN' in str(exc.value)
        assert 'WARMBLY_ORG_ID' in str(exc.value)
        # Fail closed: without a role every member could run every command.
        assert 'DISCORD_ADMIN_ROLE' in str(exc.value)
        assert 'DISCORD_EVENT_CHANNEL_ID' in str(exc.value)

    def test_batchdraft_deployment_default(self, monkeypatch):
        for k, v in {'DISCORD_BOT_TOKEN': 't', 'DISCORD_CHANNEL_ID': '1',
                     'DISCORD_EVENT_CHANNEL_ID': '2',
                     'DISCORD_ADMIN_ROLE': 'CRMadmin',
                     'WARMBLY_API_TOKEN': 'w', 'WARMBLY_WEBSOCKET_URL': 'wss://h',
                     'WARMBLY_ORG_ID': 'o'}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv('BATCHDRAFT_DEPLOYMENT', raising=False)
        monkeypatch.delenv('DISCORD_GUILD_ID', raising=False)
        cfg = bot.BotConfig.from_env()
        assert cfg.batchdraft_deployment == 'run-agents/run-agents'
        assert cfg.channel_id == 1
        assert cfg.event_channel_id == 2
        assert cfg.admin_role == 'CRMadmin'
        assert cfg.guild_id is None


class TestDeploymentImportPath:
    def test_flow_dir_on_sys_path_does_not_shadow_the_warmbly_sdk(self):
        """
        Prefect's deployment loader puts the entrypoint's directory (flow/) at
        the front of sys.path, so any flow/<name>.py shadows a top-level
        package called <name>. Running the import in a subprocess with that
        layout reproduces what a served flow run sees.
        """
        import subprocess
        import sys
        from pathlib import Path

        flow_dir = Path(bot.__file__).resolve().parent
        result = subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, sys.argv[1]); '
             'from warmbly.gateway import AsyncGatewayClient',
             str(flow_dir)],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestAnnouncement:
    GROUPS = ['campaign', 'contact', 'status']

    def _cfg(self):
        return make_cfg(org_id='org-9')

    def test_names_the_built_in_commands(self):
        msg = bot.format_announcement(self._cfg(), self.GROUPS)
        for cmd in ('/batchdraft', '/auth', '/list'):
            assert cmd in msg

    def test_names_the_batch_default_and_target_deployment(self):
        msg = bot.format_announcement(self._cfg(), self.GROUPS)
        assert str(bot.DEFAULT_BATCH) in msg
        assert 'run-agents/run-agents' in msg

    def test_lists_the_piped_cli_commands(self):
        msg = bot.format_announcement(self._cfg(), self.GROUPS)
        assert '/campaign' in msg and '/status' in msg

    def test_says_where_events_come_from(self):
        msg = bot.format_announcement(self._cfg(), self.GROUPS)
        assert 'org-9' in msg

    def test_fits_discord_message_limit_with_many_groups(self):
        groups = [f'group-{i:03d}' for i in range(200)]
        msg = bot.format_announcement(self._cfg(), groups)
        assert len(msg) <= bot.DISCORD_MESSAGE_LIMIT


class TestSender:
    def _client(self, channels):
        client = MagicMock()
        client.wait_until_ready = AsyncMock()
        client.get_channel.side_effect = channels.get
        return client

    def _channel(self, log, name, fail_on=None):
        channel = MagicMock()

        async def send(message):
            if message == fail_on:
                raise RuntimeError('missing permissions')
            log.append((name, message))
            if name == 'events':
                raise asyncio.CancelledError
        channel.send = send
        return channel

    def _go(self, client, announcement='hello'):
        async def go():
            queue = asyncio.Queue()
            await queue.put('event-1')
            try:
                await bot.run_sender(client, make_cfg(), queue, announcement)
            except asyncio.CancelledError:
                pass
        _run(go())

    def test_announces_in_command_channel_and_feeds_event_channel(self):
        log = []
        client = self._client({1: self._channel(log, 'commands'),
                               2: self._channel(log, 'events')})
        self._go(client)
        assert log == [('commands', 'hello'), ('events', 'event-1')]

    def test_failed_announcement_does_not_stop_the_feed(self):
        log = []
        client = self._client({
            1: self._channel(log, 'commands', fail_on='hello'),
            2: self._channel(log, 'events')})
        self._go(client)
        assert log == [('events', 'event-1')]


def member(*role_names):
    return SimpleNamespace(roles=[SimpleNamespace(name=n) for n in role_names])


class TestRoleGate:
    def test_member_with_role_is_allowed(self):
        assert bot.has_role(member('everyone', 'CRMadmin'), 'CRMadmin')

    def test_member_without_role_is_refused(self):
        assert not bot.has_role(member('everyone', 'Mods'), 'CRMadmin')

    def test_role_name_must_match_exactly(self):
        assert not bot.has_role(member('crmadmin'), 'CRMadmin')
        assert not bot.has_role(member('CRMadmin2'), 'CRMadmin')

    def test_dm_user_has_no_roles_and_is_refused(self):
        assert not bot.has_role(SimpleNamespace(name='someone'), 'CRMadmin')

    def test_refused_user_gets_a_private_reply(self):
        interaction = MagicMock()
        interaction.user = member('everyone')
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        allowed = _run(bot.check_role(interaction, 'CRMadmin'))

        assert allowed is False
        kwargs = interaction.response.send_message.await_args.kwargs
        args = interaction.response.send_message.await_args.args
        assert kwargs['ephemeral'] is True
        assert 'CRMadmin' in args[0]

    def test_allowed_user_gets_no_reply_from_the_gate(self):
        interaction = MagicMock()
        interaction.user = member('CRMadmin')
        interaction.response.send_message = AsyncMock()
        assert _run(bot.check_role(interaction, 'CRMadmin')) is True
        interaction.response.send_message.assert_not_awaited()


class TestTreeEnforcesRole:
    def test_every_command_is_gated_by_the_tree(self):
        groups = ['campaign', 'status']
        client = bot.build_client(make_cfg(), groups)
        tree = client.bot_tree
        names = {c.name for c in tree.get_commands()}
        assert {'batchdraft', 'auth', 'list', 'campaign', 'status'} <= names

        interaction = MagicMock()
        interaction.user = member('everyone')
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        assert _run(tree.interaction_check(interaction)) is False

        interaction.user = member('CRMadmin')
        assert _run(tree.interaction_check(interaction)) is True

    def test_commands_are_not_hidden_behind_manage_server(self):
        """CRMadmin members without Manage Server must still see them."""
        client = bot.build_client(make_cfg(), ['campaign'])
        for command in client.bot_tree.get_commands():
            assert command.default_permissions is None, command.name


class TestAnnouncementMentionsAccess:
    def test_names_the_role_and_links_the_event_channel(self):
        msg = bot.format_announcement(make_cfg(event_channel_id=42), [])
        assert 'CRMadmin' in msg
        assert '<#42>' in msg


ORG_ID = '00e33e77-6d57-4e2f-a0cd-e0bc66afd774'
ORG_NAME = 'Happy Tails Paw Care'


class TestFetchOrgName:
    def _response(self, status, body):
        import httpx
        return httpx.Response(status, json=body,
                              request=httpx.Request('GET', 'https://h/v1/me'))

    def test_reads_name_from_me(self):
        body = {'organization_id': ORG_ID, 'organization_name': ORG_NAME}
        get = AsyncMock(return_value=self._response(200, body))
        with patch('flow.discord_bot.httpx.AsyncClient') as cls:
            cls.return_value.__aenter__.return_value.get = get
            names = _run(bot.fetch_org_names('https://api.h/', 'tok'))
        assert names == {ORG_ID: ORG_NAME}
        assert get.await_args.args[0] == 'https://api.h/v1/me'
        assert get.await_args.kwargs['headers']['Authorization'] == 'Bearer tok'

    def test_failure_returns_empty_instead_of_raising(self):
        get = AsyncMock(return_value=self._response(401, {'message': 'no'}))
        with patch('flow.discord_bot.httpx.AsyncClient') as cls:
            cls.return_value.__aenter__.return_value.get = get
            assert _run(bot.fetch_org_names('https://api.h', 'tok')) == {}

    def test_missing_api_url_returns_empty(self):
        assert _run(bot.fetch_org_names(None, 'tok')) == {}


class TestDisplayFilter:
    def _filter(self, names=None):
        return bot.EventDisplayFilter(
            {ORG_ID: ORG_NAME} if names is None else names)

    def test_wraps_payload_in_a_json_code_block(self):
        msg = self._filter().render('EMAIL_REPLIED', f'org:{ORG_ID}',
                                    {'contact_id': 'c1'})
        body = msg.split('```json\n', 1)[1].rsplit('\n```', 1)[0]
        assert msg.rstrip().endswith('```')
        import json as _json
        assert _json.loads(body) == {'contact_id': 'c1'}

    def test_org_id_in_topic_becomes_the_name(self):
        msg = self._filter().render('EMAIL_REPLIED', f'org:{ORG_ID}', {})
        assert ORG_NAME in msg
        assert ORG_ID not in msg

    def test_org_id_inside_payload_becomes_the_name(self):
        msg = self._filter().render('CONTACT_CREATED', f'org:{ORG_ID}', {
            'organization_id': ORG_ID,
            'nested': {'org': ORG_ID, 'list': [ORG_ID]},
            'note': f'created in {ORG_ID}'})
        assert ORG_ID not in msg
        assert msg.count(ORG_NAME) >= 5

    def test_payload_is_not_mutated(self):
        payload = {'organization_id': ORG_ID}
        self._filter().render('X', 't', payload)
        assert payload == {'organization_id': ORG_ID}

    def test_unknown_ids_pass_through(self):
        msg = self._filter().render('X', 'org:other', {'id': 'abc'})
        assert 'org:other' in msg and 'abc' in msg

    def test_no_names_still_renders(self):
        msg = self._filter({}).render('X', f'org:{ORG_ID}', {'a': 1})
        assert ORG_ID in msg and '```json' in msg

    def test_fits_discord_message_limit(self):
        msg = self._filter().render('CONTACTS_RELOAD', 'org:1',
                                    {'blob': 'x' * 10_000})
        assert len(msg) <= bot.DISCORD_MESSAGE_LIMIT
        assert msg.rstrip().endswith('```')

    def test_backticks_in_payload_cannot_close_the_block(self):
        msg = self._filter().render('X', 't', {'body': 'a ``` b'})
        assert msg.count('```') == 2

    def test_non_json_values_do_not_raise(self):
        from datetime import datetime
        self._filter().render('X', 't', {'at': datetime(2026, 1, 1)})


class TestHandlersUseTheFilter:
    def test_handler_publishes_filtered_event(self):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(
            gw, publish, bot.EventDisplayFilter({ORG_ID: ORG_NAME}))

        _run(handlers['EMAIL_REPLIED'](f'org:{ORG_ID}', {'org': ORG_ID}))
        summary, _filename, data = publish.await_args.args[0]
        body = data.decode()
        assert 'EMAIL_REPLIED' in summary and ORG_NAME in summary
        # The substitution must apply to the attachment too, not just the line.
        assert ORG_ID not in summary and ORG_ID not in body
        assert ORG_NAME in body


class TestAnnouncementUsesOrgName:
    def test_names_the_org_instead_of_its_id(self):
        msg = bot.format_announcement(make_cfg(org_id=ORG_ID), [],
                                      org_name=ORG_NAME)
        assert ORG_NAME in msg and ORG_ID not in msg

    def test_falls_back_to_the_id(self):
        msg = bot.format_announcement(make_cfg(org_id=ORG_ID), [])
        assert ORG_ID in msg


class TestEventIntents:
    def _env(self, monkeypatch, **extra):
        base = {'DISCORD_BOT_TOKEN': 't', 'DISCORD_CHANNEL_ID': '1',
                'DISCORD_EVENT_CHANNEL_ID': '2', 'DISCORD_ADMIN_ROLE': 'r',
                'WARMBLY_API_TOKEN': 'w', 'WARMBLY_WEBSOCKET_URL': 'wss://h',
                'WARMBLY_ORG_ID': 'o'}
        base.update(extra)
        for k, v in base.items():
            monkeypatch.setenv(k, v)

    def test_unset_means_no_server_side_filter(self, monkeypatch):
        self._env(monkeypatch)
        monkeypatch.delenv('WARMBLY_EVENT_INTENTS', raising=False)
        assert bot.BotConfig.from_env().event_intents is None

    def test_comma_list_is_parsed_and_upper_cased(self, monkeypatch):
        self._env(monkeypatch, WARMBLY_EVENT_INTENTS=' email, Campaign ,,')
        assert bot.BotConfig.from_env().event_intents == ['EMAIL', 'CAMPAIGN']

    def test_blank_value_means_no_filter(self, monkeypatch):
        self._env(monkeypatch, WARMBLY_EVENT_INTENTS='  ')
        assert bot.BotConfig.from_env().event_intents is None

    def test_intents_are_passed_to_subscribe(self):
        gw = MagicMock()
        gw.connect = AsyncMock()
        gw.subscribe = AsyncMock()
        gw.run_forever = AsyncMock(side_effect=_block)
        gw.close = AsyncMock()

        async def go():
            reloader = bot.Reloader()
            reloader.attach(asyncio.get_running_loop())
            refresh = AsyncMock(return_value=('tok', bot.EventDisplayFilter()))
            task = asyncio.create_task(bot.run_gateway(
                make_cfg(event_intents=['EMAIL']), asyncio.Queue(), reloader,
                refresh, gateway_factory=lambda token, base_url: gw))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(go())
        assert gw.subscribe.await_args.kwargs['intents'] == ['EMAIL']

    def test_announcement_names_the_filter(self):
        msg = bot.format_announcement(make_cfg(event_intents=['EMAIL', 'MEETING']), [])
        assert 'EMAIL' in msg and 'MEETING' in msg


class TestActiveToken:
    def _proc(self, out, code=0):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(out, b''))
        proc.returncode = code
        return proc

    def test_prefers_the_cli_signed_in_token(self):
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=self._proc(b'wmbly_cli\n'))) as ex:
            token = _run(bot.active_warmbly_token('wmbly_env'))
        assert token == 'wmbly_cli'
        assert ex.await_args.args[1:3] == ('auth', 'token')

    def test_falls_back_to_env_token_when_cli_has_none(self):
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=self._proc(b'', code=1))):
            assert _run(bot.active_warmbly_token('wmbly_env')) == 'wmbly_env'

    def test_falls_back_when_cli_is_missing(self):
        with patch('flow.discord_bot.asyncio.create_subprocess_exec',
                   new=AsyncMock(side_effect=FileNotFoundError)):
            assert _run(bot.active_warmbly_token('wmbly_env')) == 'wmbly_env'


class TestReloader:
    def test_request_wakes_the_waiter(self):
        async def go():
            r = bot.Reloader()
            r.attach(asyncio.get_running_loop())
            waiter = asyncio.create_task(r.wait())
            await asyncio.sleep(0)
            assert not waiter.done()
            r.request('test')
            return await asyncio.wait_for(waiter, 1)
        assert _run(go()) == 'test'

    def test_request_from_another_thread_is_safe(self):
        import threading

        async def go():
            r = bot.Reloader()
            r.attach(asyncio.get_running_loop())
            threading.Thread(target=r.request, args=('from-thread',)).start()
            return await asyncio.wait_for(r.wait(), 2)
        assert _run(go()) == 'from-thread'

    def test_request_before_attach_is_ignored(self):
        bot.Reloader().request('early')


class TestFeedReloads:
    def test_reload_reconnects_with_a_fresh_token(self):
        """The gateway is rebuilt per generation with the current token."""
        tokens = iter(['tok-1', 'tok-2'])
        built = []

        def make_gw(token, base_url):
            gw = MagicMock()
            gw.connect = AsyncMock()
            gw.subscribe = AsyncMock()
            gw.close = AsyncMock()
            gw.run_forever = AsyncMock(side_effect=_block)
            built.append((token, gw))
            return gw

        async def go():
            reloader = bot.Reloader()
            reloader.attach(asyncio.get_running_loop())
            display = bot.EventDisplayFilter()
            refresh = AsyncMock(side_effect=lambda: (next(tokens), display))
            feed = asyncio.create_task(bot.run_gateway(
                make_cfg(), asyncio.Queue(), reloader, refresh,
                gateway_factory=make_gw))
            await asyncio.sleep(0.05)
            reloader.request('test')
            await asyncio.sleep(0.05)
            feed.cancel()
            try:
                await feed
            except asyncio.CancelledError:
                pass

        _run(go())
        assert [t for t, _ in built] == ['tok-1', 'tok-2']
        built[0][1].close.assert_awaited()
        built[1][1].close.assert_awaited()

    def test_failed_refresh_keeps_retrying_instead_of_dying(self):
        attempts = []

        async def refresh():
            attempts.append(1)
            raise RuntimeError('api down')

        async def go():
            reloader = bot.Reloader()
            reloader.attach(asyncio.get_running_loop())
            feed = asyncio.create_task(bot.run_gateway(
                make_cfg(), asyncio.Queue(), reloader, refresh,
                gateway_factory=MagicMock(), retry_delay=0.01))
            await asyncio.sleep(0.08)
            feed.cancel()
            try:
                await feed
            except asyncio.CancelledError:
                pass

        _run(go())
        assert len(attempts) >= 2


class TestAuthTriggersReload:
    def test_successful_login_requests_a_reload(self):
        reloader = MagicMock()
        with patch('flow.discord_bot.auth_login',
                   new=AsyncMock(return_value=(True, 'Signed in'))):
            ok, _ = _run(bot.login_and_reload('h', AsyncMock(), reloader, 'https://api.h'))
        assert ok
        reloader.request.assert_called_once_with('/auth')

    def test_failed_login_does_not_reload(self):
        reloader = MagicMock()
        with patch('flow.discord_bot.auth_login',
                   new=AsyncMock(return_value=(False, 'expired'))):
            _run(bot.login_and_reload('h', AsyncMock(), reloader, 'https://api.h'))
        reloader.request.assert_not_called()


class TestReloadCommand:
    def test_reload_is_a_built_in_command(self):
        client = bot.build_client(make_cfg(), ['campaign'], reloader=bot.Reloader())
        names = {c.name for c in client.bot_tree.get_commands()}
        assert 'reload' in names
        assert 'reload' in bot.RESERVED_NAMES

    def test_announcement_lists_reload(self):
        assert '/reload' in bot.format_announcement(make_cfg(), [])


class TestReloadDuringBackoff:
    def test_reload_cuts_the_retry_wait_short(self):
        """After a fatal gateway error, /reload must not wait out the backoff."""
        built = []

        def make_gw(token, base_url):
            gw = MagicMock()
            gw.connect = AsyncMock(side_effect=RuntimeError('401 revoked')
                                   if not built else None)
            gw.subscribe = AsyncMock()
            gw.close = AsyncMock()
            gw.run_forever = AsyncMock(side_effect=_block)
            built.append(token)
            return gw

        async def go():
            reloader = bot.Reloader()
            reloader.attach(asyncio.get_running_loop())
            refresh = AsyncMock(return_value=('tok', bot.EventDisplayFilter()))
            task = asyncio.create_task(bot.run_gateway(
                make_cfg(), asyncio.Queue(), reloader, refresh,
                gateway_factory=make_gw, retry_delay=3600))
            await asyncio.sleep(0.05)
            reloader.request('/auth')
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(go())
        assert len(built) == 2




class TestNoSighupHandler:
    def test_the_bot_does_not_claim_sighup(self):
        """
        Prefect may load the flow off the main thread, where signal.signal is
        refused, so a handler would exist under some loaders and not others.
        Reload is /reload or a successful /auth; SIGHUP keeps its default.
        """
        import signal
        assert not hasattr(bot, 'install_sighup')
        assert signal.getsignal(signal.SIGHUP) in (signal.SIG_DFL, signal.SIG_IGN, None)


class TestEventAttachment:
    """Forward the JSON as an attachment, not an inline code block.

    A fenced code block in the channel is noisy and gets truncated at
    Discord's 2000-character limit, so the part you want is often the part
    that was cut. As a file it is a one-line summary you can scan plus a
    link to click when an event is actually interesting, and nothing is
    truncated.
    """

    def _filter(self, names=None):
        return bot.EventDisplayFilter(
            {ORG_ID: 'Acme'} if names is None else names)

    def test_summary_names_the_event_and_topic(self):
        summary = self._filter().event_summary(
            'EMAIL_REPLIED', f'org:{ORG_ID}', {'id': 'abc'})

        assert 'EMAIL_REPLIED' in summary
        assert 'Acme' in summary

    def test_summary_fits_in_one_discord_message(self):
        summary = self._filter().event_summary(
            'X', 't', {'blob': 'y' * 50_000})

        assert len(summary) < bot.DISCORD_MESSAGE_LIMIT

    def test_attachment_is_untruncated_json(self):
        payload = {'blob': 'y' * 50_000}

        name, data = self._filter().event_attachment('X', 't', payload)

        assert len(data) > 50_000
        assert json.loads(data.decode())['blob'] == 'y' * 50_000

    def test_attachment_applies_the_name_substitution(self):
        name, data = self._filter().event_attachment(
            'X', f'org:{ORG_ID}', {'org': ORG_ID})

        assert json.loads(data.decode())['org'] == 'Acme'

    def test_attachment_filename_identifies_the_event(self):
        name, _ = self._filter().event_attachment('EMAIL_REPLIED', 't', {})

        assert name.endswith('.json')
        assert 'email-replied' in name.lower()

    def test_attachment_needs_no_codeblock_escaping(self):
        """The ``` escaping existed only because of the inline code block."""
        name, data = self._filter().event_attachment(
            'X', 't', {'body': 'a ``` b'})

        assert json.loads(data.decode())['body'] == 'a ``` b'


class TestSenderUploadsFiles:
    def test_sends_summary_with_the_json_attached(self):
        import asyncio as aio

        sent = {}

        class Channel:
            async def send(self, content=None, file=None):
                sent['content'] = content
                sent['file'] = file

        channel = Channel()
        queue = aio.Queue()
        queue.put_nowait(('**X** `t`', 'x.json', b'{"a": 1}'))

        async def drive():
            client = MagicMock()
            client.wait_until_ready = AsyncMock()
            with patch.object(bot, '_resolve_channel',
                              AsyncMock(return_value=channel)):
                task = aio.ensure_future(
                    bot.run_sender(client, make_cfg(), queue, 'hello'))
                await aio.sleep(0.05)
                task.cancel()

        aio.run(drive())

        assert sent['content'] == '**X** `t`'
        assert sent['file'] is not None

    def test_a_plain_string_still_sends_as_a_message(self):
        """Announcements and anything else queued as text keep working."""
        import asyncio as aio

        sent = {}

        class Channel:
            async def send(self, content=None, file=None):
                sent['content'] = content
                sent['file'] = file

        channel = Channel()
        queue = aio.Queue()
        queue.put_nowait('just text')

        async def drive():
            client = MagicMock()
            client.wait_until_ready = AsyncMock()
            with patch.object(bot, '_resolve_channel',
                              AsyncMock(return_value=channel)):
                task = aio.ensure_future(
                    bot.run_sender(client, make_cfg(), queue, 'hello'))
                await aio.sleep(0.05)
                task.cancel()

        aio.run(drive())

        assert sent['content'] == 'just text'
        assert sent['file'] is None


# Real EMAIL_RECEIVED payloads from the event channel, trimmed. Instantly
# warmup mail carries the pool's filter tag in the subject and the body.
WARMUP = {
    'event_type': 'EMAIL_RECEIVED', 'folder': 'inbox',
    'from': 'Tomiko M <tomiko@veritrogrid.com>',
    'subject': "Nate, we'd like to hear from you! | warmup 33153D9",
    'preview': 'Hi there, Could I send you a new quote? Sincerely, Tomiko '
               'warmup 33153D9',
}
REAL = {
    'event_type': 'EMAIL_RECEIVED', 'folder': 'inbox',
    'from': 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>',
    'subject': 'Delivery Status Notification (Failure)',
    'preview': '** Message not delivered ** There was a problem delivering '
               'your message to nina.l@itsninalopez.com.',
}


class TestWarmupFilter:
    def test_tag_in_subject_is_warmup(self):
        assert bot.is_warmup(WARMUP)

    def test_tag_only_in_preview_is_warmup(self):
        assert bot.is_warmup({**WARMUP, 'subject': 'Coffee?'})

    def test_multi_word_tag_prefix_is_warmup(self):
        assert bot.is_warmup({
            'subject': 'Financing Ready Now | Black Tie Funding 33153D9',
            'preview': 'Good afternoon Rose'})

    def test_tag_order_does_not_matter(self):
        assert bot.is_warmup({'subject': 'Thoughts? | 33153D9 G0CNY5F'})

    def test_real_mail_is_not_warmup(self):
        assert not bot.is_warmup(REAL)

    def test_tag_inside_a_longer_token_is_not_warmup(self):
        assert not bot.is_warmup({'subject': 'Order X33153D9Y shipped'})

    def test_missing_or_null_fields_are_not_warmup(self):
        assert not bot.is_warmup({'subject': None})
        assert not bot.is_warmup({})

    def test_non_dict_payload_is_not_warmup(self):
        assert not bot.is_warmup('33153D9')
        assert not bot.is_warmup(None)

    def test_tags_are_configurable(self):
        assert bot.is_warmup({'subject': 'hi | x ABC1234'}, tags=('ABC1234',))
        assert not bot.is_warmup(WARMUP, tags=('ABC1234',))

    def test_parse_tags(self):
        assert bot.parse_warmup_tags(None) == ('33153D9',)
        assert bot.parse_warmup_tags(' a1, b2 ,,') == ('a1', 'b2')
        assert bot.parse_warmup_tags('') == ()


class TestHandlersDropWarmup:
    def _handlers(self):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(gw, publish)
        return handlers, publish

    def test_warmup_received_is_dropped(self):
        handlers, publish = self._handlers()
        _run(handlers['EMAIL_RECEIVED']('org:1', WARMUP))
        publish.assert_not_awaited()

    def test_real_received_is_published(self):
        handlers, publish = self._handlers()
        _run(handlers['EMAIL_RECEIVED']('org:1', REAL))
        publish.assert_awaited_once()

    def test_non_dict_received_is_published(self):
        handlers, publish = self._handlers()
        _run(handlers['EMAIL_RECEIVED']('org:1', ['odd']))
        publish.assert_awaited_once()

    def test_other_events_with_the_tag_are_still_published(self):
        """Only inbound mail is filtered; our own sends stay visible."""
        handlers, publish = self._handlers()
        _run(handlers['EMAIL_SENT']('org:1', WARMUP))
        publish.assert_awaited_once()


def dm(author_bot=False, guild=None, author=None):
    message = MagicMock()
    message.guild = guild
    message.author = author or SimpleNamespace(bot=author_bot, name='nat')
    message.channel.send = AsyncMock()
    return message


class TestDirectMessageHelp:
    def test_reply_mentions_filter_and_jq(self):
        text = bot.dm_help_text()
        assert '/filter' in text
        assert '/jq' in text
        assert 'event' in text and 'jq' in text

    def test_dm_from_a_person_gets_the_help(self):
        assert bot.dm_reply(dm(), bot_user=object(), dm_user='nat') == \
            bot.dm_help_text()

    def test_guild_messages_are_ignored(self):
        assert bot.dm_reply(dm(guild=object()), bot_user=object(), dm_user='nat') is None

    def test_other_bots_are_ignored(self):
        assert bot.dm_reply(dm(author_bot=True), bot_user=object(), dm_user='nat') is None

    def test_own_messages_are_ignored(self):
        me = SimpleNamespace(bot=False, name='me')
        assert bot.dm_reply(dm(author=me), bot_user=me, dm_user='nat') is None

    def test_client_answers_a_dm(self):
        client = bot.build_client(make_cfg(dm_user='nat'), ['campaign'])
        message = dm()
        _run(client.on_message(message))
        message.channel.send.assert_awaited_once_with(bot.dm_help_text())

    def test_client_stays_quiet_in_guild_channels(self):
        client = bot.build_client(make_cfg(dm_user='nat'), ['campaign'])
        message = dm(guild=object())
        _run(client.on_message(message))
        message.channel.send.assert_not_awaited()


class TestEventNames:
    def test_forwarded_events_are_the_business_events_minus_suppressed(self):
        names = bot.forwarded_events()
        assert 'EMAIL_SENT' in names
        assert 'EMAIL_UPDATED' not in names
        assert 'presence_diff' not in names
        assert names == sorted(names)

    @pytest.mark.parametrize('typed', [
        'EMAIL_SENT', 'email_sent', 'Email_Sent', ' email-sent ', 'email sent'])
    def test_resolve_ignores_case_and_separators(self, typed):
        assert bot.resolve_event(typed) == 'EMAIL_SENT'

    def test_resolve_unknown_is_none(self):
        assert bot.resolve_event('email_sentx') is None
        assert bot.resolve_event('') is None

    def test_suppressed_events_do_not_resolve(self):
        assert bot.resolve_event('email_updated') is None


class TestEventAutocomplete:
    def test_matches_ignore_case(self):
        assert 'EMAIL_SENT' in bot.event_choices('sent')
        assert 'EMAIL_SENT' in bot.event_choices('Email_S')

    def test_prefix_matches_come_first(self):
        choices = bot.event_choices('email')
        assert choices[0].startswith('EMAIL_')
        # ACCOUNT_... contains no 'email'; ensure substring hits trail prefixes.
        prefixed = [c for c in choices if c.startswith('EMAIL_')]
        assert choices[:len(prefixed)] == prefixed

    def test_capped_at_discords_limit(self):
        assert len(bot.event_choices('')) == bot.AUTOCOMPLETE_LIMIT == 25

    def test_no_match_is_empty(self):
        assert bot.event_choices('zzz') == []

    def test_dm_from_anyone_else_is_ignored(self):
        assert bot.dm_reply(dm(), bot_user=object(), dm_user='nekromanyen') is None

    def test_no_dm_user_configured_ignores_every_dm(self):
        assert bot.dm_reply(dm(), bot_user=object(), dm_user=None) is None

    def test_help_says_filter_hides(self):
        assert 'hide' in bot.dm_help_text()


class TestDmUserConfig:
    def _env(self, **extra):
        env = {'DISCORD_BOT_TOKEN': 't', 'DISCORD_CHANNEL_ID': '1',
               'DISCORD_EVENT_CHANNEL_ID': '2', 'DISCORD_ADMIN_ROLE': 'r',
               'WARMBLY_API_TOKEN': 'w', 'WARMBLY_WEBSOCKET_URL': 'wss://h',
               'WARMBLY_ORG_ID': 'o'}
        env.update(extra)
        return env

    def test_read_from_env(self):
        with patch.dict('os.environ', self._env(DISCORD_DM_USER='nekromanyen'),
                        clear=True):
            assert bot.BotConfig.from_env().dm_user == 'nekromanyen'

    def test_optional(self):
        with patch.dict('os.environ', self._env(), clear=True):
            assert bot.BotConfig.from_env().dm_user is None


class TestDmAuthorization:
    def test_guild_uses_the_role(self):
        assert bot.is_allowed(member('CRMadmin'), object(), 'CRMadmin', 'x')
        assert not bot.is_allowed(member('everyone'), object(), 'CRMadmin', 'x')

    def test_dm_matches_the_configured_user(self):
        user = SimpleNamespace(name='nekromanyen')
        assert bot.is_allowed(user, None, 'CRMadmin', 'nekromanyen')

    def test_dm_match_ignores_case(self):
        user = SimpleNamespace(name='nekromanyen')
        assert bot.is_allowed(user, None, 'CRMadmin', 'Nekromanyen')

    def test_dm_from_someone_else_is_refused(self):
        user = SimpleNamespace(name='mallory')
        assert not bot.is_allowed(user, None, 'CRMadmin', 'nekromanyen')

    def test_dm_without_configured_user_is_refused(self):
        user = SimpleNamespace(name='nekromanyen')
        assert not bot.is_allowed(user, None, 'CRMadmin', None)

    def test_dm_role_does_not_count(self):
        """A role is a guild thing; it must not let a stranger in by DM."""
        assert not bot.is_allowed(member('CRMadmin'), None, 'CRMadmin', 'x')

    def test_tree_lets_the_dm_user_in(self):
        client = bot.build_client(make_cfg(dm_user='nekromanyen'), [])
        interaction = MagicMock()
        interaction.guild = None
        interaction.user = SimpleNamespace(name='nekromanyen')
        assert _run(client.bot_tree.interaction_check(interaction)) is True

    def test_tree_refuses_other_dm_users(self):
        client = bot.build_client(make_cfg(dm_user='nekromanyen'), [])
        interaction = MagicMock()
        interaction.guild = None
        interaction.user = SimpleNamespace(name='mallory')
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        assert _run(client.bot_tree.interaction_check(interaction)) is False
        assert interaction.response.send_message.await_args.kwargs[
            'ephemeral'] is True


class TestFeedSettings:
    def test_toggle_mutes_then_unmutes(self):
        s = bot.FeedSettings()
        assert s.toggle('EMAIL_SENT') is True
        assert s.is_muted('EMAIL_SENT')
        assert s.toggle('EMAIL_SENT') is False
        assert not s.is_muted('EMAIL_SENT')

    def test_round_trips_through_a_dict(self):
        s = bot.FeedSettings()
        s.toggle('EMAIL_SENT')
        s.formatters['EMAIL_REPLIED'] = '.subject'
        again = bot.FeedSettings.from_dict(s.to_dict())
        assert again.muted == {'EMAIL_SENT'}
        assert again.formatters == {'EMAIL_REPLIED': '.subject'}

    def test_stored_dict_is_json_friendly(self):
        s = bot.FeedSettings({'B', 'A'}, {'X': '.'})
        assert json.loads(json.dumps(s.to_dict())) == {
            'muted': ['A', 'B'], 'jq': {'X': '.'}}

    @pytest.mark.parametrize('raw', [None, 'junk', [], {'muted': 'x', 'jq': 3}])
    def test_garbage_reads_as_empty(self, raw):
        s = bot.FeedSettings.from_dict(raw)
        assert s.muted == set() and s.formatters == {}


class TestSettingsStorage:
    def test_loads_from_the_prefect_variable(self):
        stored = {'muted': ['EMAIL_SENT'], 'jq': {}}
        with patch('flow.discord_bot.Variable') as var:
            var.aget = AsyncMock(return_value=stored)
            s = _run(bot.load_settings())
        var.aget.assert_awaited_once_with(bot.SETTINGS_VARIABLE, default=None)
        assert s.muted == {'EMAIL_SENT'}

    def test_unreachable_server_reads_as_empty(self):
        with patch('flow.discord_bot.Variable') as var:
            var.aget = AsyncMock(side_effect=RuntimeError('down'))
            s = _run(bot.load_settings())
        assert s.muted == set()

    def test_saves_with_overwrite(self):
        s = bot.FeedSettings({'EMAIL_SENT'}, {})
        with patch('flow.discord_bot.Variable') as var:
            var.aset = AsyncMock()
            _run(bot.save_settings(s))
        var.aset.assert_awaited_once_with(
            bot.SETTINGS_VARIABLE, s.to_dict(), overwrite=True)

    def test_variable_name_is_valid_for_prefect(self):
        import re
        assert re.fullmatch(r'[a-z0-9_-]+', bot.SETTINGS_VARIABLE)


class TestJq:
    def test_valid_expression_compiles(self):
        assert _run(bot.check_jq('.subject')) is None

    def test_syntax_error_is_reported(self):
        assert 'error' in _run(bot.check_jq('.[')).lower()

    def test_runtime_only_errors_pass_the_check(self):
        """Checking must not evaluate: `test` errors on null at runtime."""
        assert _run(bot.check_jq('.subject | test("x")')) is None

    def test_runs_against_the_payload(self):
        assert _run(bot.run_jq('.subject', {'subject': 'hi'})) == 'hi'

    def test_non_string_output_is_compact_json(self):
        assert _run(bot.run_jq('{a: .x}', {'x': 1})) == '{"a":1}'

    def test_runtime_error_raises(self):
        with pytest.raises(bot.JqError):
            _run(bot.run_jq('.x | test("a")', {'x': 1}))


class TestFilterCommand:
    def test_toggles_and_saves(self):
        s, save = bot.FeedSettings(), AsyncMock()
        msg = _run(bot.filter_command(s, 'email_sent', save))
        assert s.is_muted('EMAIL_SENT') and 'EMAIL_SENT' in msg
        assert 'filter on' in msg and 'hidden' in msg
        save.assert_awaited_once_with(s)
        msg = _run(bot.filter_command(s, 'EMAIL_SENT', save))
        assert not s.is_muted('EMAIL_SENT')
        assert 'filter off' in msg and 'shown' in msg

    def test_unknown_event_changes_nothing(self):
        s, save = bot.FeedSettings(), AsyncMock()
        msg = _run(bot.filter_command(s, 'nope', save))
        assert 'unknown' in msg.lower() and s.muted == set()
        save.assert_not_awaited()

    def test_no_event_lists_muted(self):
        s = bot.FeedSettings({'EMAIL_SENT', 'EMAIL_OPENED'}, {})
        msg = _run(bot.filter_command(s, None, AsyncMock()))
        assert 'EMAIL_OPENED' in msg and 'EMAIL_SENT' in msg
        assert msg.lower().startswith('filtered (hidden)')

    def test_no_event_and_nothing_muted(self):
        msg = _run(bot.filter_command(bot.FeedSettings(), None, AsyncMock()))
        assert 'no events are filtered' in msg.lower()

    def test_save_failure_is_reported(self):
        s = bot.FeedSettings()
        save = AsyncMock(side_effect=RuntimeError('down'))
        msg = _run(bot.filter_command(s, 'EMAIL_SENT', save))
        assert 'not saved' in msg.lower()


class TestJqCommand:
    def test_stores_a_valid_formatter(self):
        s, save = bot.FeedSettings(), AsyncMock()
        msg = _run(bot.jq_command(s, 'email_replied', '.subject', save))
        assert s.formatters == {'EMAIL_REPLIED': '.subject'}
        assert 'EMAIL_REPLIED' in msg
        save.assert_awaited_once_with(s)

    def test_rejects_invalid_jq(self):
        s, save = bot.FeedSettings(), AsyncMock()
        msg = _run(bot.jq_command(s, 'EMAIL_REPLIED', '.[', save))
        assert s.formatters == {} and 'error' in msg.lower()
        save.assert_not_awaited()

    def test_unknown_event(self):
        s, save = bot.FeedSettings(), AsyncMock()
        msg = _run(bot.jq_command(s, 'nope', '.', save))
        assert 'unknown' in msg.lower() and s.formatters == {}

    def test_no_expression_shows_current(self):
        s = bot.FeedSettings(set(), {'EMAIL_REPLIED': '.subject'})
        msg = _run(bot.jq_command(s, 'EMAIL_REPLIED', None, AsyncMock()))
        assert '.subject' in msg

    def test_off_clears(self):
        s, save = bot.FeedSettings(set(), {'EMAIL_REPLIED': '.subject'}), AsyncMock()
        _run(bot.jq_command(s, 'EMAIL_REPLIED', 'off', save))
        assert s.formatters == {}
        save.assert_awaited_once()


class TestHandlersUseSettings:
    def _handlers(self, settings):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(gw, publish, settings=settings)
        return handlers, publish

    def test_muted_event_is_dropped(self):
        handlers, publish = self._handlers(bot.FeedSettings({'EMAIL_SENT'}, {}))
        _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        publish.assert_not_awaited()

    def test_unmuting_takes_effect_without_reregistering(self):
        settings = bot.FeedSettings({'EMAIL_SENT'}, {})
        handlers, publish = self._handlers(settings)
        settings.toggle('EMAIL_SENT')
        _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        publish.assert_awaited_once()

    def test_formatter_output_is_in_the_summary(self):
        settings = bot.FeedSettings(set(), {'EMAIL_SENT': '"to " + .email'})
        handlers, publish = self._handlers(settings)
        _run(handlers['EMAIL_SENT']('org:1', {'email': 'a@b.c'}))
        summary, _f, data = publish.await_args.args[0]
        assert 'EMAIL_SENT' in summary and 'to a@b.c' in summary
        # The attachment stays the whole payload.
        assert json.loads(data.decode()) == {'email': 'a@b.c'}

    def test_formatter_error_falls_back_to_the_default_line(self):
        settings = bot.FeedSettings(set(), {'EMAIL_SENT': '.email | test("x")'})
        handlers, publish = self._handlers(settings)
        _run(handlers['EMAIL_SENT']('org:1', {'email': 1}))
        summary, _f, _d = publish.await_args.args[0]
        assert 'EMAIL_SENT' in summary and 'jq' in summary.lower()

    def test_formatted_summary_fits_a_message(self):
        settings = bot.FeedSettings(set(), {'EMAIL_SENT': '.body'})
        handlers, publish = self._handlers(settings)
        _run(handlers['EMAIL_SENT']('org:1', {'body': 'x' * 5000}))
        summary, _f, _d = publish.await_args.args[0]
        assert len(summary) <= bot.DISCORD_MESSAGE_LIMIT


class TestNewCommandsRegistered:
    def test_filter_and_jq_are_built_in(self):
        client = bot.build_client(make_cfg(), ['campaign'])
        names = {c.name for c in client.bot_tree.get_commands()}
        assert {'filter', 'jq'} <= names
        assert {'filter', 'jq'} <= bot.RESERVED_NAMES

    def test_event_options_autocomplete(self):
        client = bot.build_client(make_cfg(), [])
        for name in ('filter', 'jq'):
            command = client.bot_tree.get_command(name)
            event = next(p for p in command.parameters if p.name == 'event')
            assert event.autocomplete, name

    def test_announcement_lists_them(self):
        msg = bot.format_announcement(make_cfg(), [])
        assert '/filter' in msg and '/jq' in msg


class TestEventStore:
    def _handlers(self, settings=None):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(gw, publish, settings=settings)
        return handlers, publish

    @pytest.fixture(autouse=True)
    def _dirs(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
        monkeypatch.delenv('PREFECT_DUMP_JSON', raising=False)
        self.store = tmp_path / 'discord'

    def test_store_is_data_discord(self):
        assert bot.event_store_dir() == self.store

    def test_every_event_is_written_to_the_store(self):
        handlers, _ = self._handlers()
        _run(handlers['EMAIL_SENT']('org:1', {'id': 7}))
        files = list(self.store.glob('*.json'))
        assert len(files) == 1
        assert json.loads(files[0].read_text()) == {'id': 7}
        assert 'email-sent' in files[0].name

    def test_events_in_the_same_second_do_not_overwrite(self):
        handlers, _ = self._handlers()
        for i in range(3):
            _run(handlers['EMAIL_SENT']('org:1', {'id': i}))
        assert len(list(self.store.glob('*.json'))) == 3

    def test_filtered_events_are_still_stored(self):
        """/filter hides an event from the feed; it is still recorded."""
        handlers, publish = self._handlers(bot.FeedSettings({'EMAIL_SENT'}, {}))
        _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        publish.assert_not_awaited()
        assert len(list(self.store.glob('*.json'))) == 1

    def test_warmup_is_still_stored(self):
        handlers, publish = self._handlers()
        _run(handlers['EMAIL_RECEIVED']('org:1', WARMUP))
        publish.assert_not_awaited()
        assert len(list(self.store.glob('*.json'))) == 1

    def test_write_failure_does_not_stop_the_feed(self, monkeypatch):
        handlers, publish = self._handlers()
        with patch('flow.discord_bot.Path.write_bytes',
                   side_effect=OSError('disk full')):
            _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        publish.assert_awaited_once()

    def test_no_prefect_artifact_when_unset(self):
        handlers, _ = self._handlers()
        with patch('flow.discord_bot.acreate_markdown_artifact',
                   new=AsyncMock()) as create:
            _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        create.assert_not_awaited()

    def test_prefect_artifact_when_set(self, monkeypatch):
        monkeypatch.setenv('PREFECT_DUMP_JSON', '1')
        handlers, _ = self._handlers()
        with patch('flow.discord_bot.acreate_markdown_artifact',
                   new=AsyncMock()) as create:
            _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        create.assert_awaited_once()
        kwargs = create.await_args.kwargs
        assert kwargs['key'] == 'discord-email-sent'
        assert '"id": 1' in kwargs['markdown']

    def test_artifact_failure_does_not_stop_the_feed(self, monkeypatch):
        monkeypatch.setenv('PREFECT_DUMP_JSON', '1')
        handlers, publish = self._handlers()
        with patch('flow.discord_bot.acreate_markdown_artifact',
                   new=AsyncMock(side_effect=RuntimeError('no api'))):
            _run(handlers['EMAIL_SENT']('org:1', {'id': 1}))
        publish.assert_awaited_once()


class TestJqMissingBinary:
    def test_check_reports_a_missing_jq(self):
        with patch('flow.discord_bot.JQ_BIN', '/nonexistent/jq'):
            error = _run(bot.check_jq('.'))
        assert error and 'jq' in error and 'not installed' in error

    def test_run_raises_jq_error_for_a_missing_jq(self):
        with patch('flow.discord_bot.JQ_BIN', '/nonexistent/jq'):
            with pytest.raises(bot.JqError):
                _run(bot.run_jq('.', {}))

    def test_command_replies_instead_of_crashing(self):
        s, save = bot.FeedSettings(), AsyncMock()
        with patch('flow.discord_bot.JQ_BIN', '/nonexistent/jq'):
            msg = _run(bot.jq_command(s, 'EMAIL_SENT', '.id', save))
        assert 'not installed' in msg and s.formatters == {}
        save.assert_not_awaited()


class TestJqPreview:
    @pytest.fixture(autouse=True)
    def _store(self, tmp_path, monkeypatch):
        monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
        self.store = tmp_path / 'discord'
        self.store.mkdir()

    def _event(self, name, stamp, payload):
        (self.store / f'{name}-{stamp}.json').write_text(json.dumps(payload))

    def test_latest_stored_event_is_found(self):
        self._event('email-sent', '20261023T100000000000', {'id': 1})
        self._event('email-sent', '20261023T110000000000', {'id': 2})
        self._event('email-sent-other', '20261023T120000000000', {'id': 9})
        assert bot.latest_event_payload('EMAIL_SENT') == {'id': 2}

    def test_no_stored_event_is_none(self):
        assert bot.latest_event_payload('EMAIL_SENT') is None

    def test_storing_shows_the_result_on_the_latest_event(self):
        self._event('email-sent', '20261023T100000000000', {'email': 'a@b.c'})
        s = bot.FeedSettings()
        msg = _run(bot.jq_command(s, 'email_sent', '"to " + .email', AsyncMock()))
        assert s.formatters == {'EMAIL_SENT': '"to " + .email'}
        assert 'to a@b.c' in msg

    def test_showing_the_formatter_also_shows_the_result(self):
        self._event('email-sent', '20261023T100000000000', {'id': 5})
        s = bot.FeedSettings(set(), {'EMAIL_SENT': '.id'})
        msg = _run(bot.jq_command(s, 'EMAIL_SENT', None, AsyncMock()))
        assert '.id' in msg and '5' in msg

    def test_runtime_error_on_the_sample_is_shown_but_still_stored(self):
        self._event('email-sent', '20261023T100000000000', {'x': 1})
        s = bot.FeedSettings()
        msg = _run(bot.jq_command(s, 'EMAIL_SENT', '.x | test("a")', AsyncMock()))
        assert 'EMAIL_SENT' in s.formatters
        assert 'error' in msg.lower()

    def test_no_sample_says_so(self):
        s = bot.FeedSettings()
        msg = _run(bot.jq_command(s, 'EMAIL_SENT', '.id', AsyncMock()))
        assert 'no stored' in msg.lower()

    def test_reply_fits_a_message(self):
        self._event('email-sent', '20261023T100000000000', {'b': 'x' * 5000})
        msg = _run(bot.jq_command(bot.FeedSettings(), 'EMAIL_SENT', '.b',
                                  AsyncMock()))
        assert len(msg) <= bot.DISCORD_MESSAGE_LIMIT
