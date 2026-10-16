"""
Bulk-modify Warmbly email accounts (mailboxes).

    $ set -a && . ./.env && set +a
    $ python flow/warmbly_accounts.py --set reply_to=nate@happytailspawcare.com
    $ python flow/warmbly_accounts.py --set campaign_limit=30 min_wait_time=12 --dry-run
    $ python flow/warmbly_accounts.py --set warmup=false --only nate@happytailspawcare.com
    $ python flow/warmbly_accounts.py --restore data/warmbly-accounts-backup-<ts>.json

Every account the filters match is PATCHed at /v1/emails/{id} with only the
fields that differ from what it has now, so a re-run changes nothing. Before
any write, each account's current values for those fields are saved to
data/warmbly-accounts-backup-<timestamp>.json; --restore puts them back.

Timeouts, dropped connections and overload statuses (429, 5xx) are waited
out and retried: WARMBLY_RETRY_DELAY seconds (10) doubling up to
WARMBLY_RETRY_MAX_DELAY (120), WARMBLY_RETRIES times (6) -- about 7 minutes
in all. Retrying a PATCH is safe: it sets values, so a second copy of one
that did land changes nothing.

Values are JSON where they parse as JSON (30, true, ["a","b"]) and strings
otherwise, so reply_to=nate@x.com needs no quoting.

Connecting a new mailbox is not possible here: Warmbly's onboarding routes
take a browser session, not an API key (see the warmbly SDK's
resources/emails.py docstring).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from flow.artifacts import artifact_dir
from flow.warmbly_contacts import REQUEST_TIMEOUT, _warmbly_request_parts

EMAILS_PATH = '/v1/emails'

# Statuses another try may not repeat. A 4xx other than 408/429 is the
# request itself being wrong, and fails the same way every time.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

_sleep = time.sleep

# What PATCH /v1/emails/{id} accepts (warmbly SDK: Emails.update).
UPDATE_FIELDS = (
    'name', 'status', 'reply_to', 'timezone', 'tags', 'campaign_limit',
    'min_wait_time', 'signature_plain', 'signature_html', 'signature_sync',
    'signature_code', 'save_to_sent', 'warmup', 'warmup_base', 'warmup_max',
    'warmup_increase', 'warmup_reply_rate', 'warmup_tag', 'warmup_start_time',
    'warmup_end_time', 'warmup_days',
)


def parse_settings(pairs: Sequence[str]) -> Dict[str, Any]:
    """['reply_to=a@b.com', 'campaign_limit=30'] -> {'reply_to': 'a@b.com', 'campaign_limit': 30}."""
    settings: Dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition('=')
        key = key.strip()
        if not sep or not key:
            raise ValueError(f'{pair!r} is not field=value')
        if key not in UPDATE_FIELDS:
            raise ValueError(f'{key!r} is not an account field; one of: '
                             f"{', '.join(UPDATE_FIELDS)}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        settings[key] = value
    return settings


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '').strip() or default)
    except ValueError:
        return default


def retry_delays() -> List[float]:
    """Seconds to wait before each retry. Read per call."""
    retries = max(0, int(_env_number('WARMBLY_RETRIES', 6)))
    base = _env_number('WARMBLY_RETRY_DELAY', 10)
    cap = _env_number('WARMBLY_RETRY_MAX_DELAY', 120)
    return [min(base * 2 ** i, cap) for i in range(retries)]


def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """
    One API call, retried after retry_delays() on a transport failure or a
    TRANSIENT_STATUSES answer. Any other answer is returned as it is; the
    last failure is raised, or its response returned, once retries run out.
    """
    delays = retry_delays()
    for attempt in range(len(delays) + 1):
        try:
            response = getattr(httpx, method)(url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == len(delays):
                raise
            problem = f'{type(exc).__name__}: {exc}'
        else:
            if response.status_code not in TRANSIENT_STATUSES or attempt == len(delays):
                return response
            problem = f'HTTP {response.status_code}'
        print(f'  {method.upper()} {url}: {problem}; retrying in '
              f'{delays[attempt]:g}s ({attempt + 1}/{len(delays)})')
        _sleep(delays[attempt])
    raise AssertionError('unreachable')


def list_accounts(tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every email account, following the cursor through each page."""
    base, headers = _warmbly_request_parts()
    accounts: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        params: Dict[str, Any] = {'limit': 100}
        if cursor:
            params['cursor'] = cursor
        if tag:
            params['tag'] = tag
        response = request('get', base + EMAILS_PATH, params=params,
                           headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        page = response.json()
        accounts.extend(page.get('data') or [])
        pagination = page.get('pagination') or {}
        cursor = pagination.get('next_cursor')
        if not (pagination.get('has_more') and cursor):
            return accounts


def select(accounts: List[Dict[str, Any]], only: Sequence[str] = (),
           exclude: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The accounts named in `only` (all when empty), minus `exclude`. By address, case-blind."""
    wanted = {a.lower() for a in only}
    skipped = {a.lower() for a in exclude}
    return [a for a in accounts
            if (not wanted or a.get('email', '').lower() in wanted)
            and a.get('email', '').lower() not in skipped]


def changes_for(account: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """The settings whose value differs from the account's current one."""
    return {k: v for k, v in settings.items() if account.get(k) != v}


def plan(accounts: List[Dict[str, Any]], settings: Dict[str, Any]
         ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """(account, changes) for each account with something to change."""
    return [(a, c) for a in accounts if (c := changes_for(a, settings))]


def backup(planned: List[Tuple[Dict[str, Any], Dict[str, Any]]],
           directory: Optional[Path] = None) -> Path:
    """Save each planned account's current values for the fields about to change."""
    directory = artifact_dir() if directory is None else Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"warmbly-accounts-backup-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps([
        {'id': a['id'], 'email': a.get('email'),
         'previous': {k: a.get(k) for k in changes}}
        for a, changes in planned
    ], indent=2))
    return path


def update_account(account_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """PATCH one account; the account as Warmbly now has it."""
    base, headers = _warmbly_request_parts()
    response = request('patch', f'{base}{EMAILS_PATH}/{account_id}',
                       json=changes, headers=headers, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f'Warmbly returned {response.status_code}: '
                           f'{response.text[:400]}')
    return response.json()


def apply(planned: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Dict[str, list]:
    """
    PATCH each planned account, carrying on past failures. An account counts
    as updated only if the response shows every requested value; one that
    answers 200 without them is 'unconfirmed'.
    """
    result: Dict[str, list] = {'updated': [], 'unconfirmed': [], 'failed': []}
    for account, changes in planned:
        email = account.get('email')
        try:
            after = update_account(account['id'], changes)
        except Exception as exc:
            print(f'FAILED  {email}: {type(exc).__name__}: {exc}')
            result['failed'].append({'email': email,
                                     'error': f'{type(exc).__name__}: {exc}'})
            continue
        missing = {k: after.get(k) for k, v in changes.items() if after.get(k) != v}
        if missing:
            print(f'UNCONFIRMED  {email}: response still has {missing}')
            result['unconfirmed'].append({'email': email, 'response': missing})
        else:
            print(f'updated  {email}: {changes}')
            result['updated'].append(email)
    return result


def restore_plan(backup_file: str, accounts: List[Dict[str, Any]]
                 ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """A plan that puts a backup's previous values back, for accounts still present."""
    by_id = {a['id']: a for a in accounts}
    planned = []
    for entry in json.loads(Path(backup_file).read_text()):
        account = by_id.get(entry['id'])
        if account is None:
            print(f"skipped {entry.get('email')}: no longer an account")
            continue
        changes = changes_for(account, entry['previous'])
        if changes:
            planned.append((account, changes))
    return planned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='warmbly_accounts', description='Bulk-modify Warmbly email accounts.')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--set', nargs='+', metavar='FIELD=VALUE',
                        help=f"fields to set: {', '.join(UPDATE_FIELDS)}")
    action.add_argument('--restore', metavar='BACKUP_JSON',
                        help='put back the values a backup file saved')
    parser.add_argument('--only', nargs='+', default=[], metavar='EMAIL',
                        help='only these accounts (default: every account)')
    parser.add_argument('--exclude', nargs='+', default=[], metavar='EMAIL')
    parser.add_argument('--tag', help='only accounts carrying this tag')
    parser.add_argument('--dry-run', action='store_true',
                        help='show what would change; write nothing')
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = parse_settings(args.set) if args.set else None
    except ValueError as exc:
        parser.error(str(exc))

    try:
        accounts = select(list_accounts(tag=args.tag), args.only, args.exclude)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f'could not list accounts: {type(exc).__name__}: {exc}')
        return 2
    planned = plan(accounts, settings) if settings is not None \
        else restore_plan(args.restore, accounts)
    print(f'{len(accounts)} account(s) matched; {len(planned)} to change, '
          f'{len(accounts) - len(planned)} already as asked')
    if args.dry_run or not planned:
        for account, changes in planned:
            print(f"would update  {account.get('email')}: {changes}")
        return 0

    print(f'backup of previous values: {backup(planned)}')
    result = apply(planned)
    print(f"updated={len(result['updated'])} unconfirmed={len(result['unconfirmed'])} "
          f"failed={len(result['failed'])}")
    return 0 if not (result['failed'] or result['unconfirmed']) else 1


if __name__ == '__main__':
    sys.exit(main())
