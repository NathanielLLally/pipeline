"""
Discord bot: a Warmbly event feed plus slash commands, run as a Prefect flow.

One long-running flow run owns two connections on one event loop:

  * the Warmbly realtime gateway (AsyncGatewayClient), subscribed to
    org:{WARMBLY_ORG_ID}; events are posted to DISCORD_EVENT_CHANNEL_ID,
    except EMAIL_UPDATED (per-message flag churn) and EMAIL_RECEIVED for
    Instantly warmup mail
  * the Discord gateway, serving the slash commands:

      /batchdraft [n]   trigger the selector -> research -> drafting pipeline
                        (BATCHDRAFT_DEPLOYMENT, default run-agents/run-agents)
                        with batch_size=n, default 50
      /auth             `warmbly auth login --web` device flow; the approval
                        link is relayed to the invoker
      /list             Prefect flows and their deployments
      /reload           reconnect the event feed with the current credential
      /filter [event]   toggle a filter on an event: filtered events are not
                        posted; no event lists the filtered ones
      /jq event [expr]  format the event's feed line with a jq expression;
                        `off` clears it, no expr shows the current one
      /<group> [args]   one command per `warmbly` CLI command group, discovered
                        from `warmbly --help` at startup and piped to the CLI

    and answering a direct message from DISCORD_DM_USER with a pointer to
    /filter and /jq.

Every event's JSON, posted or not, is saved in data/discord (ARTIFACT_DIR/
discord), and also registered as a Prefect artifact (key discord-<event>)
when PREFECT_DUMP_JSON is set.

/filter and /jq settings are kept in the Prefect Variable discord_bot_feed,
so they survive restarts of the bot and of the machine.

Environment:
  DISCORD_BOT_TOKEN                          required
  DISCORD_CHANNEL_ID                         required; startup announcement
  DISCORD_EVENT_CHANNEL_ID                   required; Warmbly event feed
  DISCORD_ADMIN_ROLE                         required; only members with this
                                             role (by exact name) may use any
                                             command
  DISCORD_DM_USER                            optional; the Discord username
                                             allowed to use commands by DM
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
  INSTANTLY_WARMUP_TAGS                      optional; comma-separated
                                             Instantly warmup filter tags;
                                             EMAIL_RECEIVED carrying one is
                                             not posted (default 33153D9,
                                             empty disables the filter)

The bot runs the CLI as whatever user the Prefect worker runs as, so /auth
signs in that user's ~/.config/warmbly, which the piped commands then use.

Reloading: the event feed takes its token from `warmbly auth token` (the CLI's
active sign-in), falling back to WARMBLY_API_TOKEN. A successful /auth or the
/reload command reconnects the feed with whatever token is active then and
re-resolves the org name, without restarting the flow run.

There is deliberately no SIGHUP handler: Prefect can load the flow off the
main thread, where signal.signal is refused, so it would work under some
loaders and silently not under others (where SIGHUP then kills the bot).
"""

import asyncio
import io
import json
from datetime import datetime, timezone
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from prefect import flow
from prefect.client.orchestration import get_client
from prefect.artifacts import acreate_markdown_artifact
from prefect.variables import Variable

from flow.artifacts import artifact_dir, dump_json_enabled
from flow.discord_bot_reaper import reap, self_tags

WARMBLY_BIN = shutil.which('warmbly') or 'warmbly'
DISCORD_MESSAGE_LIMIT = 2000
DEFAULT_BATCH = 50
DEFAULT_BATCHDRAFT_DEPLOYMENT = 'run-agents/run-agents'
BOT_DEPLOYMENT = os.environ.get('DISCORD_BOT_DEPLOYMENT') or 'discord-bot/discord-bot'
CLI_TIMEOUT = float(os.environ.get('WARMBLY_REQUEST_TIMEOUT', '60'))
# GET /me resolves the caller's org name with any valid key; the
# /organization endpoint rejects API keys (session-only).
ME_PATH = '/v1/me'
# The device code expires after ten minutes; give the CLI a little longer so it
# reports the expiry itself.
AUTH_TIMEOUT = 11 * 60
# More chunks than this goes out as a file attachment instead.
MAX_CHUNKS = 5
# Discord shows at most this many autocomplete choices.
AUTOCOMPLETE_LIMIT = 25
# /filter and /jq settings, kept on the Prefect server.
SETTINGS_VARIABLE = 'discord_bot_feed'
JQ_BIN = shutil.which('jq') or 'jq'
JQ_TIMEOUT = 5.0

# Not exposed as slash commands: auth has its own command; browse, completion,
# help and upgrade act on the bot host, and events streams until killed.
EXCLUDED_GROUPS = frozenset(
    {'auth', 'browse', 'completion', 'help', 'upgrade', 'events', 'warmbly'})
# Names this module registers itself.
RESERVED_NAMES = frozenset(
    {'batchdraft', 'auth', 'list', 'reload', 'filter', 'jq'})

# Fires on every flag/folder change of every message, warmup included.
SUPPRESSED_EVENTS = frozenset({'EMAIL_UPDATED'})
DEFAULT_WARMUP_TAGS = ('33153D9',)

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
    dm_user: Optional[str] = None

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
            dm_user=os.environ.get('DISCORD_DM_USER') or None,
        )


# -- pure helpers ------------------------------------------------------------

def parse_intents(raw: Optional[str]) -> Optional[List[str]]:
    """'email, Campaign' -> ['EMAIL', 'CAMPAIGN']; blank -> None (no filter)."""
    tokens = [t.strip().upper() for t in (raw or '').split(',') if t.strip()]
    return tokens or None


def parse_warmup_tags(raw: Optional[str]) -> Tuple[str, ...]:
    if raw is None:
        return DEFAULT_WARMUP_TAGS
    return tuple(t.strip() for t in raw.split(',') if t.strip())


WARMUP_TAGS = parse_warmup_tags(os.environ.get('INSTANTLY_WARMUP_TAGS'))


def is_warmup(payload: Any, tags: Tuple[str, ...] = WARMUP_TAGS) -> bool:
    """
    Whether an email payload is Instantly warmup traffic.

    Instantly appends the pool's filter tag to the subject and the body
    (`Coffee? | warmup 33153D9`); the word before it varies, so only the tag
    is matched, as a whole token.
    """
    if not isinstance(payload, dict) or not tags:
        return False
    text = ' '.join(str(payload.get(k) or '') for k in ('subject', 'preview'))
    return any(re.search(rf'(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])',
                         text) for t in tags)


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


def event_slug(event: str) -> str:
    """'EMAIL_SENT' -> 'email-sent', as used in stored file names."""
    return re.sub(r'[^a-z0-9]+', '-', event.lower()).strip('-') or 'event'


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

    def event_summary(self, event: str, topic: str, payload: Any) -> str:
        """
        The one line that goes in the channel, with the JSON attached.

        Deliberately short: this is what you scan to decide whether an event
        is worth opening. The payload itself rides along as a file, so nothing
        has to be truncated to fit Discord's message limit.
        """
        head = f'**{event}** `{self._label(topic)}`'
        hint = ''
        if isinstance(payload, dict):
            # Whichever identifier is present, so the line says *which* thing.
            for key in ('email', 'name', 'subject', 'id', 'contact_id',
                        'campaign_id'):
                value = payload.get(key)
                if isinstance(value, (str, int)) and str(value):
                    hint = f' · {key}=`{self._label(str(value))[:120]}`'
                    break
        line = head + hint
        return line[:DISCORD_MESSAGE_LIMIT]

    def event_attachment(self, event: str, topic: str,
                         payload: Any) -> tuple:
        """
        (filename, bytes) carrying the whole payload as JSON.

        No truncation and no ``` escaping: neither is needed once this is a
        file rather than a fenced block in a message.
        """
        body = json.dumps(self._translate(payload), indent=2, default=str,
                          ensure_ascii=False)
        slug = event_slug(event)
        # Microseconds: the file is also stored, and must not overwrite one
        # from the same second.
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        return f'{slug}-{stamp}.json', body.encode()

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
        f'Posting Warmbly events from **{org_name or cfg.org_id}** to '
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
        '`/filter [event]` hide an event from the feed (again to show it)\n'
        '`/jq event [expr]` format an event\'s feed line with jq\n'
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
    # Imported here, not at module scope, matching the rest of this file: the
    # module must stay importable without discord.py installed.
    import discord

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
            if isinstance(message, tuple):
                # (summary, filename, json bytes): the payload goes up as an
                # attachment so it is clickable and never truncated.
                content, filename, data = message
                await events.send(
                    content,
                    file=discord.File(io.BytesIO(data), filename))
            else:
                await events.send(message)
        except Exception as exc:
            print(f'could not post event: {type(exc).__name__}: {exc}')


def has_role(user: Any, role_name: str) -> bool:
    """Exact role-name match; a DM user (a User, not a Member) has no roles."""
    return any(r.name == role_name for r in getattr(user, 'roles', ()))


def is_allowed(user: Any, guild: Any, role_name: str,
               dm_user: Optional[str]) -> bool:
    """In a guild, the role; in a DM, only DISCORD_DM_USER by username."""
    if guild is not None:
        return has_role(user, role_name)
    name = getattr(user, 'name', None)
    return bool(dm_user and name and name.lower() == dm_user.lower())


async def check_role(interaction: Any, role_name: str,
                     dm_user: Optional[str] = None) -> bool:
    if is_allowed(interaction.user, interaction.guild, role_name, dm_user):
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f'This bot is limited to the `{role_name}` role.', ephemeral=True)
    return False

def dm_help_text() -> str:
    return (
        'You can `/filter (event)` to hide an event (again to show it), or '
        'store an output formatter for an event with '
        '`/jq (event) (jq syntax)`.')


def dm_reply(message: Any, bot_user: Any,
             dm_user: Optional[str]) -> Optional[str]:
    """The help text for DISCORD_DM_USER's direct message; else None."""
    author = message.author
    if message.guild is not None or author == bot_user or getattr(
            author, 'bot', False):
        return None
    if not is_allowed(author, None, '', dm_user):
        return None
    return dm_help_text()


# -- feed settings (/filter, /jq) --------------------------------------------

class FeedSettings:
    """Muted event names and per-event jq formatters, shared by the feed."""

    def __init__(self, muted: Optional[set] = None,
                 formatters: Optional[dict] = None) -> None:
        self.muted = set(muted or ())
        self.formatters = dict(formatters or {})

    def is_muted(self, event: str) -> bool:
        return event in self.muted

    def toggle(self, event: str) -> bool:
        """Flip the event; True when it is now muted."""
        if event in self.muted:
            self.muted.discard(event)
            return False
        self.muted.add(event)
        return True

    def to_dict(self) -> dict:
        return {'muted': sorted(self.muted), 'jq': dict(self.formatters)}

    @classmethod
    def from_dict(cls, raw: Any) -> 'FeedSettings':
        if not isinstance(raw, dict):
            return cls()
        muted, jq = raw.get('muted'), raw.get('jq')
        return cls(
            {m for m in muted if isinstance(m, str)}
            if isinstance(muted, list) else set(),
            {k: v for k, v in jq.items()
             if isinstance(k, str) and isinstance(v, str)}
            if isinstance(jq, dict) else {})


async def load_settings() -> FeedSettings:
    try:
        raw = await Variable.aget(SETTINGS_VARIABLE, default=None)
    except Exception as exc:
        # The feed still runs, unfiltered, rather than the bot not starting.
        print(f'could not load feed settings: {type(exc).__name__}: {exc}')
        return FeedSettings()
    return FeedSettings.from_dict(raw)


async def save_settings(settings: FeedSettings) -> None:
    await Variable.aset(SETTINGS_VARIABLE, settings.to_dict(), overwrite=True)


class JqError(Exception):
    pass


async def _jq(args: List[str], stdin: bytes) -> Tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            JQ_BIN, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        # The bot's container image once lacked jq: every /jq then raised
        # here and Discord got no reply at all.
        return 127, (f'jq is not installed on the bot host ({JQ_BIN}): '
                     f'{type(exc).__name__}')
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), JQ_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, f'jq timed out after {JQ_TIMEOUT:g}s'
    return proc.returncode, out.decode(errors='replace').strip()


async def check_jq(expr: str) -> Optional[str]:
    """
    None if the expression compiles, else jq's error.

    Wrapped in `if false` so it is compiled but never evaluated: many valid
    filters fail at runtime on a sample input.
    """
    code, out = await _jq(['-n', f'if false then ({expr}) else empty end'], b'')
    return None if code == 0 else (out or f'jq error (exit {code})')


async def run_jq(expr: str, payload: Any) -> str:
    """The filter's output: strings raw, everything else compact JSON."""
    data = json.dumps(payload, default=str, ensure_ascii=False).encode()
    code, out = await _jq(['-r', '-c', expr], data)
    if code != 0:
        raise JqError(out or f'exit {code}')
    return out


async def _save_note(settings: FeedSettings,
                     save: Callable[[FeedSettings], Awaitable[None]]) -> str:
    try:
        await save(settings)
    except Exception as exc:
        return f' (not saved, lost on restart: {type(exc).__name__}: {exc})'
    return ''


async def filter_command(settings: FeedSettings, event: Optional[str],
                         save: Callable[[FeedSettings], Awaitable[None]]) -> str:
    if not event:
        if not settings.muted:
            return 'No events are filtered.'
        return 'Filtered (hidden): ' + ', '.join(
            f'`{e}`' for e in sorted(settings.muted))
    name = resolve_event(event)
    if name is None:
        return f'Unknown event `{event}`.'
    muted = settings.toggle(name)
    state = ('filter on: hidden from the feed' if muted
             else 'filter off: shown in the feed')
    return (f'`{name}` {state}.'
            + await _save_note(settings, save))


def latest_event_payload(event: str) -> Any:
    """The payload of the newest stored file for `event`, or None."""
    pattern = re.compile(rf'{re.escape(event_slug(event))}-\d{{8}}T\d+\.json')
    try:
        names = [p for p in event_store_dir().iterdir()
                 if pattern.fullmatch(p.name)]
        if not names:
            return None
        # The name ends in a UTC timestamp, so the largest is the newest.
        return json.loads(max(names, key=lambda p: p.name).read_text())
    except (OSError, ValueError):
        return None


PREVIEW_LIMIT = 1500


async def _jq_preview(event: str, expr: str) -> str:
    """The formatter's output on the latest stored event, for the reply."""
    payload = latest_event_payload(event)
    if payload is None:
        return f'\nNo stored `{event}` event to try it on yet.'
    try:
        out, head = await run_jq(expr, payload), 'On the latest stored event:'
    except JqError as exc:
        out, head = str(exc), 'jq error on the latest stored event:'
    if len(out) > PREVIEW_LIMIT:
        out = out[:PREVIEW_LIMIT] + '\n… (truncated)'
    return f'\n{head}\n{_fence(out or "(empty output)")}'


async def jq_command(settings: FeedSettings, event: str, expr: Optional[str],
                     save: Callable[[FeedSettings], Awaitable[None]]) -> str:
    name = resolve_event(event)
    if name is None:
        return f'Unknown event `{event}`.'
    if not expr:
        current = settings.formatters.get(name)
        if not current:
            return f'`{name}` has no formatter.'
        msg = f'`{name}`: `{current}`' + await _jq_preview(name, current)
        return msg[:DISCORD_MESSAGE_LIMIT]
    if expr.strip().lower() == 'off':
        settings.formatters.pop(name, None)
        msg = f'`{name}` formatter cleared.'
    else:
        error = await check_jq(expr)
        if error:
            return f'Not stored; jq error:\n{chunk_output(error)[0]}'
        settings.formatters[name] = expr
        msg = (f'`{name}` now formatted with `{expr}`.'
               + await _jq_preview(name, expr))
    return (msg + await _save_note(settings, save))[:DISCORD_MESSAGE_LIMIT]

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


def forwarded_events() -> List[str]:
    """Every event name the feed forwards, sorted."""
    from warmbly.gateway import GatewayEvent

    # Business events are UPPER_CASE. The lowercase values (presence_state,
    # presence_diff, rate_limited, resumed, resume_failed) are transport
    # frames: presence is dashboard users coming and going.
    return sorted({v for k, v in vars(GatewayEvent).items()
                   if k.isupper() and isinstance(v, str) and v.isupper()}
                  - SUPPRESSED_EVENTS)


def _event_key(text: str) -> str:
    """' email-sent ' -> 'EMAIL_SENT', so typed names match any case."""
    return re.sub(r'[\s-]+', '_', text.strip()).upper()


def resolve_event(text: str) -> Optional[str]:
    """The forwarded event a user typed, ignoring case; None if unknown."""
    key = _event_key(text)
    return key if key in forwarded_events() else None


def event_choices(current: str) -> List[str]:
    """Autocomplete for an event option: prefix matches, then substrings."""
    key = _event_key(current)
    names = forwarded_events()
    ranked = ([n for n in names if n.startswith(key)]
              + [n for n in names if key in n and not n.startswith(key)])
    return ranked[:AUTOCOMPLETE_LIMIT]


def register_event_handlers(
    gw: Any,
    publish: Callable[[str], Awaitable[None]],
    display: Optional[EventDisplayFilter] = None,
    settings: Optional[FeedSettings] = None,
) -> None:
    """
    Forward every named business event but SUPPRESSED_EVENTS, warmup
    EMAIL_RECEIVED and whatever /filter turned off; the SDK has no wildcard
    handler.
    """
    names = forwarded_events()
    display = display or EventDisplayFilter()
    settings = settings or FeedSettings()
    for name in names:
        async def handler(topic: str, payload: Any, _name: str = name) -> None:
            filename, data = display.event_attachment(
                _name, topic, payload)
            # Every event is kept, including the ones not posted below.
            await store_event(_name, filename, data)
            # Read per event, so /filter and /jq apply without a reload.
            if settings.is_muted(_name):
                return
            if _name == 'EMAIL_RECEIVED' and is_warmup(payload):
                return
            summary = display.event_summary(_name, topic, payload)
            expr = settings.formatters.get(_name)
            if expr:
                summary = await _formatted_summary(
                    display, _name, topic, payload, expr, summary)
            await publish((summary, filename, data))
        gw.on_event(name)(handler)


def event_store_dir() -> Path:
    """Where every event's JSON is kept: data/discord (ARTIFACT_DIR/discord)."""
    return artifact_dir() / 'discord'


async def store_event(event: str, filename: str, data: bytes) -> None:
    """
    Keep the event's JSON in event_store_dir(), and as a Prefect artifact
    when PREFECT_DUMP_JSON is set. Failures are printed, never raised: the
    feed matters more than the record.
    """
    try:
        directory = event_store_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(data)
    except Exception as exc:
        print(f'could not store {filename}: {type(exc).__name__}: {exc}')
    if not dump_json_enabled():
        return
    slug = event_slug(event)
    try:
        await acreate_markdown_artifact(
            key=f'discord-{slug}',
            markdown='```json\n' + data.decode(errors='replace') + '\n```',
            description=event)
    except Exception as exc:
        print(f'could not register artifact for {event}: '
              f'{type(exc).__name__}: {exc}')


async def _formatted_summary(display: EventDisplayFilter, event: str,
                             topic: str, payload: Any, expr: str,
                             fallback: str) -> str:
    """The /jq formatter's output under the event name; the default on error."""
    try:
        out = await run_jq(expr, display._translate(payload))
    except Exception as exc:
        note = f'\n(jq formatter failed: {str(exc)[:200]})'
        return (fallback + note)[:DISCORD_MESSAGE_LIMIT]
    head = f'**{event}** `{display._label(topic)}`\n'
    return (head + out)[:DISCORD_MESSAGE_LIMIT]


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
                 reloader: Optional['Reloader'] = None,
                 settings: Optional[FeedSettings] = None) -> Any:
    import discord
    from discord import app_commands

    client = discord.Client(intents=discord.Intents.default())
    role = cfg.admin_role

    class RoleGatedTree(app_commands.CommandTree):
        # Runs before every command, including the CLI ones added below.
        async def interaction_check(self, interaction):
            return await check_role(interaction, role, cfg.dm_user)

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

    settings = settings if settings is not None else FeedSettings()

    async def autocomplete_event(interaction: discord.Interaction,
                                 current: str):
        return [app_commands.Choice(name=n, value=n)
                for n in event_choices(current)]

    @tree.command(name='filter',
                  description='Hide an event from the feed, or show it again')
    @app_commands.describe(event='event name; omit to list the filtered ones')
    @app_commands.autocomplete(event=autocomplete_event)
    async def filter_cmd(interaction: discord.Interaction,
                         event: Optional[str] = None):
        # Deferred: jq and the Prefect save can outlast Discord's 3 s limit.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            await filter_command(settings, event, save_settings),
            ephemeral=True)

    @tree.command(name='jq', description="Format an event's feed line with jq")
    @app_commands.describe(event='event name',
                           expr='jq expression; `off` clears, omit to show')
    @app_commands.autocomplete(event=autocomplete_event)
    async def jq_cmd(interaction: discord.Interaction, event: str,
                     expr: Optional[str] = None):
        # Deferred: jq and the Prefect save can outlast Discord's 3 s limit.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(
            await jq_command(settings, event, expr, save_settings),
            ephemeral=True)

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

    @client.event
    async def on_message(message: discord.Message) -> None:
        reply = dm_reply(message, client.user, cfg.dm_user)
        if reply:
            await message.channel.send(reply)

    return client


class Reloader:
    """A reload request that any thread may raise (/auth, /reload)."""

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
        # call_soon_threadsafe: requests can come from another thread than
        # the one running the flow's event loop.
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> str:
        await self._event.wait()
        self._event.clear()
        return self._reason


RELOADER = Reloader()


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
    settings: Optional[FeedSettings] = None,
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
        register_event_handlers(gw, queue.put, display, settings)
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


async def claim_and_reap(run_id: Optional[Any]) -> None:
    """
    Tag this run with its pid/host, then stop every older run of this
    deployment, so the newest start always wins.

    The tags let a later run find and stop this process.
    """
    if run_id is None:
        return
    try:
        async with get_client() as client:
            current = await client.read_flow_run(run_id)
            tags = sorted({*(current.tags or ()), *self_tags()})
            await client.update_flow_run(run_id, tags=tags)
            report = await reap(client, BOT_DEPLOYMENT, current)
        print(f'replaced older runs: {report.summary()}')
    except Exception as exc:
        print(f'could not stop older runs: {type(exc).__name__}: {exc}')


@flow(log_prints=True)
async def discord_bot() -> None:
    """Run the bot until cancelled."""
    from prefect.runtime import flow_run as current_flow_run

    await claim_and_reap(current_flow_run.id)
    cfg = BotConfig.from_env()
    groups = parse_cli_groups(await _cli_help())
    print(f'warmbly command groups: {", ".join(groups)}')

    reloader = RELOADER
    reloader.attach(asyncio.get_running_loop())
    print('reload: /reload or a successful /auth')

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
    # One object shared by the commands and every gateway generation.
    settings = await load_settings()
    print(f'feed settings: {settings.to_dict()}')
    client = build_client(cfg, groups, reloader=reloader, settings=settings)
    async with client:
        await asyncio.gather(
            client.start(cfg.discord_token),
            run_sender(client, cfg, queue,
                       format_announcement(cfg, groups, org_name)),
            run_gateway(cfg, queue, reloader, refresh, settings=settings))


if __name__ == '__main__':
    asyncio.run(discord_bot())
