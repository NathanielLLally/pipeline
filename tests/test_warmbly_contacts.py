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
    build_contact_payloads,
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
    """Imported contacts must arrive unsubscribed.

    The API defaults to subscribed when the field is omitted, which it was --
    so every imported contact landed subscribed and therefore sendable before
    anyone had reviewed the draft. The review gate is the point: a contact is
    created, a human reads the draft, and only then is it assigned to a
    campaign and made sendable.
    """

    def test_payload_sets_unsubscribed(self):
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert p['subscribed'] is False

    def test_the_field_is_present_not_merely_falsy(self):
        """Omitting it is what caused the bug; it must be sent explicitly."""
        p = build_contact_payloads(BUSINESS, RESEARCH, DRAFT)[0]

        assert 'subscribed' in p

    def test_every_payload_in_a_multi_email_draft_is_unsubscribed(self):
        draft = dict(DRAFT, selected_emails=['a@x.com', 'b@x.com'])

        payloads = build_contact_payloads(BUSINESS, RESEARCH, draft)

        assert [p['subscribed'] for p in payloads] == [False, False]

    def test_can_be_overridden_for_a_deliberate_opt_in_import(self, monkeypatch):
        monkeypatch.setenv('WARMBLY_IMPORT_SUBSCRIBED', '1')
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
        b = [dict(p, subscribed=True) for p in a]

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
