"""
leads.outreach_pipeline: which businesses a batch has claimed and how each ended.

Pure parts only -- status mapping and SQL shape. The database calls are
mocked at the run_agents level (tests/test_run_agents.py).
"""

from flow.agents.selector import build_candidate_query
from flow.outreach_state import CLAIM_SQL, FORCE_CLAIM_SQL, outcome_status


class TestOutcomeStatus:
    def test_imported(self):
        assert outcome_status({'research_status': 'researched', 'draft_status': 'drafted',
                               'import_status': 'imported'}) == ('imported', None)

    def test_research_rejection_is_final(self):
        assert outcome_status({'research_status': 'rejected',
                               'rejection_reason': 'low_confidence_after_two_passes'}) == (
            'rejected', 'low_confidence_after_two_passes')

    def test_draft_rejection_is_final(self):
        status, reason = outcome_status({'research_status': 'researched',
                                         'draft_status': 'rejected'})
        assert status == 'rejected' and 'draft' in reason

    def test_errors_are_retryable(self):
        assert outcome_status({'research_status': 'error', 'error': 'Boom'}) == ('failed', 'Boom')
        assert outcome_status({'research_status': 'researched', 'draft_status': 'error',
                               'error': 'Boom'})[0] == 'failed'

    def test_failed_import_is_retryable(self):
        status, reason = outcome_status({'research_status': 'researched', 'draft_status': 'drafted',
                                         'import_status': 'error', 'import_error': '429'})
        assert (status, reason) == ('failed', '429')


class TestClaimSql:
    def test_claims_atomically_and_reports_what_it_got(self):
        assert 'on conflict (business_id)' in CLAIM_SQL.lower()
        assert 'returning business_id' in CLAIM_SQL.lower()

    def test_only_failed_or_stale_claims_are_taken_over(self):
        sql = CLAIM_SQL.lower()
        assert "status = 'failed'" in sql
        assert 'claimed_at <' in sql

    def test_named_runs_take_over_unconditionally(self):
        assert 'where' not in FORCE_CLAIM_SQL.lower().split('do update')[1]


class TestSelectionSkipsProcessed:
    def test_processed_businesses_are_excluded_when_tracking_exists(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None, exclude_processed=True)
        assert 'leads.outreach_pipeline' in sql
        assert "status <> 'failed'" in sql

    def test_no_tracking_clause_without_the_table(self):
        sql, _ = build_candidate_query(['Tier 1'], 10, None)
        assert 'outreach_pipeline' not in sql

    def test_named_ids_bypass_the_exclusion(self):
        sql, _ = build_candidate_query(None, None, None, ['a'], exclude_processed=True)
        assert 'outreach_pipeline' not in sql
