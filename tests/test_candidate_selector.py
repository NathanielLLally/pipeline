"""
Unit tests for the candidate selector (spec stage 1).

The SQL text and the row-to-dict shaping are pure functions so they can be
asserted without a database. The one live query is exercised separately.
"""

import os
from datetime import datetime, timezone

import pytest

from flow.agents.selector import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_TIERS,
    build_candidate_query,
    join_crawl_pages,
    shape_candidate,
)


class TestQueryBuilder:
    def test_filters_on_the_requested_tiers(self):
        sql, params = build_candidate_query(['Tier 1'], 10, None)

        assert 'icp_tier' in sql
        assert params[0] == ['Tier 1']

    def test_limit_is_a_bound_parameter_not_interpolated(self):
        """A batch size spliced into SQL text is an injection waiting to happen."""
        sql, params = build_candidate_query(['Tier 1'], 25, None)

        assert '25' not in sql
        assert 25 in params

    def test_requires_a_candidate_email(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'leads.business_email' in sql

    def test_requires_crawl_text(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'leads.website_crawl' in sql
        assert 'text_excerpt' in sql

    def test_orders_by_icp_score_descending(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'icp_score desc' in sql.lower()

    def test_excludes_ids_when_given(self):
        sql, params = build_candidate_query(['Tier 1'], 10, ['abc', 'def'])

        assert 'not in' in sql.lower() or '!= all' in sql.lower()
        assert ['abc', 'def'] in params

    def test_no_exclusion_clause_when_none_given(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'not in' not in sql.lower()

    def test_empty_exclusion_list_adds_no_clause(self):
        """An empty list is 'exclude nothing', not 'exclude everything'."""
        sql, _ = build_candidate_query(['Tier 1'], 10, [])

        assert 'not in' not in sql.lower()


class TestDefaults:
    def test_default_tiers_are_the_high_value_ones(self):
        assert DEFAULT_TIERS == ['Tier 1', 'Tier 2']

    def test_default_batch_size_matches_the_spec(self):
        assert DEFAULT_BATCH_SIZE == 50


class TestShapeCandidate:
    def _row(self, **over):
        base = {
            'id': 'uuid-1', 'name': 'Happy Tails',
            'website': 'https://happytails.example',
            'domain': 'happytails.example', 'city': 'Austin', 'state': 'TX',
            'icp_score': 88, 'icp_tier': 'Tier 1',
            'primary_category': 'Pet groomer', 'service_category': 'grooming',
            'description': 'Grooming and daycare', 'rating': 4.8,
            'review_count': 120,
            'emails': [{'email': 'owner@happytails.example',
                        'source': 'website', 'confidence': 'high',
                        'is_role': False}],
            'crawl_pages': [
                {'url': 'https://happytails.example/', 'page_kind': 'home',
                 'text': 'Call us to book.'},
            ],
        }
        base.update(over)
        return base

    def test_carries_the_business_identity(self):
        out = shape_candidate(self._row())

        assert out['business']['business_name'] == 'Happy Tails'
        assert out['business']['website'] == 'https://happytails.example'
        assert out['business']['id'] == 'uuid-1'

    def test_carries_the_crawl_text(self):
        assert 'Call us to book.' in shape_candidate(self._row())['crawl_excerpt']

    def test_candidate_emails_are_not_called_verified(self):
        """Nothing here has been MX-checked; stage 2 does that.

        leads.email_verification is empty, so calling these 'verified' would
        be a lie that the research prompt would then repeat.
        """
        out = shape_candidate(self._row())

        assert 'candidate_emails' in out
        assert 'verified_emails' not in out
        assert out['candidate_emails'][0]['email'] == 'owner@happytails.example'

    def test_nothing_is_truncated_however_long(self):
        """The research agent reads everything available. No cap, period.

        A hook invented from half a page is worthless, and a wasted research
        pass costs the whole pipeline run, not just the tokens it saved.
        """
        huge = 'x' * 500_000
        out = shape_candidate(self._row(crawl_pages=[
            {'url': 'u', 'page_kind': 'home', 'text': huge}]))

        assert huge in out['crawl_excerpt']

    def test_every_crawled_page_is_included_not_just_the_longest(self):
        """Picking one page discarded the about/services/contact pages."""
        out = shape_candidate(self._row(crawl_pages=[
            {'url': 'u1', 'page_kind': 'home', 'text': 'a' * 5000},
            {'url': 'u2', 'page_kind': 'about', 'text': 'SHORT BUT VITAL'},
            {'url': 'u3', 'page_kind': 'services', 'text': 'board and train'},
        ]))

        assert 'SHORT BUT VITAL' in out['crawl_excerpt']
        assert 'board and train' in out['crawl_excerpt']
        assert 'a' * 5000 in out['crawl_excerpt']

    def test_pages_are_labelled_so_evidence_is_attributable(self):
        out = shape_candidate(self._row(crawl_pages=[
            {'url': 'https://x/about', 'page_kind': 'about', 'text': 'hi'}]))

        assert 'https://x/about' in out['crawl_excerpt']
        assert 'about' in out['crawl_excerpt']

    def test_raw_pages_are_also_exposed(self):
        out = shape_candidate(self._row())

        assert out['crawl_pages'][0]['page_kind'] == 'home'

    def test_tolerates_missing_pages(self):
        out = shape_candidate(self._row(crawl_pages=None))

        assert out['crawl_excerpt'] == ''

    def test_tolerates_no_emails(self):
        out = shape_candidate(self._row(emails=None))

        assert out['candidate_emails'] == []


class TestJoinCrawlPages:
    def test_skips_empty_pages_without_leaving_blank_blocks(self):
        joined = join_crawl_pages([
            {'url': 'u1', 'page_kind': 'home', 'text': 'real'},
            {'url': 'u2', 'page_kind': 'about', 'text': ''},
            {'url': 'u3', 'page_kind': 'contact', 'text': None},
        ])

        assert 'real' in joined
        assert 'u2' not in joined
        assert 'u3' not in joined

    def test_empty_input_is_an_empty_string(self):
        assert join_crawl_pages([]) == ''
        assert join_crawl_pages(None) == ''


class TestExplicitIds:
    """business_ids means 'these exactly', bypassing the batch filters.

    A caller naming a business has already decided it is worth researching, so
    the tier/email/crawl predicates must not silently drop it. If it lacks
    crawl text that is the caller's problem to see, not ours to hide.
    """

    def test_ids_mode_binds_the_ids(self):
        sql, params = build_candidate_query(None, None, None,
                                            include_ids=['a', 'b'])

        assert ['a', 'b'] in params

    def test_ids_mode_ignores_the_tier_filter(self):
        """icp_tier is a selected column in both modes; the FILTER must go."""
        sql, _ = build_candidate_query(['Tier 1'], 50, None,
                                       include_ids=['a'])

        assert 'icp_tier = any' not in sql

    def test_ids_mode_does_not_require_an_email(self):
        sql, _ = build_candidate_query(None, None, None, include_ids=['a'])

        assert 'exists' not in sql.lower()

    def test_ids_mode_has_no_outer_limit(self):
        """The id list is the limit.

        Checks for a *bound* limit rather than the word: correlated
        subqueries legitimately contain `limit 1` (the latest verification
        verdict), so a bare substring test gives a false failure.
        """
        sql, params = build_candidate_query(None, None, None,
                                            include_ids=['a'])

        assert 'limit $' not in sql.lower()
        assert len(params) == 1

    def test_batch_mode_is_unchanged_when_no_ids_given(self):
        sql, params = build_candidate_query(['Tier 1'], 10, None,
                                            include_ids=None)

        assert 'icp_tier = any' in sql
        assert 'limit' in sql.lower()
        assert params[0] == ['Tier 1']

    def test_empty_id_list_is_not_treated_as_ids_mode(self):
        """An empty list is 'you gave me nothing', not 'select everything'."""
        sql, _ = build_candidate_query(['Tier 1'], 10, None, include_ids=[])

        assert 'icp_tier = any' in sql


class TestContactFieldsForWarmbly:
    """The selector must supply what the Warmbly import consumes.

    flow/warmbly_contacts.build_contact_payloads reads decision_maker_name,
    decision_maker_title and phone off the business dict -- the fallback for
    when the research node finds no person on the page. If the selector does
    not fetch them the fallback is dead code and every imported contact gets
    a blank name and phone, silently.
    """

    def test_query_selects_the_decision_maker(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'decision_maker_name' in sql
        assert 'decision_maker_title' in sql

    def test_query_selects_the_phone(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'b.phone' in sql

    def test_ids_mode_selects_them_too(self):
        sql, _ = build_candidate_query(None, None, None, include_ids=['a'])

        assert 'decision_maker_name' in sql
        assert 'b.phone' in sql

    def test_shaped_business_carries_them(self):
        row = dict(self._row_with_contact())

        business = shape_candidate(row)['business']

        assert business['decision_maker_name'] == 'Wyatt Smith'
        assert business['decision_maker_title'] == 'Owner'
        assert business['phone'] == '972-245-3386'

    def test_absent_values_shape_to_none_not_keyerror(self):
        row = dict(self._row_with_contact(), decision_maker_name=None,
                   decision_maker_title=None, phone=None)

        business = shape_candidate(row)['business']

        assert business['decision_maker_name'] is None
        assert business['phone'] is None

    def _row_with_contact(self):
        return {
            'id': 'uuid-1', 'name': 'Happy Tails',
            'website': 'https://happytails.example',
            'domain': 'happytails.example', 'city': 'Austin', 'state': 'TX',
            'icp_score': 88, 'icp_tier': 'Tier 1',
            'primary_category': 'Pet groomer', 'service_category': 'grooming',
            'description': 'Grooming', 'rating': 4.8, 'review_count': 120,
            'phone': '972-245-3386',
            'decision_maker_name': 'Wyatt Smith',
            'decision_maker_title': 'Owner',
            'emails': [{'email': 'a@b.com', 'source': 'website'}],
            'crawl_pages': [{'url': 'u', 'page_kind': 'home', 'text': 'hi'}],
        }


class TestVerificationAndJinaExposure:
    """run_agents cannot decide what to run without these two facts.

    Before research it must know (a) which emails have no verification verdict
    yet, so they can go through mx_check, and (b) whether the business has any
    Jina-crawled pages, so jina_crawl can fill the gap. The selector read
    neither table, so neither question was answerable.

    Measured on the live database when this was written: leads.email_verification
    had 1 row and 13 businesses had any Jina page, so both paths fire for
    essentially every candidate.
    """

    def test_query_reads_the_verification_table(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'leads.email_verification' in sql

    def test_query_reads_the_jina_table(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)

        assert 'leads.website_crawl_jina' in sql

    def test_ids_mode_reads_them_too(self):
        sql, _ = build_candidate_query(None, None, None, include_ids=['a'])

        assert 'leads.email_verification' in sql
        assert 'leads.website_crawl_jina' in sql

    def test_emails_carry_a_verification_verdict(self):
        row = self._row(emails=[
            {'email': 'a@b.com', 'source': 'crawl', 'verified': True},
            {'email': 'c@d.com', 'source': 'crawl', 'verified': None}])

        emails = shape_candidate(row)['candidate_emails']

        assert emails[0]['verified'] is True
        assert emails[1]['verified'] is None

    def test_jina_pages_are_exposed_separately(self):
        row = self._row(jina_pages=[
            {'url': 'u', 'page_kind': 'home', 'text': 'jina text'}])

        assert shape_candidate(row)['jina_pages'][0]['text'] == 'jina text'

    def test_jina_text_also_reaches_the_research_context(self):
        """Everything available goes to research; Jina pages are context too."""
        row = self._row(
            crawl_pages=[{'url': 'u1', 'page_kind': 'home', 'text': 'classic'}],
            jina_pages=[{'url': 'u2', 'page_kind': 'home', 'text': 'JINA ONLY'}])

        excerpt = shape_candidate(row)['crawl_excerpt']

        assert 'classic' in excerpt
        assert 'JINA ONLY' in excerpt

    def test_absent_jina_pages_shape_to_an_empty_list(self):
        assert shape_candidate(self._row(jina_pages=None))['jina_pages'] == []

    def _row(self, **over):
        base = {
            'id': 'uuid-1', 'name': 'Happy Tails',
            'website': 'https://happytails.example',
            'domain': 'happytails.example', 'city': 'Austin', 'state': 'TX',
            'icp_score': 88, 'icp_tier': 'Tier 1',
            'primary_category': 'Pet groomer', 'service_category': 'grooming',
            'description': 'Grooming', 'rating': 4.8, 'review_count': 120,
            'phone': '555', 'decision_maker_name': None,
            'decision_maker_title': None,
            'emails': [{'email': 'a@b.com', 'source': 'crawl',
                        'verified': None}],
            'crawl_pages': [{'url': 'u', 'page_kind': 'home', 'text': 'hi'}],
            'jina_pages': None,
        }
        base.update(over)
        return base


class TestPreflightPredicates:
    """Pure predicates so run_agents' decisions are testable without a DB."""

    def test_unknown_verification_needs_mx_check(self):
        from flow.agents.selector import needs_email_verification

        candidate = {'candidate_emails': [
            {'email': 'a@b.com', 'verified': True},
            {'email': 'c@d.com', 'verified': None}]}

        assert needs_email_verification(candidate) is True

    def test_all_verdicts_present_needs_no_mx_check(self):
        from flow.agents.selector import needs_email_verification

        candidate = {'candidate_emails': [
            {'email': 'a@b.com', 'verified': True},
            {'email': 'c@d.com', 'verified': False}]}

        assert needs_email_verification(candidate) is False

    def test_a_false_verdict_is_a_verdict(self):
        """Rejected is answered; only a missing row is unknown."""
        from flow.agents.selector import needs_email_verification

        assert needs_email_verification(
            {'candidate_emails': [{'email': 'a@b.com', 'verified': False}]}
        ) is False

    def test_no_emails_needs_no_mx_check(self):
        from flow.agents.selector import needs_email_verification

        assert needs_email_verification({'candidate_emails': []}) is False

    def test_missing_jina_pages_needs_a_crawl(self):
        from flow.agents.selector import needs_jina_crawl

        assert needs_jina_crawl({'jina_pages': []}) is True

    def test_existing_jina_pages_need_no_crawl(self):
        from flow.agents.selector import needs_jina_crawl

        assert needs_jina_crawl(
            {'jina_pages': [{'url': 'u', 'text': 't'}]}) is False
