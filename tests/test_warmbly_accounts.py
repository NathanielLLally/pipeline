import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow import warmbly_accounts as wa

A = {'id': 'a1', 'email': 'nate@happytailspawcare.ca', 'reply_to': ''}
B = {'id': 'b2', 'email': 'nate@happytailspawcare.net',
     'reply_to': 'nate@happytailspawcare.com'}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv('WARMBLY_API_URL', 'https://warmbly.example')
    monkeypatch.setenv('WARMBLY_API_TOKEN', 'tok')
    monkeypatch.setenv('ARTIFACT_DIR', str(tmp_path))
    monkeypatch.setattr(wa, '_sleep', lambda s: None)


def _response(body, status=200):
    r = MagicMock(status_code=status, text=json.dumps(body))
    r.json.return_value = body
    r.raise_for_status.return_value = None
    return r


class TestParseSettings:
    def test_plain_text_stays_a_string_and_json_is_decoded(self):
        assert wa.parse_settings(['reply_to=nate@x.com', 'campaign_limit=30',
                                  'warmup=false', 'tags=["a","b"]']) == {
            'reply_to': 'nate@x.com', 'campaign_limit': 30, 'warmup': False,
            'tags': ['a', 'b']}

    def test_unknown_field_is_refused(self):
        with pytest.raises(ValueError, match='not an account field'):
            wa.parse_settings(['reply_too=x'])

    def test_missing_equals_is_refused(self):
        with pytest.raises(ValueError):
            wa.parse_settings(['reply_to'])


class TestListAccounts:
    def test_follows_the_cursor_through_every_page(self):
        pages = [_response({'data': [A], 'pagination': {'has_more': True, 'next_cursor': 'c2'}}),
                 _response({'data': [B], 'pagination': {'has_more': False, 'next_cursor': None}})]
        with patch.object(wa.httpx, 'get', side_effect=pages) as get:
            assert wa.list_accounts() == [A, B]
        assert get.call_args_list[1].kwargs['params']['cursor'] == 'c2'
        assert get.call_args.args[0] == 'https://warmbly.example/v1/emails'


class TestPlan:
    def test_only_accounts_that_differ_are_planned(self):
        planned = wa.plan([A, B], {'reply_to': 'nate@happytailspawcare.com'})
        assert planned == [(A, {'reply_to': 'nate@happytailspawcare.com'})]

    def test_only_and_exclude_match_addresses_case_blind(self):
        assert wa.select([A, B], only=['NATE@happytailspawcare.ca']) == [A]
        assert wa.select([A, B], exclude=['nate@happytailspawcare.ca']) == [B]


class TestApply:
    def test_patches_only_the_changed_fields(self):
        with patch.object(wa.httpx, 'patch',
                          return_value=_response({**A, 'reply_to': 'n@x.com'})) as p:
            result = wa.apply([(A, {'reply_to': 'n@x.com'})])
        assert p.call_args.args[0] == 'https://warmbly.example/v1/emails/a1'
        assert p.call_args.kwargs['json'] == {'reply_to': 'n@x.com'}
        assert result['updated'] == [A['email']]

    def test_a_200_without_the_value_is_unconfirmed(self):
        with patch.object(wa.httpx, 'patch', return_value=_response(A)):
            result = wa.apply([(A, {'reply_to': 'n@x.com'})])
        assert result['updated'] == []
        assert result['unconfirmed'][0]['email'] == A['email']

    def test_one_failure_does_not_stop_the_rest(self):
        responses = [_response({'error': 'nope'}, status=400),
                     _response({**B, 'reply_to': 'n@x.com'})]
        with patch.object(wa.httpx, 'patch', side_effect=responses):
            result = wa.apply([(A, {'reply_to': 'n@x.com'}),
                               (B, {'reply_to': 'n@x.com'})])
        assert [f['email'] for f in result['failed']] == [A['email']]
        assert result['updated'] == [B['email']]


class TestBackupAndRestore:
    def test_backup_saves_previous_values_and_restore_puts_them_back(self, tmp_path):
        path = wa.backup([(A, {'reply_to': 'n@x.com'})])
        assert json.loads(path.read_text()) == [
            {'id': 'a1', 'email': A['email'], 'previous': {'reply_to': ''}}]

        changed = {**A, 'reply_to': 'n@x.com'}
        assert wa.restore_plan(str(path), [changed]) == [(changed, {'reply_to': ''})]

    def test_restore_skips_accounts_that_are_gone(self, tmp_path):
        path = wa.backup([(A, {'reply_to': 'n@x.com'})])
        assert wa.restore_plan(str(path), [B]) == []


class TestMain:
    def test_dry_run_writes_nothing(self, tmp_path):
        with patch.object(wa, 'list_accounts', return_value=[A, B]), \
             patch.object(wa.httpx, 'patch') as p:
            assert wa.main(['--set', 'reply_to=n@x.com', '--dry-run']) == 0
        p.assert_not_called()
        assert not list(tmp_path.glob('warmbly-accounts-backup-*'))

    def test_a_failure_makes_the_exit_code_nonzero(self):
        with patch.object(wa, 'list_accounts', return_value=[A]), \
             patch.object(wa.httpx, 'patch',
                          return_value=_response({'e': 1}, status=400)):
            assert wa.main(['--set', 'reply_to=n@x.com']) == 1

    def test_set_or_restore_is_required(self):
        with pytest.raises(SystemExit):
            wa.main([])


class TestRetry:
    """api.crm.happytailspawcare.com stopped answering part way through a
    live run; five accounts failed on a 60s timeout with no second try."""

    @pytest.fixture(autouse=True)
    def _no_wait(self, monkeypatch):
        self.waits = []
        monkeypatch.setattr(wa, '_sleep', self.waits.append)
        monkeypatch.setenv('WARMBLY_RETRIES', '3')
        monkeypatch.setenv('WARMBLY_RETRY_DELAY', '10')
        monkeypatch.setenv('WARMBLY_RETRY_MAX_DELAY', '15')

    def test_a_timeout_is_waited_out_and_retried(self):
        ok = _response({**A, 'reply_to': 'n@x.com'})
        with patch.object(wa.httpx, 'patch',
                          side_effect=[wa.httpx.ConnectTimeout('timed out'), ok]) as p:
            result = wa.apply([(A, {'reply_to': 'n@x.com'})])
        assert p.call_count == 2
        assert result['updated'] == [A['email']]
        assert self.waits == [10]

    def test_waits_back_off_up_to_the_cap(self):
        with patch.object(wa.httpx, 'patch',
                          side_effect=wa.httpx.ReadTimeout('timed out')) as p:
            result = wa.apply([(A, {'reply_to': 'n@x.com'})])
        assert p.call_count == 4  # the first try and 3 retries
        assert self.waits == [10, 15, 15]
        assert 'timed out' in result['failed'][0]['error']

    def test_overload_statuses_are_retried(self):
        responses = [_response({'e': 1}, status=503), _response({'e': 1}, status=429),
                     _response({**A, 'reply_to': 'n@x.com'})]
        with patch.object(wa.httpx, 'patch', side_effect=responses):
            assert wa.apply([(A, {'reply_to': 'n@x.com'})])['updated'] == [A['email']]

    def test_a_client_error_is_not_retried(self):
        """A 400 says the request is wrong; sending it again cannot help."""
        with patch.object(wa.httpx, 'patch',
                          return_value=_response({'e': 1}, status=400)) as p:
            result = wa.apply([(A, {'reply_to': 'n@x.com'})])
        assert p.call_count == 1
        assert result['failed']

    def test_listing_is_retried_too(self):
        page = _response({'data': [A], 'pagination': {'has_more': False}})
        with patch.object(wa.httpx, 'get',
                          side_effect=[wa.httpx.ConnectTimeout('timed out'), page]):
            assert wa.list_accounts() == [A]

    def test_an_unreachable_api_is_reported_not_a_traceback(self, capsys):
        with patch.object(wa.httpx, 'get',
                          side_effect=wa.httpx.ConnectTimeout('timed out')):
            assert wa.main(['--set', 'reply_to=n@x.com']) == 2
        assert 'could not list' in capsys.readouterr().out
