"""
Unit tests for flow.email_pool: what mxCheck.pl's verdict means, and the
domain facts the import uses to tell a business's own address from another
business's.

The error strings are real ones from leads.email_verification, not invented:
a boolean `verified` collapses "the mailbox does not exist" and "the check
could not finish" into the same False, and the pipeline used to treat both
alike -- the 2026-10-14 Home Dog Training run drafted alongside a dead
address that scored the same 65 extraction confidence as a live one.
"""

import pytest

from flow.email_pool import (
    deliverability,
    describe_address,
    email_domain,
    is_free_mail,
    site_domain,
)


def _entry(verified, error=None, **over):
    return {'email': 'a@b.com', 'verified': verified, 'error': None,
            'verification_error': error, **over}


class TestDeliverability:
    def test_an_accepted_address_is_valid(self):
        assert deliverability(_entry(True)) == 'valid'

    def test_no_verdict_is_unchecked(self):
        assert deliverability(_entry(None)) == 'unchecked'

    def test_an_entry_with_no_verdict_key_is_unchecked(self):
        assert deliverability({'email': 'a@b.com'}) == 'unchecked'

    @pytest.mark.parametrize('error', [
        "5.1.1 The email account that you tried to reach does not exist. "
        "Please try\n5.1.1 double-checking the recipient's email address",
        "5.2.2 The recipient's inbox is out of storage space and inactive.",
        "5.4.1 Recipient address rejected: Access denied. For more information",
        '\n5.1.1 leading whitespace is tolerated',
        'no mx record for hostname example.invalid',
        'invalid email format',
        'User unknown',
    ])
    def test_a_permanent_rejection_is_invalid(self, error):
        assert deliverability(_entry(False, error)) == 'invalid'

    def test_a_domain_that_accepts_anything_is_catch_all(self):
        error = 'false positive check failed for mx smtp.google.com'

        assert deliverability(_entry(False, error)) == 'catch_all'

    @pytest.mark.parametrize('error', [
        '[Net::SMTP] Connection closed',
        'could not connect to smtp server mx.example.com',
        'exception: timeout',
        '5.7.1 Service unavailable; client host blocked',
        None,
        '',
    ])
    def test_a_check_that_did_not_finish_is_unknown(self, error):
        """A blocked probe or a dropped connection says nothing about the
        mailbox; 5.7.x is a policy refusal of *us*, not of the address."""
        assert deliverability(_entry(False, error)) == 'unknown'


class TestDescribeAddress:
    BUSINESS = {'website': 'https://www.happytails.example/', 'domain': None}

    def test_names_the_status(self):
        line = describe_address(
            _entry(False, '[Net::SMTP] Connection closed',
                   email='info@happytails.example'), self.BUSINESS)

        assert line.startswith('info@happytails.example')
        assert 'unknown' in line

    def test_says_a_role_address_is_a_shared_inbox(self):
        line = describe_address(
            _entry(True, email='info@happytails.example', is_role=True),
            self.BUSINESS)

        assert 'shared inbox' in line

    def test_flags_an_address_off_the_website_domain(self):
        line = describe_address(
            _entry(True, email='info@otherbiz.example'), self.BUSINESS)

        assert "not on their website's domain" in line

    def test_does_not_flag_free_mail(self):
        line = describe_address(
            _entry(True, email='happytails@gmail.com'), self.BUSINESS)

        assert "not on their website's domain" not in line

    def test_never_calls_an_address_verified_unless_it_is(self):
        line = describe_address(
            _entry(False, 'false positive check failed for mx x'),
            self.BUSINESS)

        assert 'verified' not in line.replace('unverified', '')


class TestDomains:
    def test_email_domain_is_lowercased(self):
        assert email_domain('Training@HomeDogTraining.com') == \
            'homedogtraining.com'

    def test_email_domain_of_garbage_is_empty(self):
        assert email_domain('not an address') == ''

    def test_site_domain_prefers_the_domain_column(self):
        assert site_domain({'domain': 'HomeDogTrainingLLC.com',
                            'website': 'https://elsewhere.example'}) == \
            'homedogtrainingllc.com'

    def test_site_domain_falls_back_to_the_website_host_without_www(self):
        assert site_domain({'website': 'https://www.homedogtrainingllc.com/x'}) \
            == 'homedogtrainingllc.com'

    def test_site_domain_of_nothing_is_empty(self):
        assert site_domain({}) == ''

    @pytest.mark.parametrize('domain', ['gmail.com', 'yahoo.com',
                                        'hotmail.com', 'outlook.com',
                                        'icloud.com', 'aol.com'])
    def test_free_mail(self, domain):
        assert is_free_mail(domain)

    def test_a_business_domain_is_not_free_mail(self):
        assert not is_free_mail('k9culturedogtraining.com')


from flow.email_pool import address_rank, choose_recipient, greeting_name, person_for

LAURA = {'name': 'Laura', 'title': None, 'email': None,
         'is_decision_maker': True}
CHARLES = {'name': 'Charles Smith', 'title': None, 'email': None,
           'is_decision_maker': False}
WYATT = {'name': 'Wyatt', 'title': None, 'email': None,
         'is_decision_maker': False}

SHARED = {'email': 'training@homedogtraining.com', 'is_role': True,
          'verified': False, 'verification_error': '[Net::SMTP] Connection closed'}
WYATTS = {'email': 'wyatt@homedogtrainingllc.com', 'is_role': False,
          'verified': False,
          'verification_error': 'false positive check failed for mx x'}


class TestPersonFor:
    def test_the_address_research_tied_to_them(self):
        megan = {'name': 'Megan', 'email': 'megs@x.example'}

        assert person_for('Megs@X.example', [LAURA, megan]) is megan

    def test_a_local_part_that_is_their_first_name(self):
        assert person_for('charles.smith@x.example', [LAURA, CHARLES]) is CHARLES

    def test_a_shared_inbox_is_nobody(self):
        assert person_for('training@x.example', [LAURA, WYATT]) is None

    def test_short_first_names_do_not_match_by_local_part(self):
        assert person_for('al@x.example', [{'name': 'Al'}]) is None


class TestChooseRecipient:
    """A trainer's own address must not beat the shared inbox when the
    owners are known -- the 2026-10-14 replay wrote to Wyatt."""

    def test_shared_inbox_beats_a_non_decision_makers_own_address(self):
        assert choose_recipient([WYATTS, SHARED], [LAURA, WYATT]) is SHARED

    def test_a_decision_makers_own_address_beats_the_shared_inbox(self):
        wyatt_owner = dict(WYATT, is_decision_maker=True)

        assert choose_recipient([SHARED, WYATTS], [wyatt_owner]) is WYATTS

    def test_an_accepted_mailbox_beats_everything_unconfirmed(self):
        accepted = dict(WYATTS, verified=True, verification_error=None)

        assert choose_recipient([SHARED, accepted], [LAURA, WYATT]) is accepted

    def test_ties_keep_pool_order(self):
        other = dict(SHARED, email='info@homedogtraining.com')

        assert choose_recipient([SHARED, other], []) is SHARED

    def test_an_empty_pool_has_no_recipient(self):
        assert choose_recipient([], [LAURA]) is None

    def test_rank_is_lower_for_better(self):
        assert address_rank(SHARED, [LAURA, WYATT]) < \
            address_rank(WYATTS, [LAURA, WYATT])


class TestGreetingName:
    def test_the_person_whose_address_it_is(self):
        assert greeting_name(WYATTS['email'], [LAURA, WYATT]) == 'Wyatt'

    def test_a_shared_inbox_greets_the_first_decision_maker(self):
        assert greeting_name(SHARED['email'], [WYATT, LAURA]) == 'Laura'

    def test_a_shared_inbox_with_no_decision_maker_greets_nobody(self):
        """Greeting whoever is listed first could be a trainer."""
        assert greeting_name(SHARED['email'], [WYATT, CHARLES]) is None

    def test_uses_the_first_name_only(self):
        charles = dict(CHARLES, is_decision_maker=True)

        assert greeting_name(SHARED['email'], [charles]) == 'Charles'
