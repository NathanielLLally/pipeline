"""
Unit tests for the Discord bot flow.

The Discord and Warmbly connections are never opened here: the command logic
lives in plain async functions that take their collaborators as arguments, and
those are what is tested. The discord.py wiring around them is thin.
"""

import asyncio
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


def _run(coro):
    return asyncio.run(coro)


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

    def test_every_gateway_event_is_published(self):
        from warmbly.gateway import GatewayEvent
        gw = MagicMock()
        registered = []
        gw.on_event.side_effect = lambda name: (
            lambda handler: registered.append(name) or handler)
        bot.register_event_handlers(gw, AsyncMock())

        names = {v for k, v in vars(GatewayEvent).items()
                 if k.isupper() and isinstance(v, str)}
        assert names <= set(registered)

    def test_handler_publishes_formatted_event(self):
        gw = MagicMock()
        handlers = {}
        gw.on_event.side_effect = lambda name: (
            lambda h: handlers.setdefault(name, h))
        publish = AsyncMock()
        bot.register_event_handlers(gw, publish)

        _run(handlers['EMAIL_REPLIED']('org:1', {'contact_id': 'c1'}))
        publish.assert_awaited_once()
        assert 'EMAIL_REPLIED' in publish.await_args.args[0]


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
        sent = publish.await_args.args[0]
        assert 'EMAIL_REPLIED' in sent and ORG_NAME in sent
        assert ORG_ID not in sent


class TestAnnouncementUsesOrgName:
    def test_names_the_org_instead_of_its_id(self):
        msg = bot.format_announcement(make_cfg(org_id=ORG_ID), [],
                                      org_name=ORG_NAME)
        assert ORG_NAME in msg and ORG_ID not in msg

    def test_falls_back_to_the_id(self):
        msg = bot.format_announcement(make_cfg(org_id=ORG_ID), [])
        assert ORG_ID in msg
