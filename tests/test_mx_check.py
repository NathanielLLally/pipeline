"""
Unit tests for flow/mx_check.py: the Prefect wrapper around scripts/mxCheck.pl.

Neither Perl nor Postgres is touched: the subprocess and the connection are
fakes. What is tested is the contract around them -- addresses go in on stdin
(no temp file), results are matched back to every business_email row that
holds the address, and the write is an idempotent upsert on business_email_id.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from flow import mx_check


def _run(coro):
    return asyncio.run(coro)


def fake_proc(results, code=0, stderr=b''):
    proc = MagicMock()
    proc.communicate = AsyncMock(
        return_value=(json.dumps(results).encode(), stderr))
    proc.returncode = code
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestRunMxCheck:
    def test_addresses_go_in_on_stdin_not_a_temp_file(self):
        proc = fake_proc([])
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as ex, \
             patch('tempfile.NamedTemporaryFile') as tmp:
            _run(mx_check.run_mxcheck(['a@x.com', 'b@y.com']))

        tmp.assert_not_called()
        argv = ex.await_args.args
        assert argv[argv.index('--file') + 1] == '/dev/stdin'
        sent = proc.communicate.await_args.kwargs['input']
        assert sent == b'a@x.com\nb@y.com\n'

    def test_options_are_passed_through(self):
        proc = fake_proc([])
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as ex:
            _run(mx_check.run_mxcheck(['a@x.com'], threads=7, rate_limit=2.5,
                                      force_check=True))
        argv = ex.await_args.args
        assert argv[argv.index('--threads') + 1] == '7'
        assert argv[argv.index('--rate-limit') + 1] == '2.5'
        assert '--force-check' in argv

    def test_force_check_is_off_unless_asked(self):
        proc = fake_proc([])
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as ex:
            _run(mx_check.run_mxcheck(['a@x.com']))
        assert '--force-check' not in ex.await_args.args

    def test_script_path_is_absolute_so_cwd_does_not_matter(self):
        proc = fake_proc([])
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)) as ex:
            _run(mx_check.run_mxcheck(['a@x.com']))
        from pathlib import Path
        script = Path(ex.await_args.args[1])
        assert script.is_absolute() and script.name == 'mxCheck.pl'

    def test_addresses_are_normalised_and_deduplicated(self):
        proc = fake_proc([])
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)):
            _run(mx_check.run_mxcheck([' A@X.com', 'a@x.com', '', 'b@y.com']))
        sent = proc.communicate.await_args.kwargs['input']
        assert sent == b'a@x.com\nb@y.com\n'

    def test_returns_parsed_results(self):
        results = [{'email': 'a@x.com', 'verified': 1, 'error': None,
                    'mx_server': 'mx.x.com'}]
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=fake_proc(results))):
            assert _run(mx_check.run_mxcheck(['a@x.com'])) == results

    def test_empty_input_runs_nothing(self):
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock()) as ex:
            assert _run(mx_check.run_mxcheck([])) == []
        ex.assert_not_awaited()

    def test_nonzero_exit_raises_with_stderr(self):
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=fake_proc(
                       [], code=2, stderr=b'Could not open file'))):
            with pytest.raises(mx_check.MxCheckError) as exc:
                _run(mx_check.run_mxcheck(['a@x.com']))
        assert 'Could not open file' in str(exc.value)

    def test_unparseable_output_raises(self):
        proc = fake_proc([])
        proc.communicate = AsyncMock(return_value=(b'not json', b''))
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)):
            with pytest.raises(mx_check.MxCheckError):
                _run(mx_check.run_mxcheck(['a@x.com']))

    def test_timeout_kills_the_script(self):
        proc = fake_proc([])
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch('flow.mx_check.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=proc)):
            with pytest.raises(mx_check.MxCheckError) as exc:
                _run(mx_check.run_mxcheck(['a@x.com'], timeout=1))
        proc.kill.assert_called_once()
        assert 'timed out' in str(exc.value)


def row(email, business_id=None, row_id=None):
    return {'id': row_id or uuid4(), 'business_id': business_id or uuid4(),
            'email': email}


class TestMatchResults:
    def test_one_result_fans_out_to_every_row_with_that_address(self):
        """The same address held by two businesses: one check, two rows."""
        r1, r2 = row('a@x.com'), row('A@x.com')
        results = [{'email': 'a@x.com', 'verified': 1, 'error': None,
                    'mx_server': 'mx.x.com'}]
        records = mx_check.match_results([r1, r2], results)
        assert {r['business_email_id'] for r in records} == {r1['id'], r2['id']}
        assert all(r['verified'] is True for r in records)

    def test_verified_is_a_real_boolean(self):
        records = mx_check.match_results(
            [row('a@x.com')],
            [{'email': 'a@x.com', 'verified': 0, 'error': 'catch-all',
              'mx_server': None}])
        assert records[0]['verified'] is False
        assert records[0]['error'] == 'catch-all'

    def test_rows_without_a_result_are_not_written(self):
        """No result is not a failed check; writing verified=false would lie."""
        records = mx_check.match_results(
            [row('a@x.com'), row('missing@y.com')],
            [{'email': 'a@x.com', 'verified': 1, 'error': None,
              'mx_server': 'mx'}])
        assert [r['email'] for r in records] == ['a@x.com']

    def test_text_is_stripped_of_nul_for_postgres(self):
        records = mx_check.match_results(
            [row('a@x.com')],
            [{'email': 'a@x.com', 'verified': 0,
              'error': 'bad\x00banner', 'mx_server': 'mx\x00.x'}])
        assert '\x00' not in records[0]['error']
        assert '\x00' not in records[0]['mx_server']


class TestUpsert:
    def test_upserts_on_business_email_id(self):
        conn = MagicMock()
        conn.executemany = AsyncMock()
        record = {'business_email_id': uuid4(), 'business_id': uuid4(),
                  'email': 'a@x.com', 'verified': True,
                  'mx_server': 'mx', 'error': None}
        n = _run(mx_check.upsert_verifications(conn, [record]))

        sql, args = conn.executemany.await_args.args
        assert 'insert into leads.email_verification' in sql.lower()
        assert 'on conflict (business_email_id)' in sql.lower()
        # A re-check must refresh the timestamp, not keep the first one.
        assert 'verified_at = now()' in sql.lower()
        assert args[0][0] == record['business_email_id']
        assert n == 1

    def test_nothing_to_write_skips_the_database(self):
        conn = MagicMock()
        conn.executemany = AsyncMock()
        assert _run(mx_check.upsert_verifications(conn, [])) == 0
        conn.executemany.assert_not_awaited()


class TestLoadBusinessEmails:
    def _conn(self, rows):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        return conn

    def test_by_address_is_case_insensitive(self):
        conn = self._conn([])
        _run(mx_check.load_business_emails(conn, emails=['A@X.com']))
        sql, *args = conn.fetch.await_args.args
        assert 'lower(email)' in sql.lower()
        assert args[0] == ['a@x.com']

    def test_by_business(self):
        conn = self._conn([])
        bid = str(uuid4())
        _run(mx_check.load_business_emails(conn, business_ids=[bid]))
        sql, *args = conn.fetch.await_args.args
        assert 'business_id = any' in sql.lower()
        assert args[0] == [bid]

    def test_unverified_batch_is_limited(self):
        conn = self._conn([])
        _run(mx_check.load_business_emails(conn, unverified_limit=25))
        sql, *args = conn.fetch.await_args.args
        assert 'not exists' in sql.lower()
        assert 'email_verification' in sql.lower()
        assert args[-1] == 25

    def test_requires_exactly_one_selector(self):
        with pytest.raises(ValueError):
            _run(mx_check.load_business_emails(self._conn([])))
        with pytest.raises(ValueError):
            _run(mx_check.load_business_emails(
                self._conn([]), emails=['a@x.com'], unverified_limit=5))


class TestVerifyTask:
    def test_is_an_importable_prefect_task(self):
        from prefect import Task
        assert isinstance(mx_check.verify_business_emails, Task)

    def test_checks_each_address_once_and_writes_every_row(self):
        b1, b2 = uuid4(), uuid4()
        rows = [row('a@x.com', b1), row('a@x.com', b2), row('c@z.com', b1)]
        results = [
            {'email': 'a@x.com', 'verified': 1, 'error': None, 'mx_server': 'm'},
            {'email': 'c@z.com', 'verified': 0, 'error': 'no mx', 'mx_server': None},
        ]
        conn = MagicMock()
        conn.close = AsyncMock()
        with patch('flow.mx_check._connect', new=AsyncMock(return_value=conn)), \
             patch('flow.mx_check.load_business_emails',
                   new=AsyncMock(return_value=rows)), \
             patch('flow.mx_check.run_mxcheck',
                   new=AsyncMock(return_value=results)) as run, \
             patch('flow.mx_check.upsert_verifications',
                   new=AsyncMock(return_value=3)) as up:
            summary = _run(mx_check.verify_business_emails.fn(
                business_ids=[str(b1)]))

        assert sorted(run.await_args.args[0]) == ['a@x.com', 'c@z.com']
        written = up.await_args.args[1]
        assert len(written) == 3
        assert summary == {'addresses': 2, 'rows': 3, 'written': 3,
                           'verified': 1, 'failed': 1, 'unchecked': 0}
        conn.close.assert_awaited_once()

    def test_connection_is_closed_when_the_check_fails(self):
        conn = MagicMock()
        conn.close = AsyncMock()
        with patch('flow.mx_check._connect', new=AsyncMock(return_value=conn)), \
             patch('flow.mx_check.load_business_emails',
                   new=AsyncMock(return_value=[row('a@x.com')])), \
             patch('flow.mx_check.run_mxcheck',
                   new=AsyncMock(side_effect=mx_check.MxCheckError('boom'))):
            with pytest.raises(mx_check.MxCheckError):
                _run(mx_check.verify_business_emails.fn(emails=['a@x.com']))
        conn.close.assert_awaited_once()

    def test_nothing_selected_does_not_run_perl(self):
        conn = MagicMock()
        conn.close = AsyncMock()
        with patch('flow.mx_check._connect', new=AsyncMock(return_value=conn)), \
             patch('flow.mx_check.load_business_emails',
                   new=AsyncMock(return_value=[])), \
             patch('flow.mx_check.run_mxcheck', new=AsyncMock()) as run:
            summary = _run(mx_check.verify_business_emails.fn(
                unverified_limit=10))
        run.assert_not_awaited()
        assert summary['rows'] == 0


class TestDsn:
    def test_reads_leads_db_url(self, monkeypatch):
        monkeypatch.setenv('LEADS_DB_URL', 'postgres://u@h/db')
        assert mx_check._dsn() == 'postgresql://u@h/db'

    def test_missing_url_is_named(self, monkeypatch):
        monkeypatch.delenv('LEADS_DB_URL', raising=False)
        with pytest.raises(RuntimeError) as exc:
            mx_check._dsn()
        assert 'LEADS_DB_URL' in str(exc.value)
