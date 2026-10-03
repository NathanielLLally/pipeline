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
            'crawl_excerpt': 'Call us to book.',
        }
        base.update(over)
        return base

    def test_carries_the_business_identity(self):
        out = shape_candidate(self._row())

        assert out['business']['business_name'] == 'Happy Tails'
        assert out['business']['website'] == 'https://happytails.example'
        assert out['business']['id'] == 'uuid-1'

    def test_carries_the_crawl_excerpt(self):
        assert shape_candidate(self._row())['crawl_excerpt'] == 'Call us to book.'

    def test_candidate_emails_are_not_called_verified(self):
        """Nothing here has been MX-checked; stage 2 does that.

        leads.email_verification is empty, so calling these 'verified' would
        be a lie that the research prompt would then repeat.
        """
        out = shape_candidate(self._row())

        assert 'candidate_emails' in out
        assert 'verified_emails' not in out
        assert out['candidate_emails'][0]['email'] == 'owner@happytails.example'

    def test_truncates_an_overlong_excerpt(self):
        out = shape_candidate(self._row(crawl_excerpt='x' * 50_000))

        assert len(out['crawl_excerpt']) < 50_000

    def test_tolerates_a_missing_excerpt(self):
        out = shape_candidate(self._row(crawl_excerpt=None))

        assert out['crawl_excerpt'] == ''

    def test_tolerates_no_emails(self):
        out = shape_candidate(self._row(emails=None))

        assert out['candidate_emails'] == []
