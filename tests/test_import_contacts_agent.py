"""Unit tests for the import-contacts-agent deployment."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.agents.import_contacts import import_contacts

BUSINESS = {
    'id': 'biz-1',
    'business_name': 'Happy Tails Dog Training',
    'website': 'https://happytails.example',
    'city': 'Austin',
    'state': 'TX',
}
RESEARCH = {
    'business_name': 'Happy Tails Dog Training',
    'pain_signals': ['no online booking'],
    'personalization_hook': 'board-and-train specialist',
    'inferred_tone': 'warm',
    'confidence': 0.92,
    'evidence': ['homepage emphasizes reactivity'],
    'suggested_email': 'owner@happytails.example',
}
DRAFT = {
    'selected_emails': ['owner@happytails.example'],
    'subject': 'Dog training leads',
    'body': 'Hi there, we have dog owner leads for you.',
    'rationale': 'warm tone, boarding friction',
}


@pytest.fixture(autouse=True)
def _no_live_drafts():
    """Every successful import now also PUTs compose drafts to Warmbly.

    Patched for every test so none reaches the live API; the draft tests
    patch it again inside the test, which takes precedence.
    """
    with patch('flow.agents.import_contacts.create_compose_drafts',
               side_effect=lambda drafts: [d['id'] for d in drafts]):
        yield


class TestImportCreatesComposeDrafts:
    """After the contact is imported, its email is created as a Warmbly draft."""

    def _created(self, payloads, **kwargs):
        return [dict(p, id=f'warmbly-{i}') for i, p in enumerate(payloads)]

    def test_creates_a_draft_for_each_selected_email_after_import(self):
        order = []
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=lambda p, **k: order.append('contacts') or
                   self._created(p)), \
             patch('flow.agents.import_contacts.create_compose_drafts',
                   side_effect=lambda d: order.append('drafts') or
                   [x['id'] for x in d]) as mk, \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(business=BUSINESS, research=RESEARCH,
                                     draft=DRAFT)

        assert order == ['contacts', 'drafts']
        sent = mk.call_args.args[0]
        assert [d['body']['to'] for d in sent] == [DRAFT['selected_emails']]
        assert sent[0]['body']['subject'] == DRAFT['subject']
        assert result['status'] == 'imported'
        assert result['drafts_status'] == 'created'
        assert result['drafts'] == [sent[0]['id']]

    def test_draft_failure_keeps_the_import_and_reports_it(self):
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=lambda p, **k: self._created(p)), \
             patch('flow.agents.import_contacts.create_compose_drafts',
                   side_effect=RuntimeError('Warmbly returned 403')), \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(business=BUSINESS, research=RESEARCH,
                                     draft=DRAFT)

        assert result['status'] == 'imported'
        assert result['drafts_status'] == 'error'
        assert '403' in result['drafts_error']

    def test_no_draft_when_the_contact_import_fails(self):
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=RuntimeError('Warmbly returned 500')), \
             patch('flow.agents.import_contacts.create_compose_drafts') as mk, \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(business=BUSINESS, research=RESEARCH,
                                     draft=DRAFT)

        assert result['status'] == 'rejected'
        mk.assert_not_called()


class TestImportContactsHappyPath:
    """Test successful contact import."""

    def _mock_create_contacts(self, payloads, **kwargs):
        """Mock return value for create_contacts."""
        return [dict(p, id=f'warmbly-{i}') for i, p in enumerate(payloads)]

    def test_imports_contacts_successfully(self):
        """Successfully builds payloads and imports them"""
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=self._mock_create_contacts), \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(BUSINESS, RESEARCH, DRAFT)

        assert result['status'] == 'imported'
        assert result['business_id'] == 'biz-1'
        assert result['payloads_sent'] == 1
        assert len(result['created']) == 1

    def test_derives_idempotency_key_from_business_and_payload(self):
        """The key names the business and hashes the payload.

        It used to be the business id alone, which made Warmbly reject every
        re-import after a draft changed with 409 "Idempotency-Key was already
        used with a different request". The business id stays in the key for
        legibility; the hash is what makes a changed draft a new request.
        """
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=self._mock_create_contacts) as mock_create, \
             patch('flow.agents.import_contacts.write_artifact'):
            import_contacts.fn(BUSINESS, RESEARCH, DRAFT)

        key = mock_create.call_args.kwargs['idempotency_key']
        assert key.startswith('leads-business-biz-1-')
        assert len(key) > len('leads-business-biz-1-')

    def test_a_changed_draft_changes_the_derived_key(self):
        """Otherwise the second import of a revised draft 409s forever."""
        keys = []
        for subject in ('First subject', 'Revised subject'):
            with patch('flow.agents.import_contacts.create_contacts',
                       side_effect=self._mock_create_contacts) as mock_create, \
                 patch('flow.agents.import_contacts.write_artifact'):
                import_contacts.fn(BUSINESS, RESEARCH,
                                   dict(DRAFT, subject=subject))
            keys.append(mock_create.call_args.kwargs['idempotency_key'])

        assert keys[0] != keys[1]

    def test_uses_provided_idempotency_key(self):
        """Uses provided idempotency_key over derived one"""
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=self._mock_create_contacts) as mock_create, \
             patch('flow.agents.import_contacts.write_artifact'):
            import_contacts.fn(
                BUSINESS, RESEARCH, DRAFT,
                idempotency_key='custom-key-123'
            )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs['idempotency_key'] == 'custom-key-123'

    def test_writes_input_and_output_artifacts(self):
        """Writes both input and output artifacts"""
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=self._mock_create_contacts), \
             patch('flow.agents.import_contacts.write_artifact') as mock_write:
            import_contacts.fn(BUSINESS, RESEARCH, DRAFT)

            assert mock_write.call_count == 2
            input_call = mock_write.call_args_list[0]
            output_call = mock_write.call_args_list[1]

            assert input_call.kwargs['suffix'] == 'input'
            assert output_call.kwargs['suffix'] == 'output'


class TestImportsOnlyTheDraftedContact:
    """Only an address the draft is written to becomes a Warmbly contact.
    Other pool addresses at the business used to be imported too, without the
    draft; they are left out now."""

    POOL = [{'email': 'owner@happytails.example', 'verified': True},
            {'email': 'info@happytails.example', 'verified': False}]

    def _import(self, draft=DRAFT, pool=POOL):
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=lambda p, **k: p) as create, \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(BUSINESS, RESEARCH, draft,
                                        candidate_emails=pool)
        return result, [p['email'] for p in create.call_args.args[0]]

    def test_unselected_pool_addresses_are_not_imported(self):
        result, sent = self._import()
        assert sent == ['owner@happytails.example']
        assert result['payloads_sent'] == 1

    def test_the_imported_contact_carries_the_draft(self):
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=lambda p, **k: p) as create, \
             patch('flow.agents.import_contacts.write_artifact'):
            import_contacts.fn(BUSINESS, RESEARCH, DRAFT, candidate_emails=self.POOL)
        fields = create.call_args.args[0][0]['custom_fields']
        assert fields['draft_body'] == DRAFT['body']

    def test_a_selected_address_outside_the_pool_is_still_imported(self):
        """Drafting may pick an address the pool lacks; it has the draft."""
        draft = dict(DRAFT, selected_emails=['laura@happytails.example'])
        _, sent = self._import(draft=draft)
        assert sent == ['laura@happytails.example']

    def test_matching_ignores_case(self):
        draft = dict(DRAFT, selected_emails=['Owner@HappyTails.example'])
        _, sent = self._import(draft=draft)
        assert [e.lower() for e in sent] == ['owner@happytails.example']


class TestImportContactsFailures:
    """Test failure modes."""

    def test_no_emails_to_import(self):
        """Returns rejected when draft has no selected emails"""
        draft = dict(DRAFT, selected_emails=[])
        with patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(BUSINESS, RESEARCH, draft)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'no_emails'
        assert result['payloads_sent'] == 0

    def test_payload_build_failure(self):
        """Handles failures in build_contact_payloads"""
        with patch('flow.agents.import_contacts.build_contact_payloads',
                   side_effect=ValueError('Missing field')), \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(BUSINESS, RESEARCH, DRAFT)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'payload_build_failed'
        assert 'Missing field' in result['error']

    def test_warmbly_api_error(self):
        """Handles Warmbly API failures"""
        with patch('flow.agents.import_contacts.create_contacts',
                   side_effect=RuntimeError('Warmbly returned 401')), \
             patch('flow.agents.import_contacts.write_artifact'):
            result = import_contacts.fn(BUSINESS, RESEARCH, DRAFT)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'warmbly_error'
        assert '401' in result['error']


class TestImportContactsJSONInput:
    """Test loading from JSON input files."""

    def _mock_create_contacts(self, payloads, **kwargs):
        """Mock return value for create_contacts."""
        return [dict(p, id=f'warmbly-{i}') for i, p in enumerate(payloads)]

    def test_loads_from_json_input_file(self):
        """Loads all params from JSON artifact"""
        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / 'biz-1-import-input.json'
            input_data = {
                'business': BUSINESS,
                'research': RESEARCH,
                'draft': DRAFT,
                'idempotency_key': 'custom-key',
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.import_contacts.create_contacts',
                       side_effect=self._mock_create_contacts), \
                 patch('flow.agents.import_contacts.write_artifact'):
                result = import_contacts.fn(json_input_file=str(input_file))

            assert result['status'] == 'imported'
            assert result['idempotency_key'] == 'custom-key'

    def test_raises_when_json_input_file_missing(self):
        """Raises FileNotFoundError if json_input_file does not exist"""
        with pytest.raises(FileNotFoundError):
            import_contacts.fn(json_input_file="/nonexistent/file.json")

    def test_requires_either_dict_params_or_json_file(self):
        """Raises ValueError if neither dict params nor json_input_file provided"""
        with pytest.raises(ValueError, match='Either json_input_file or'):
            import_contacts.fn()

    def test_json_input_takes_precedence(self):
        """json_input_file overrides dict params when both provided"""
        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / 'test-input.json'
            json_business = dict(BUSINESS, id='json-biz-999')
            input_data = {
                'business': json_business,
                'research': RESEARCH,
                'draft': DRAFT,
                'idempotency_key': None,
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.import_contacts.create_contacts',
                       side_effect=self._mock_create_contacts), \
                 patch('flow.agents.import_contacts.write_artifact') as mock_write:
                import_contacts.fn(
                    business=BUSINESS,
                    research=RESEARCH,
                    draft=DRAFT,
                    json_input_file=str(input_file)
                )

            # Check that the input artifact written was from JSON file
            input_call = mock_write.call_args_list[0]
            data = input_call.args[0]
            assert data['business']['id'] == 'json-biz-999'


class TestImportContactsMultipleEmails:
    """Test importing to multiple email addresses."""

    def test_one_payload_per_email(self):
        """Builds one payload per selected email"""
        draft = dict(
            DRAFT,
            selected_emails=['owner@happytails.example', 'trainer@happytails.example']
        )

        with patch('flow.agents.import_contacts.create_contacts') as mock_create:
            mock_create.return_value = [{'id': '1'}, {'id': '2'}]
            with patch('flow.agents.import_contacts.write_artifact'):
                result = import_contacts.fn(BUSINESS, RESEARCH, draft)

        assert result['status'] == 'imported'
        assert result['payloads_sent'] == 2
        call_args = mock_create.call_args.args[0]
        assert len(call_args) == 2
        assert {p['email'] for p in call_args} == {
            'owner@happytails.example',
            'trainer@happytails.example'
        }
