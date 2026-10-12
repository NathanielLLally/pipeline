"""
Unit tests for the Warmbly contact import (spec stage 5).

The wire contract was captured from a real request via `warmbly contact create
--debug`, not inferred:

    POST https://api.crm.happytailspawcare.com/v1/contacts   -> HTTP 200
    response: a bare JSON array of created contacts

Two things the published docs get wrong for this instance, both verified:
  * the path is /v1/contacts, not /contacts
  * verification_status="unknown" does not stick; it comes back "", so we
    do not send it rather than pretend we set it

Custom fields must be nested under `custom_fields` and are string-valued. A
top-level `-f research_hook=...` was silently dropped, which is why every
non-standard value goes in that object.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from flow.warmbly_contacts import (
    CONTACTS_PATH,
    DRAFTS_PATH,
    build_compose_drafts,
    build_contact_payloads,
    compose_draft_id,
    create_compose_drafts,
    create_contacts,
)

BUSINESS = {
    'id': 'biz-1', 'business_name': 'Home Dog Training',
    'website': 'https://www.homedogtrainingllc.com/', 'city': 'Rockwall',
    'state': 'TX', 'icp_tier': 'Tier 1', 'icp_score': 95,
    'primary_category': 'Dog trainer', 'phone': '972-245-3386',
    'decision_maker_name': 'Wyatt Smith', 'decision_maker_title': 'Owner',
}
RESEARCH = {
    'business_name': 'Home Dog Training', 'personalization_hook': 'phone only',
    'inferred_tone': 'warm', 'confidence': 0.88,
    'pain_signals': ['no online booking', 'no lead form'],
    'evidence': ['Call us to book'], 'suggested_email': 'a@b.com',
    'contact_name': None, 'contact_title': None,
}
DRAFT = {
    'selected_emails': ['training@homedogtraining.com'],
    'subject': 'Live dog-owner leads', 'body': 'Noticed the HOME Method.',
    'rationale': 'warm tone, booking friction',
}


class TestPayloadShape:
    def test_one_payload_per_selected_email(self):
        draft = dict(DRAFT, selected_emails=['a@x.com', 'b@x.com'])

        payloads = build_contact_payloads(BUSINESS, RESEARCH, draft)

        assert [p['email'] for p in payloads] == ['a@x.com', 'b@x.com']

    def test_company_is_the_business_name(self):
        assert build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]['company'] \
            == 'Home Dog Training'

    def test_splits_the_decision_maker_name(self):
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert p['first_name'] == 'Wyatt'
        assert p['last_name'] == 'Smith'

    def test_single_word_name_becomes_first_name_only(self):
        business = dict(BUSINESS, decision_maker_name='Wyatt')

        p = build_contact_payloads(business, RESEARCH, DRAFT)[0]

        assert p['first_name'] == 'Wyatt'
        assert p['last_name'] == ''

    def test_missing_decision_maker_leaves_names_empty(self):
        business = dict(BUSINESS, decision_maker_name=None)

        p = build_contact_payloads(business, RESEARCH, DRAFT)[0]

        assert p['first_name'] == ''
        assert p['last_name'] == ''

    def test_three_part_name_keeps_the_remainder_as_surname(self):
        business = dict(BUSINESS, decision_maker_name='Mary Jane Watson')

        p = build_contact_payloads(business, RESEARCH, DRAFT)[0]

        assert p['first_name'] == 'Mary'
        assert p['last_name'] == 'Jane Watson'


class TestEveryPoolEmailIsImported:
    """Every address in the research pool becomes a contact; only the ones
    drafting chose carry the draft. Warmbly runs its own verification, so our
    verdict decides who gets drafted, not who gets imported."""

    POOL = [
        {'email': 'training@homedogtraining.com', 'source': 'crawl', 'verified': True},
        {'email': 'info@homedogtraining.com', 'source': 'crawl', 'verified': False},
        {'email': 'wyatt@homedogtraining.com', 'source': 'hunter', 'verified': None},
    ]

    def _payloads(self, pool=None, draft=DRAFT):
        return build_contact_payloads(BUSINESS, RESEARCH, draft,
                                      candidate_emails=pool or self.POOL)

    def test_unselected_pool_emails_are_imported_after_the_selected(self):
        assert [p['email'] for p in self._payloads()] == [
            'training@homedogtraining.com',
            'info@homedogtraining.com',
            'wyatt@homedogtraining.com',
        ]

    def test_only_selected_contacts_carry_the_draft(self):
        by_email = {p['email']: p['custom_fields'] for p in self._payloads()}

        assert by_email['training@homedogtraining.com']['draft_subject'] == \
            DRAFT['subject']
        for email in ('info@homedogtraining.com', 'wyatt@homedogtraining.com'):
            assert not any(k.startswith('draft_') for k in by_email[email]), email

    def test_unselected_contacts_still_carry_research_and_business(self):
        fields = {p['email']: p['custom_fields']
                  for p in self._payloads()}['info@homedogtraining.com']

        assert fields['research_hook'] == RESEARCH['personalization_hook']
        assert fields['leads_business_id'] == BUSINESS['id']

    def test_an_address_in_both_lists_is_imported_once(self):
        pool = [{'email': 'Training@HomeDogTraining.com', 'verified': True}]

        emails = [p['email'] for p in self._payloads(pool=pool)]

        assert emails == ['training@homedogtraining.com']

    def test_our_verification_verdict_is_never_sent(self):
        for p in self._payloads():
            assert 'verification_status' not in p
            assert 'verified' not in p['custom_fields']

    def test_without_a_pool_only_the_selected_are_imported(self):
        payloads = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        assert [p['email'] for p in payloads] == DRAFT['selected_emails']


class TestCustomFields:
    def _cf(self, **over):
        business = dict(BUSINESS, **over)
        return build_contact_payloads(business, RESEARCH, DRAFT)[0]['custom_fields']

    def test_every_value_is_a_string(self):
        """The API stores string key/value pairs only."""
        for key, value in self._cf().items():
            assert isinstance(value, str), f'{key} is {type(value).__name__}'

    def test_carries_the_draft(self):
        cf = self._cf()

        assert cf['draft_subject'] == 'Live dog-owner leads'
        assert cf['draft_body'] == 'Noticed the HOME Method.'
        assert cf['draft_rationale'] == 'warm tone, booking friction'

    def test_carries_the_research(self):
        cf = self._cf()

        assert cf['research_hook'] == 'phone only'
        assert cf['research_tone'] == 'warm'
        assert cf['research_confidence'] == '0.88'

    def test_lists_are_joined_not_json_dumped(self):
        """A human reads these in the Warmbly UI."""
        assert self._cf()['research_pain_signals'] == \
            'no online booking; no lead form'

    def test_carries_business_provenance(self):
        cf = self._cf()

        assert cf['leads_business_id'] == 'biz-1'
        assert cf['business_icp_tier'] == 'Tier 1'
        assert cf['business_website'] == 'https://www.homedogtrainingllc.com/'

    def test_omits_empty_values_rather_than_sending_blanks(self):
        cf = self._cf(decision_maker_title=None)

        assert 'decision_maker_title' not in cf


class TestDeliberateOmissions:
    def test_does_not_assign_a_campaign(self):
        """Assignment is the human review gate; sending needs it, so it is
        deliberately left to the reviewer (spec 2.1)."""
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert p.get('campaigns', []) == []

    def test_does_not_claim_a_verification_status(self):
        """Nothing is MX-verified yet, and 'unknown' does not persist anyway."""
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert 'verification_status' not in p


class TestCreateContacts:
    def _ok(self, payloads):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = [dict(p, id=f'id-{i}')
                               for i, p in enumerate(payloads)]
        r.raise_for_status.return_value = None
        return r

    def test_posts_to_the_captured_path(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        payloads = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        with patch('flow.warmbly_contacts.httpx.post',
                   return_value=self._ok(payloads)) as post:
            create_contacts(payloads)

        assert post.call_args.args[0] == f'https://api.example.test{CONTACTS_PATH}'

    def test_sends_the_bearer_token(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        payloads = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        with patch('flow.warmbly_contacts.httpx.post',
                   return_value=self._ok(payloads)) as post:
            create_contacts(payloads)

        assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer wb-token'

    def test_sends_an_idempotency_key_so_reruns_do_not_duplicate(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        payloads = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        with patch('flow.warmbly_contacts.httpx.post',
                   return_value=self._ok(payloads)) as post:
            create_contacts(payloads, idempotency_key='leads-biz-1')

        assert post.call_args.kwargs['headers']['Idempotency-Key'] == 'leads-biz-1'

    def test_returns_the_created_contacts(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        payloads = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        with patch('flow.warmbly_contacts.httpx.post', return_value=self._ok(payloads)):
            created = create_contacts(payloads)

        assert created[0]['id'] == 'id-0'

    def test_missing_token_is_an_explicit_error(self, monkeypatch):
        monkeypatch.delenv('WARMBLY_API_TOKEN', raising=False)
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')

        with pytest.raises(RuntimeError, match='WARMBLY_API_TOKEN'):
            create_contacts(build_contact_payloads(BUSINESS, RESEARCH, DRAFT))

    def test_empty_payload_list_makes_no_request(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')

        with patch('flow.warmbly_contacts.httpx.post') as post:
            assert create_contacts([]) == []

        post.assert_not_called()


class TestPersonPrecedence:
    """Research reads the live page; the DB column is a prior enrichment pass."""

    def test_research_extracted_person_wins(self):
        research = dict(RESEARCH, contact_name='Megan Fields',
                        contact_title='Head Trainer')

        p = build_contact_payloads(BUSINESS, research, DRAFT)[0]

        assert p['first_name'] == 'Megan'
        assert p['last_name'] == 'Fields'
        assert p['custom_fields']['decision_maker_title'] == 'Head Trainer'

    def test_falls_back_to_the_database_column(self):
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert p['first_name'] == 'Wyatt'

    def test_neither_source_leaves_it_empty(self):
        business = dict(BUSINESS, decision_maker_name=None)

        p = build_contact_payloads(business, RESEARCH, DRAFT)[0]

        assert p['first_name'] == ''


class TestSubscriptionState:
    """Imported contacts arrive subscribed.

    Sent explicitly rather than left to the API default, so the state does not
    change silently if the instance's default does.
    """

    def test_payload_sets_subscribed(self):
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert p['subscribed'] is True

    def test_every_payload_in_a_multi_email_draft_is_subscribed(self):
        draft = dict(DRAFT, selected_emails=['a@x.com', 'b@x.com'])

        payloads = build_contact_payloads(BUSINESS, RESEARCH, draft)

        assert [p['subscribed'] for p in payloads] == [True, True]

    def test_old_env_override_no_longer_unsubscribes(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_IMPORT_SUBSCRIBED', '0')
        import importlib
        import flow.warmbly_contacts as mod
        importlib.reload(mod)
        try:
            p = mod.build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]
            assert p['subscribed'] is True
        finally:
            monkeypatch.delenv('WARMBLY_IMPORT_SUBSCRIBED')
            importlib.reload(mod)


class TestIdempotencyKey:
    """The key must cover the payload, not just the business.

    A live run failed with 409 "Idempotency-Key was already used with a
    different request": the key was leads-business-{id}, stable per business,
    so once a business had been imported any later change to its draft -- or
    to the subscribed flag -- collided with the earlier key forever. Warmbly
    is right to reject that; the key was wrong.

    What we want: an identical re-run is deduplicated (a safe retry), while a
    changed draft is a genuinely new request.
    """

    def test_identical_payloads_give_the_same_key(self):
        from flow.warmbly_contacts import contact_idempotency_key

        a = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)
        b = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)

        assert contact_idempotency_key(BUSINESS, a) == \
            contact_idempotency_key(BUSINESS, b)

    def test_a_changed_draft_gives_a_different_key(self):
        from flow.warmbly_contacts import contact_idempotency_key

        a = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)
        b = build_contact_payloads(
            BUSINESS, RESEARCH, dict(DRAFT, subject='Different subject'))

        assert contact_idempotency_key(BUSINESS, a) != \
            contact_idempotency_key(BUSINESS, b)

    def test_a_changed_subscribed_flag_gives_a_different_key(self):
        """The exact collision seen live."""
        from flow.warmbly_contacts import contact_idempotency_key

        a = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)
        b = [dict(p, subscribed=not p['subscribed']) for p in a]

        assert contact_idempotency_key(BUSINESS, a) != \
            contact_idempotency_key(BUSINESS, b)

    def test_key_still_names_the_business_for_legibility(self):
        from flow.warmbly_contacts import contact_idempotency_key

        key = contact_idempotency_key(
            BUSINESS, build_contact_payloads(BUSINESS, RESEARCH, DRAFT))

        assert 'biz-1' in key

    def test_key_order_does_not_depend_on_dict_ordering(self):
        """Same content, different insertion order, same key."""
        from flow.warmbly_contacts import contact_idempotency_key

        a = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)
        b = [{k: p[k] for k in reversed(list(p))} for p in a]

        assert contact_idempotency_key(BUSINESS, a) == \
            contact_idempotency_key(BUSINESS, b)


class TestComposeDrafts:
    """The draft also lands in Warmbly's Unibox Drafts, ready to review and send.

    Contacts alone carried the draft only as custom fields, which nothing in
    Warmbly renders as an email. PUT /v1/unibox/drafts/:id creates a compose
    draft owned by the API key's user; the id is client-generated, so the PUT
    is idempotent and a re-run updates the same draft instead of adding one.
    """

    def _ok(self, draft_id):
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = {'id': draft_id}
        r.raise_for_status.return_value = None
        return r

    def test_one_draft_per_selected_email(self):
        draft = dict(DRAFT, selected_emails=['a@x.com', 'b@x.com'])

        drafts = build_compose_drafts(BUSINESS, draft)

        assert [d['body']['to'] for d in drafts] == [['a@x.com'], ['b@x.com']]

    def test_draft_carries_subject_and_body(self):
        d = build_compose_drafts(BUSINESS, DRAFT)[0]['body']

        assert d['subject'] == DRAFT['subject']
        assert d['body'] == DRAFT['body']

    def test_draft_id_is_a_stable_uuid_per_business_and_address(self):
        import uuid
        a = compose_draft_id('biz-1', 'a@x.com')

        assert uuid.UUID(a)
        assert a == compose_draft_id('biz-1', 'A@X.com')
        assert a != compose_draft_id('biz-1', 'b@x.com')
        assert a != compose_draft_id('biz-2', 'a@x.com')

    def test_puts_each_draft_to_its_id(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        drafts = build_compose_drafts(BUSINESS, DRAFT)

        with patch('flow.warmbly_contacts.httpx.put',
                   return_value=self._ok(drafts[0]['id'])) as put:
            ids = create_compose_drafts(drafts)

        url = put.call_args.args[0]
        assert url == f"https://api.example.test{DRAFTS_PATH}/{drafts[0]['id']}"
        assert put.call_args.kwargs['json'] == drafts[0]['body']
        assert put.call_args.kwargs['headers']['Authorization'] == 'Bearer wb-token'
        assert ids == [drafts[0]['id']]

    def test_http_error_is_raised_with_the_reason(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_API_URL', 'https://api.example.test')
        monkeypatch.setenv('WARMBLY_API_TOKEN', 'wb-token')
        bad = MagicMock(spec=httpx.Response)
        bad.status_code = 403
        bad.text = 'missing WRITE_UNIBOX'
        bad.raise_for_status.side_effect = httpx.HTTPStatusError(
            '403', request=MagicMock(), response=bad)

        with patch('flow.warmbly_contacts.httpx.put', return_value=bad), \
             pytest.raises(RuntimeError, match='WRITE_UNIBOX'):
            create_compose_drafts(build_compose_drafts(BUSINESS, DRAFT))
