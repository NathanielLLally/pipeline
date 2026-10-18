"""Unit tests for the import-campaign deployment and its Warmbly helpers.

The variant of import-contacts that, instead of a Unibox draft, files a
campaign named after the business -- in the 'initial' folder, sending from
the "october '26 htpc primary" mailboxes -- whose first step is the drafted
email, with the drafted contact as its one lead.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.agents.import_campaign import import_campaign
from flow.warmbly_contacts import (
    CAMPAIGN_EMAIL_TAG_ID,
    CAMPAIGN_FOLDER_ID,
    CAMPAIGNS_PATH,
    build_campaign,
    create_campaign,
)

BUSINESS = {
    'id': 'biz-1',
    'business_name': 'Happy Tails Dog Training',
    'website': 'https://happytails.example',
}
RESEARCH = {'personalization_hook': 'board-and-train specialist'}
DRAFT = {
    'selected_emails': ['owner@happytails.example'],
    'subject': 'Dog training leads',
    'body': 'Hi there, we have dog owner leads for you.',
    'rationale': 'warm tone',
}
CANDIDATES = [{'email': 'info@happytails.example'}]


def _response(payload, status=200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
    monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')


class TestBuildCampaign:
    def test_named_after_the_business(self):
        assert build_campaign(BUSINESS, DRAFT)['name'] == BUSINESS['business_name']

    def test_filed_in_initial_and_sent_from_the_htpc_primary_tag(self):
        body = build_campaign(BUSINESS, DRAFT)
        assert CAMPAIGN_FOLDER_ID == 'aaf0ecbf-1a52-4052-873e-ddeb62a6ad80'
        assert CAMPAIGN_EMAIL_TAG_ID == '10e2c278-9eed-4081-8aea-8a04263d3c73'
        # Create's names; update calls them folders / email_tags.
        assert body['folder_ids'] == [CAMPAIGN_FOLDER_ID]
        assert body['email_tag_ids'] == [CAMPAIGN_EMAIL_TAG_ID]
        assert body['sender_strategy'] == 'tags'

    def test_first_step_is_the_drafted_email(self):
        steps = build_campaign(BUSINESS, DRAFT)['steps']
        assert len(steps) == 1
        assert steps[0]['subject'] == DRAFT['subject']
        assert steps[0]['body_plain'] == DRAFT['body']

    def test_adds_no_signature_template(self):
        # The send path appends the mailbox signature itself.
        assert 'Signature' not in build_campaign(BUSINESS, DRAFT)['steps'][0]['body_plain']

    def test_sends_plain_text_only(self):
        # text_only ships no HTML part (and so no open/click tracking).
        assert build_campaign(BUSINESS, DRAFT)['text_only'] is True

    def test_is_created_as_a_draft_never_started(self):
        assert 'status' not in build_campaign(BUSINESS, DRAFT)


class TestCreateCampaign:
    def test_posts_when_no_campaign_has_the_name(self, env):
        with patch('flow.warmbly_contacts.httpx.get',
                   return_value=_response({'data': [
                       {'id': 'other', 'name': 'Happy Tails Dog Training II'}]})), \
             patch('flow.warmbly_contacts.httpx.post',
                   return_value=_response({'id': 'c-1', 'name': 'x'})) as post:
            campaign, created = create_campaign(build_campaign(BUSINESS, DRAFT))
        assert created is True
        assert campaign['id'] == 'c-1'
        assert post.call_args.args[0] == 'https://api.example.test' + CAMPAIGNS_PATH

    def test_reuses_an_exact_name_match_instead_of_duplicating(self, env):
        existing = {'id': 'c-old', 'name': BUSINESS['business_name']}
        with patch('flow.warmbly_contacts.httpx.get',
                   return_value=_response({'data': [existing]})), \
             patch('flow.warmbly_contacts.httpx.post') as post:
            campaign, created = create_campaign(build_campaign(BUSINESS, DRAFT))
        assert (campaign, created) == (existing, False)
        post.assert_not_called()


@pytest.fixture
def warmbly():
    """Patch every Warmbly call the flow makes; record contact payloads."""
    sent = {}

    def contacts(payloads, **kwargs):
        sent['payloads'] = payloads
        return [dict(p, id=f'w-{i}') for i, p in enumerate(payloads)]

    with patch('flow.agents.import_campaign.create_campaign',
               return_value=({'id': 'camp-1', 'name': BUSINESS['business_name']},
                             True)) as camp, \
         patch('flow.agents.import_campaign.create_contacts',
               side_effect=contacts), \
         patch('flow.agents.import_campaign.write_artifact'):
        yield camp, sent


class TestImportCampaign:
    def test_campaign_comes_from_the_business_and_draft(self, warmbly):
        camp, _ = warmbly
        import_campaign.fn(business=BUSINESS, research=RESEARCH, draft=DRAFT)
        assert camp.call_args.args[0] == build_campaign(BUSINESS, DRAFT)

    def test_only_the_drafted_contact_is_enrolled(self, warmbly):
        _, sent = warmbly
        result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                    draft=DRAFT, candidate_emails=CANDIDATES)
        by_email = {p['email']: p for p in sent['payloads']}
        assert by_email['owner@happytails.example']['campaigns'] == ['camp-1']
        # Not drafted, so not imported at all -- as in import_contacts.
        assert 'info@happytails.example' not in by_email
        assert result['status'] == 'imported'
        assert result['campaign_status'] == 'created'
        assert result['campaign_id'] == 'camp-1'
        assert result['enrolled'] == ['owner@happytails.example']

    def test_only_the_first_of_several_selected_emails_is_enrolled(self, warmbly):
        _, sent = warmbly
        draft = dict(DRAFT, selected_emails=['owner@happytails.example',
                                             'office@happytails.example'])
        result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                    draft=draft)
        enrolled = [p['email'] for p in sent['payloads'] if p.get('campaigns')]
        assert enrolled == ['owner@happytails.example']
        assert result['not_enrolled'] == ['office@happytails.example']

    def test_only_addresses_with_a_draft_are_imported(self, warmbly):
        _, sent = warmbly
        pool = [{'email': 'owner@happytails.example', 'verified': True},
                {'email': 'info@happytails.example'},
                {'email': 'jobs@happytails.example'}]
        import_campaign.fn(business=BUSINESS, research=RESEARCH, draft=DRAFT,
                           candidate_emails=pool)
        assert [p['email'] for p in sent['payloads']] == ['owner@happytails.example']

    def test_creates_no_unibox_draft(self, warmbly):
        with patch('flow.warmbly_contacts.create_compose_drafts') as drafts:
            import_campaign.fn(business=BUSINESS, research=RESEARCH, draft=DRAFT)
        drafts.assert_not_called()

    def test_reports_an_existing_campaign(self, warmbly):
        camp, _ = warmbly
        camp.return_value = ({'id': 'camp-old', 'name': 'x'}, False)
        result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                    draft=DRAFT)
        assert result['campaign_status'] == 'existing'
        assert result['campaign_id'] == 'camp-old'

    def test_campaign_failure_still_imports_the_contact_unenrolled(self, warmbly):
        camp, sent = warmbly
        camp.side_effect = RuntimeError('Warmbly returned 429')
        result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                    draft=DRAFT)
        assert result['status'] == 'imported'
        assert result['campaign_status'] == 'error'
        assert '429' in result['campaign_error']
        assert all('campaigns' not in p for p in sent['payloads'])

    def test_contact_failure_is_a_rejection(self, warmbly):
        with patch('flow.agents.import_campaign.create_contacts',
                   side_effect=RuntimeError('boom')):
            result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                        draft=DRAFT)
        assert result['status'] == 'rejected'
        assert result['reason'] == 'warmbly_error'
        assert result['campaign_id'] == 'camp-1'

    def test_no_emails_creates_no_campaign(self, warmbly):
        camp, _ = warmbly
        result = import_campaign.fn(business=BUSINESS, research=RESEARCH,
                                    draft=dict(DRAFT, selected_emails=[]))
        assert result['reason'] == 'no_emails'
        camp.assert_not_called()
