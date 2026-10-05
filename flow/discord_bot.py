"""
Discord bot: a Warmbly event feed plus slash commands, run as a Prefect flow.

One long-running flow run owns two connections on one event loop:

  * the Warmbly realtime gateway (AsyncGatewayClient), subscribed to
    org:{WARMBLY_ORG_ID}; every event is posted to DISCORD_EVENT_CHANNEL_ID
  * the Discord gateway, serving the slash commands:

      /batchdraft [n]   trigger the selector -> research -> drafting pipeline
                        (BATCHDRAFT_DEPLOYMENT, default run-agents/run-agents)
                        with batch_size=n, default 50
      /auth             `warmbly auth login --web` device flow; the approval
                        link is relayed to the invoker
      /list             Prefect flows and their deployments
      /reload           reconnect the event feed with the current credential
      /<group> [args]   one command per `warmbly` CLI command group, discovered
                        from `warmbly --help` at startup and piped to the CLI

Environment:
  DISCORD_BOT_TOKEN                          required
  DISCORD_CHANNEL_ID                         required; startup announcement
  DISCORD_EVENT_CHANNEL_ID                   required; Warmbly event feed
  DISCORD_ADMIN_ROLE                         required; only members with this
                                             role (by exact name) may use any
                                             command
  DISCORD_GUILD_ID                           optional; sync commands to this
                                             guild instantly instead of globally
  WARMBLY_API_TOKEN, WARMBLY_WEBSOCKET_URL,
  WARMBLY_ORG_ID                             required (gateway)
  WARMBLY_API_URL                            /auth derives --hostname from it
  BATCHDRAFT_DEPLOYMENT                      optional
  WARMBLY_EVENT_INTENTS                      optional; comma-separated event
                                             families (EMAIL,CAMPAIGN,...) to
                                             forward; unset forwards all
  WARMBLY_REQUEST_TIMEOUT                    CLI command timeout, seconds

The bot runs the CLI as whatever user the Prefect worker runs as, so /auth
signs in that user's ~/.config/warmbly, which the piped commands then use.

Reloading: the event feed takes its token from `warmbly auth token` (the CLI's
active sign-in), falling back to WARMBLY_API_TOKEN. A successful /auth, the
/reload command, or SIGHUP to the process reconnects the feed with whatever
token is active then and re-resolves the org name, without restarting the
flow run. SIGHUP needs the handler is installed at import, on the main thread, so it works both run
directly and as a served deployment; the flow run logs the PID to signal.
"""

import asyncio
import io
import json
import os
import re
import shlex
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from prefect import flow

WARMBLY_BIN = shutil.which('warmbly') or 'warmbly'
DISCORD_MESSAGE_LIMIT = 2000
DEFAULT_BATCH = 50
DEFAULT_BATCHDRAFT_DEPLOYMENT = 'run-agents/run-agents'
CLI_TIMEOUT = float(os.environ.get('WARMBLY_REQUEST_TIMEOUT', '60'))
# GET /me resolves the caller's org name with any valid key; the
# /organization endpoint rejects API keys (session-only).
ME_PATH = '/v1/me'
# The device code expires after ten minutes; give the CLI a little longer so it
# reports the expiry itself.
AUTH_TIMEOUT = 11 * 60
# More chunks than this goes out as a file attachment instead.
MAX_CHUNKS = 5

# Not exposed as slash commands: auth has its own command; browse, completion,
# help and upgrade act on the bot host, and events streams until killed.
EXCLUDED_GROUPS = frozenset(
    {'auth', 'browse', 'completion', 'help', 'upgrade', 'events', 'warmbly'})
# Names this module registers itself.
RESERVED_NAMES = frozenset({'batchdraft', 'auth', 'list', 'reload'})

_GROUP_LINE = re.compile(r'^  ([a-z][a-z0-9-]*)\s+\S')
_DEVICE_CODE = re.compile(r'Your code:\s*(\S+)')
_DEVICE_URL = re.compile(r'Approve at:\s*(\S+)')


@dataclass(frozen=True)
class BotConfig:
    discord_token: str
    channel_id: int
    event_channel_id: int
    guild_id: Optional[int]
    admin_role: str
    warmbly_token: str
    gateway_url: str
    org_id: str
    batchdraft_deployment: str
    warmbly_api_url: Optional[str]
    event_intents: Optional[List[str]] = None

    @classmethod
    def from_env(cls) -> 'BotConfig':
        required = ['DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID',
                    'DISCORD_EVENT_CHANNEL_ID', 'DISCORD_ADMIN_ROLE',
                    'WARMBLY_API_TOKEN', 'WARMBLY_WEBSOCKET_URL',
                    'WARMBLY_ORG_ID']
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f'not set: {", ".join(missing)}')
        guild = os.environ.get('DISCORD_GUILD_ID')
        return cls(
            discord_token=os.environ['DISCORD_BOT_TOKEN'],
            channel_id=int(os.environ['DISCORD_CHANNEL_ID']),
            event_channel_id=int(os.environ['DISCORD_EVENT_CHANNEL_ID']),
            guild_id=int(guild) if guild else None,
            admin_role=os.environ['DISCORD_ADMIN_ROLE'],
            warmbly_token=os.environ['WARMBLY_API_TOKEN'],
            gateway_url=os.environ['WARMBLY_WEBSOCKET_URL'],
            org_id=os.environ['WARMBLY_ORG_ID'],
            batchdraft_deployment=os.environ.get(
                'BATCHDRAFT_DEPLOYMENT') or DEFAULT_BATCHDRAFT_DEPLOYMENT,
            warmbly_api_url=os.environ.get('WARMBLY_API_URL') or None,
            event_intents=parse_intents(
                os.environ.get('WARMBLY_EVENT_INTENTS')),
        )


# -- pure helpers ------------------------------------------------------------

def parse_intents(raw: Optional[str]) -> Optional[List[str]]:
    """'email, Campaign' -> ['EMAIL', 'CAMPAIGN']; blank -> None (no filter)."""
    tokens = [t.strip().upper() for t in (raw or '').split(',') if t.strip()]
    return tokens or None


def gateway_base_url(url: str) -> str:
    """AsyncGatewayClient appends /socket/websocket itself."""
    return url.rstrip('/').split('/socket/websocket')[0]


def auth_hostname(api_url: str) -> str:
    """Warmbly serves each tenant's API at api.<hostname>."""
    host = urlparse(api_url).hostname or api_url
    return host[len('api.'):] if host.startswith('api.') else host


def parse_cli_groups(help_text: str) -> List[str]:
    groups = []
    for line in help_text.splitlines():
        if line.startswith('Flags:'):
            break
        m = _GROUP_LINE.match(line)
        if m and m.group(1) not in EXCLUDED_GROUPS:
            groups.append(m.group(1))
    return groups


def parse_device_line(line: str) -> Optional[Tuple[str, str]]:
    if m := _DEVICE_URL.search(line):
        return 'url', m.group(1)
    if m := _DEVICE_CODE.search(line):
        return 'code', m.group(1)
    return None


def _fence(text: str) -> str:
    # A literal ``` in the output would close the code block early.
    return '```\n' + text.replace('```', '`​``') + '\n```'


def chunk_output(text: str) -> List[str]:
    text = text.rstrip() or '(no output)'
    room = DISCORD_MESSAGE_LIMIT - len(_fence(''))
    parts, current = [], ''
    for line in text.splitlines(keepends=True):
        while len(line) > room:
            if current:
                parts.append(current)
                current = ''
            parts.append(line[:room])
            line = line[room:]
        if len(current) + len(line) > room:
            parts.append(current)
            current = ''
        current += line
    if current:
        parts.append(current)
    return [_fence(p.rstrip('\n')) for p in parts]


class EventDisplayFilter:
    """Turns a raw gateway event into the Discord message posted for it."""

    def __init__(self, names: Optional[dict] = None) -> None:
        # id -> display name; longest first so no id is replaced inside another.
        self._names = sorted((names or {}).items(), key=lambda kv: -len(kv[0]))

    def _label(self, text: str) -> str:
        for ident, name in self._names:
            text = text.replace(ident, name)
        return text

    def _translate(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._label(value)
        if isinstance(value, dict):
            return {k: self._translate(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._translate(v) for v in value]
        return value

    def render(self, event: str, topic: str, payload: Any) -> str:
        head = f'**{event}** `{self._label(topic)}`\n'
        body = json.dumps(self._translate(payload), indent=2, default=str,
                          ensure_ascii=False)
        # A literal ``` would end the code block early.
        body = body.replace('```', '`\u200b``')
        room = DISCORD_MESSAGE_LIMIT - len(head) - len('```json\n\n```')
        if len(body) > room:
            marker = '\n… (truncated)'
            body = body[:room - len(marker)] + marker
        return f'{head}```json\n{body}\n```'


async def fetch_org_names(api_url: Optional[str], token: str) -> dict:
    """{organization_id: organization_name} from GET /v1/me, or {} on failure."""
    if not api_url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=CLI_TIMEOUT) as http:
            response = await http.get(
                api_url.rstrip('/') + ME_PATH,
                headers={'Authorization': f'Bearer {token}'})
            response.raise_for_status()
            me = response.json()
    except Exception as exc:
        # Events still flow, labelled by id, rather than the bot not starting.
        print(f'could not resolve org name: {type(exc).__name__}: {exc}')
        return {}
    org_id, name = me.get('organization_id'), me.get('organization_name')
    return {org_id: name} if org_id and name else {}


def format_announcement(cfg: 'BotConfig', cli_groups: List[str],
                        org_name: Optional[str] = None) -> str:
    head = (
        '**Warmbly bot online.**\n'
        f'Posting every Warmbly event from **{org_name or cfg.org_id}** to '
        f'<#{cfg.event_channel_id}>. Commands are for the '
        f'`{cfg.admin_role}` role.\n'
        + (f'Event families: {", ".join(cfg.event_intents)}.\n'
           if cfg.event_intents else '')
        + '\n'
        f'`/batchdraft [n]` select, research and draft a batch '
        f'(default {DEFAULT_BATCH}) via `{cfg.batchdraft_deployment}`\n'
        '`/auth` sign the Warmbly CLI in\n'
        '`/list` Prefect flows and deployments\n'
        '`/reload` reconnect the event feed with the current sign-in\n'
    )
    names = [g for g in cli_groups if g not in RESERVED_NAMES]
    if not names:
        return head
    intro = '\nWarmbly CLI, as `/<command> args:...`: '
    room = DISCORD_MESSAGE_LIMIT - len(head) - len(intro)
    listed = ''
    for i, name in enumerate(names):
        item = f'`/{name}`' if i == 0 else f' `/{name}`'
        more = f' … and {len(names) - i} more'
        if len(listed) + len(item) + len(more) > room:
            listed += more
            break
        listed += item
    return head + intro + listed


async def _resolve_channel(client: Any, channel_id: int) -> Any:
    return (client.get_channel(channel_id)
            or await client.fetch_channel(channel_id))


async def run_sender(
    client: Any, cfg: 'BotConfig', queue: 'asyncio.Queue', announcement: str
) -> None:
    """Announce once in the command channel, then feed the event channel."""
    await client.wait_until_ready()
    # Once per flow run: discord.py reconnects without re-running this.
    try:
        channel = await _resolve_channel(client, cfg.channel_id)
        await channel.send(announcement)
    except Exception as exc:
        print(f'could not post announcement: {type(exc).__name__}: {exc}')
    events = await _resolve_channel(client, cfg.event_channel_id)
    while True:
        message = await queue.get()
        try:
            await events.send(message)
        except Exception as exc:
            print(f'could not post event: {type(exc).__name__}: {exc}')


def has_role(user: Any, role_name: str) -> bool:
    """Exact role-name match; a DM user (a User, not a Member) has no roles."""
    return any(r.name == role_name for r in getattr(user, 'roles', ()))


async def check_role(interaction: Any, role_name: str) -> bool:
    if has_role(interaction.user, role_name):
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f'This bot is limited to the `{role_name}` role.', ephemeral=True)
    return False

# -- command logic -----------------------------------------------------------

async def run_cli(group: str, args: str) -> Tuple[int, str]:
    """Run `warmbly <group> <args>` without a shell; return (code, output)."""
    try:
        argv = shlex.split(args or '')
    except ValueError as exc:
        return 2, f'could not parse arguments: {exc}'
    proc = await asyncio.create_subprocess_exec(
        WARMBLY_BIN, group, *argv, '--no-color',
        # Closed stdin makes a confirmation prompt fail fast instead of
        # hanging until the timeout.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f'timed out after {CLI_TIMEOUT:g}s'
    text = out.decode(errors='replace')
    if err:
        text += err.decode(errors='replace')
    return proc.returncode, text


async def trigger_batchdraft(
    n: Optional[int],
    *,
    deployment: str,
    run_deployment: Optional[Callable[..., Awaitable[Any]]] = None,
) -> str:
    if run_deployment is None:
        from prefect.deployments import run_deployment
    batch = n or DEFAULT_BATCH
    flow_run = await run_deployment(
        name=deployment,
        parameters={'batch_size': batch},
        timeout=0,
        as_subflow=False,
    )
    return (f'Started `{deployment}` with batch_size={batch}: '
            f'flow run `{flow_run.name}` ({flow_run.id})')


async def auth_login(
    hostname: str,
    on_prompt: Callable[[str, Optional[str]], Awaitable[None]],
    api_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """Run the CLI's device flow, calling on_prompt(url, code) once."""
    argv = [WARMBLY_BIN, 'auth', 'login', '--web', '--force',
            '--hostname', hostname, '--no-color']
    if api_url:
        argv += ['--api-url', api_url]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def consume() -> List[str]:
        lines: List[str] = []
        code = None
        prompted = False
        while raw := await proc.stdout.readline():
            line = raw.decode(errors='replace').rstrip()
            parsed = parse_device_line(line)
            if parsed and parsed[0] == 'code':
                code = parsed[1]
            elif parsed and parsed[0] == 'url' and not prompted:
                prompted = True
                await on_prompt(parsed[1], code)
            elif line.strip():
                lines.append(line)
        await proc.wait()
        return lines

    try:
        lines = await asyncio.wait_for(consume(), AUTH_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, 'timed out waiting for approval'
    return proc.returncode == 0, '\n'.join(lines)


async def list_flows(client: Any) -> str:
    flows = await client.read_flows()
    deployments = await client.read_deployments()
    by_flow: dict = {}
    for d in deployments:
        by_flow.setdefault(d.flow_id, []).append(d)
    lines = []
    for f in sorted(flows, key=lambda f: f.name):
        deps = by_flow.get(f.id, [])
        if not deps:
            lines.append(f'{f.name}  (no deployment)')
        for d in sorted(deps, key=lambda d: d.name):
            state = '  [paused]' if getattr(d, 'paused', False) else ''
            lines.append(f'{f.name}/{d.name}{state}')
    return '\n'.join(lines) or 'no flows registered'


def register_event_handlers(
    gw: Any,
    publish: Callable[[str], Awaitable[None]],
    display: Optional[EventDisplayFilter] = None,
) -> None:
    """Forward every named business event; the SDK has no wildcard handler."""
    from warmbly.gateway import GatewayEvent

    # Business events are UPPER_CASE. The lowercase values (presence_state,
    # presence_diff, rate_limited, resumed, resume_failed) are transport
    # frames: presence is dashboard users coming and going.
    names = sorted({v for k, v in vars(GatewayEvent).items()
                    if k.isupper() and isinstance(v, str) and v.isupper()})
    display = display or EventDisplayFilter()
    for name in names:
        async def handler(topic: str, payload: Any, _name: str = name) -> None:
            await publish(display.render(_name, topic, payload))
        gw.on_event(name)(handler)


# -- Discord wiring ----------------------------------------------------------

async def _cli_help() -> str:
    proc = await asyncio.create_subprocess_exec(
        WARMBLY_BIN, '--help', stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT)
    return out.decode(errors='replace')


async def _send_output(interaction: Any, text: str, ephemeral: bool) -> None:
    import discord

    parts = chunk_output(text)
    if len(parts) > MAX_CHUNKS:
        await interaction.followup.send(
            file=discord.File(io.BytesIO(text.encode()), 'output.txt'),
            ephemeral=ephemeral)
        return
    for part in parts:
        await interaction.followup.send(part, ephemeral=ephemeral)


def build_client(cfg: BotConfig, cli_groups: List[str],
                 reloader: Optional['Reloader'] = None) -> Any:
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.default())
    role = cfg.admin_role

    class RoleGatedTree(app_commands.CommandTree):
        # Runs before every command, including the CLI ones added below.
        async def interaction_check(self, interaction):
            return await check_role(interaction, role)

    tree = RoleGatedTree(client)
    client.bot_tree = tree
    guild = discord.Object(cfg.guild_id) if cfg.guild_id else None

    @tree.command(name='batchdraft',
                  description='Select, research and draft a batch of prospects')
    @app_commands.describe(n=f'batch size (default {DEFAULT_BATCH})')
    async def batchdraft(interaction: discord.Interaction,
                         n: Optional[app_commands.Range[int, 1, 1000]] = None):
        await interaction.response.defer(thinking=True)
        try:
            msg = await trigger_batchdraft(
                n, deployment=cfg.batchdraft_deployment)
        except Exception as exc:
            msg = f'could not start batch: {type(exc).__name__}: {exc}'
        await interaction.followup.send(msg)

    @tree.command(name='auth', description='Sign the Warmbly CLI in (device flow)')
    async def auth(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not cfg.warmbly_api_url:
            await interaction.followup.send(
                'WARMBLY_API_URL is not set', ephemeral=True)
            return

        async def on_prompt(url: str, code: Optional[str]) -> None:
            suffix = f' and confirm the code `{code}`' if code else ''
            await interaction.followup.send(
                f'Approve this sign-in: {url}{suffix}. '
                'The code expires in 10 minutes.', ephemeral=True)

        ok, out = await login_and_reload(
            auth_hostname(cfg.warmbly_api_url), on_prompt,
            reloader or Reloader(), cfg.warmbly_api_url)
        await interaction.followup.send(
            ('Signed in; reconnecting the event feed with the new sign-in.'
             if ok else 'Sign-in failed.')
            + (f'\n{chunk_output(out)[0]}' if out else ''),
            ephemeral=True)

    @tree.command(name='reload',
                  description='Reconnect the event feed with the current sign-in')
    async def reload_cmd(interaction: discord.Interaction):
        if reloader is None:
            await interaction.response.send_message(
                'Reload is not wired up in this process.', ephemeral=True)
            return
        reloader.request(f'/reload by {interaction.user}')
        await interaction.response.send_message(
            'Reconnecting the event feed.', ephemeral=True)

    @tree.command(name='list', description='Prefect flows and deployments')
    async def list_cmd(interaction: discord.Interaction):
        from prefect.client.orchestration import get_client

        await interaction.response.defer(thinking=True)
        async with get_client() as pc:
            text = await list_flows(pc)
        await _send_output(interaction, text, ephemeral=False)

    def make_cli_command(group: str) -> app_commands.Command:
        @app_commands.describe(args=f'arguments, as for `warmbly {group} ...`')
        async def run(interaction: discord.Interaction, args: str = ''):
            # Ephemeral: CLI output can contain contact data or keys.
            await interaction.response.defer(ephemeral=True, thinking=True)
            code, out = await run_cli(group, args)
            if code != 0:
                out = f'(exit {code})\n{out}'
            await _send_output(interaction, out, ephemeral=True)

        command = app_commands.Command(
            name=group, description=f'warmbly {group}'[:100], callback=run)
        return command

    for group in cli_groups:
        if group not in RESERVED_NAMES:
            tree.add_command(make_cli_command(group))

    if guild:
        tree.copy_global_to(guild=guild)

    async def setup_hook() -> None:
        synced = await tree.sync(guild=guild)
        print(f'synced {len(synced)} slash command(s) '
              f'{"to guild " + str(cfg.guild_id) if guild else "globally"}')

    client.setup_hook = setup_hook
    return client


class Reloader:
    """A reload request that any thread or signal handler may raise."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event: Optional[asyncio.Event] = None
        self._reason = ''

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    def request(self, reason: str) -> None:
        if self._loop is None or self._event is None:
            return
        self._reason = reason
        # call_soon_threadsafe: SIGHUP lands on the main thread, while a
        # served flow runs its event loop on another one.
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> str:
        await self._event.wait()
        self._event.clear()
        return self._reason


def install_sighup(reloader: Any) -> bool:
    try:
        signal.signal(signal.SIGHUP,
                      lambda signum, frame: reloader.request('SIGHUP'))
    except ValueError:
        # signal.signal is main-thread only.
        return False
    return True


# Installed at import, which Prefect does on the main thread of the flow run's
# process. The async flow body itself then runs on Prefect's
# RunSyncEventLoopThread, where signal.signal would be refused; the flow
# attaches its loop to this reloader instead.
RELOADER = Reloader()
_SIGHUP_INSTALLED = install_sighup(RELOADER)


async def active_warmbly_token(fallback: str) -> str:
    """The CLI's signed-in token (what /auth just wrote), else the env token."""
    try:
        proc = await asyncio.create_subprocess_exec(
            WARMBLY_BIN, 'auth', 'token', stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return fallback
    token = out.decode(errors='replace').strip()
    return token if proc.returncode == 0 and token else fallback


async def login_and_reload(
    hostname: str,
    on_prompt: Callable[[str, Optional[str]], Awaitable[None]],
    reloader: Any,
    api_url: Optional[str],
) -> Tuple[bool, str]:
    ok, out = await auth_login(hostname, on_prompt, api_url=api_url)
    if ok:
        reloader.request('/auth')
    return ok, out


async def _connect_gateway(gw: Any, cfg: BotConfig) -> None:
    await gw.connect()
    # Intents filter on the server; they do not stop presence frames.
    await gw.subscribe(f'org:{cfg.org_id}', intents=cfg.event_intents)
    print(f'subscribed to org:{cfg.org_id} '
          f'intents={cfg.event_intents or "all"}')
    await gw.run_forever()


async def _backoff(reloader: 'Reloader', delay: float) -> None:
    """Sleep before retrying, but a reload request (e.g. /auth) ends it now."""
    try:
        reason = await asyncio.wait_for(reloader.wait(), delay)
        print(f'retrying now ({reason})')
    except asyncio.TimeoutError:
        pass


async def run_gateway(
    cfg: BotConfig,
    queue: asyncio.Queue,
    reloader: Reloader,
    refresh: Callable[[], Awaitable[Tuple[str, EventDisplayFilter]]],
    gateway_factory: Optional[Callable[..., Any]] = None,
    retry_delay: float = 30.0,
) -> None:
    """
    One gateway connection per generation; a reload request ends the current
    one and starts the next with a freshly read token and org name.
    """
    if gateway_factory is None:
        from warmbly.gateway import AsyncGatewayClient

        def gateway_factory(token, base_url):
            return AsyncGatewayClient(token=token, base_url=base_url)

    while True:
        try:
            token, display = await refresh()
        except Exception as exc:
            print(f'reload failed: {type(exc).__name__}: {exc}; '
                  f'retrying in {retry_delay:g}s')
            await _backoff(reloader, retry_delay)
            continue

        gw = gateway_factory(token, gateway_base_url(cfg.gateway_url))
        register_event_handlers(gw, queue.put, display)
        connection = asyncio.create_task(_connect_gateway(gw, cfg))
        reload_wait = asyncio.create_task(reloader.wait())
        try:
            done, _ = await asyncio.wait(
                {connection, reload_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            reload_wait.cancel()
            connection.cancel()
            await gw.close()
        if reload_wait in done and not reload_wait.cancelled():
            print(f'reloading event feed ({reload_wait.result()})')
            continue
        # The connection ended on its own: the SDK supervises reconnects, so
        # this is a fatal error (e.g. a revoked token). Retry, as a reload.
        exc = connection.exception() if not connection.cancelled() else None
        print(f'gateway stopped: {type(exc).__name__ if exc else "closed"}: '
              f'{exc}; retrying in {retry_delay:g}s')
        await _backoff(reloader, retry_delay)


@flow(log_prints=True)
async def discord_bot() -> None:
    """Run the bot until cancelled."""
    cfg = BotConfig.from_env()
    groups = parse_cli_groups(await _cli_help())
    print(f'warmbly command groups: {", ".join(groups)}')

    reloader = RELOADER
    reloader.attach(asyncio.get_running_loop())
    print(f'reload: /reload, successful /auth'
          + (f', or kill -HUP {os.getpid()}' if _SIGHUP_INSTALLED else ''))

    async def refresh() -> Tuple[str, EventDisplayFilter]:
        token = await active_warmbly_token(cfg.warmbly_token)
        source = 'env' if token == cfg.warmbly_token else 'warmbly auth'
        names = await fetch_org_names(cfg.warmbly_api_url, token)
        print(f'credential from {source}; org {cfg.org_id} -> '
              f'{names.get(cfg.org_id) or "(name unresolved)"}')
        return token, EventDisplayFilter(names)

    # Resolved once for the announcement; reloads re-resolve per generation.
    first_token = await active_warmbly_token(cfg.warmbly_token)
    org_name = (await fetch_org_names(cfg.warmbly_api_url, first_token)
                ).get(cfg.org_id)

    # The queue and sender outlive gateway generations, so a reload neither
    # re-announces nor drops events already queued.
    queue: asyncio.Queue = asyncio.Queue()
    client = build_client(cfg, groups, reloader=reloader)
    async with client:
        await asyncio.gather(
            client.start(cfg.discord_token),
            run_sender(client, cfg, queue,
                       format_announcement(cfg, groups, org_name)),
            run_gateway(cfg, queue, reloader, refresh))


if __name__ == '__main__':
    asyncio.run(discord_bot())
