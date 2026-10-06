"""
Unit tests for run_agents, the orchestrating flow.

run_agents exists so the nodes can exchange data as ordinary Python objects:
everything happens inside one flow, so there is no serialization boundary, no
result persistence and no database handoff between stages.

The node flows are patched here. They have their own tests; what matters at
this level is the sequencing, the skip decisions, and the accounting.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow.run_agents import run_agents

CANDIDATE = {
    'business': {'id': 'biz-1', 'business_name': 'Happy Tails',
                 'website': 'https://happytails.example'},
    'candidate_emails': [{'email': 'owner@happytails.example',
                          'source': 'website'}],
    'crawl_excerpt': 'Call us to book.',
}

RESEARCHED = {'status': 'researched', 'passes': 1,
              'research': {'business_name': 'Happy Tails', 'confidence': 0.9}}
REJECTED = {'status': 'rejected', 'reason': 'low_confidence_after_two_passes',
            'passes': 2, 'research': {'confidence': 0.3}}
DRAFTED = {'status': 'drafted', 'draft': {'subject': 's', 'body': 'b'},
           'outside_known_pool': []}


IMPORTED = {'status': 'imported', 'payloads_sent': 1,
            'created': [{'id': 'c-1'}], 'business_id': 'biz-1'}


@pytest.fixture(autouse=True)
def _no_live_analysis():
    """
    Every drafted business now goes on to analysis_agent, which calls the LLM
    proxy. Tests that predate that stage patch research and drafting only, so
    without this they reach the live proxy and hang the suite. Tests about
    analysis patch it again inside the test, which takes precedence. The same
    goes for import_contacts, which would otherwise POST to the live Warmbly.
    """
    with patch('flow.run_agents.analysis_agent',
               return_value={'status': 'analyzed', 'analysis': {}}), \
         patch('flow.run_agents.import_contacts',
               return_value=IMPORTED):
        yield


def _run(**kwargs):
    return asyncio.run(run_agents.fn(**kwargs))


class TestBatchMode:
    def test_selects_a_batch_when_no_ids_given(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]) as sel, \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run(tiers=['Tier 1'], batch_size=7)

        sel.assert_awaited_once()
        assert sel.await_args.kwargs['include_ids'] is None
        assert sel.await_args.kwargs['batch_size'] == 7
        assert result['selected'] == 1

    def test_batch_size_is_passed_through_as_the_limit(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[]) as sel, \
             patch('flow.run_agents.research_agent'), \
             patch('flow.run_agents.drafting_agent'):
            _run(batch_size=3)

        assert sel.await_args.kwargs['batch_size'] == 3


class TestExplicitIdsMode:
    def test_researches_exactly_the_named_ids(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]) as sel, \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            _run(business_ids=['biz-1'])

        assert sel.await_args.kwargs['include_ids'] == ['biz-1']

    def test_named_but_unfound_ids_are_reported_not_silently_dropped(self):
        """A caller who named a business must learn it was not found."""
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run(business_ids=['biz-1', 'biz-missing'])

        assert result['not_found'] == ['biz-missing']

    def test_batch_mode_reports_no_missing_ids(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run()

        assert result['not_found'] == []


class TestSequencing:
    def test_research_output_is_handed_to_drafting(self):
        """The whole point: an in-memory object, no serialization boundary."""
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent',
                   return_value=DRAFTED) as draft:
            _run()

        assert draft.call_args.args[0] == RESEARCHED['research']
        assert draft.call_args.args[1] == CANDIDATE['candidate_emails']

    def test_rejected_research_is_not_drafted(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=REJECTED), \
             patch('flow.run_agents.drafting_agent') as draft:
            result = _run()

        draft.assert_not_called()
        assert result['rejected'] == 1
        assert result['drafted'] == 0

    def test_crawl_text_reaches_research_untruncated(self):
        big = 'x' * 120_000
        candidate = dict(CANDIDATE, crawl_excerpt=big)
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[candidate]), \
             patch('flow.run_agents.research_agent',
                   return_value=RESEARCHED) as research, \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            _run()

        assert research.call_args.args[2] == big


class TestAccounting:
    def test_counts_each_outcome(self):
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(3)]
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent',
                   side_effect=[RESEARCHED, REJECTED, RESEARCHED]), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run()

        assert result['selected'] == 3
        assert result['researched'] == 2
        assert result['rejected'] == 1
        assert result['drafted'] == 2

    def test_per_business_outcomes_are_returned(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run()

        outcome = result['outcomes'][0]
        assert outcome['business_id'] == 'biz-1'
        assert outcome['research_status'] == 'researched'
        assert outcome['draft_status'] == 'drafted'

    def test_empty_selection_is_not_an_error(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[]), \
             patch('flow.run_agents.research_agent') as research, \
             patch('flow.run_agents.drafting_agent'):
            result = _run()

        research.assert_not_called()
        assert result['selected'] == 0
        assert result['outcomes'] == []


class TestArtifactTagging:
    """
    Each stage must run under tags(business_id, stage).

    artifacts.artifact_filename() names every file from the ambient flow run
    tags and falls back to 'unknown-{suffix}.json' when there are none. So
    without a tags() wrapper here, every business in the batch writes to the
    same two files and overwrites the last one -- the artifacts are produced,
    but only the final business survives on disk.
    """

    @staticmethod
    def _tag_capturing(return_value):
        """A subflow stand-in that records the tags ambient at call time."""
        seen = []

        def record(*args, **kwargs):
            from prefect.context import TagsContext
            seen.append(set(TagsContext.get().current_tags))
            return return_value

        return MagicMock(side_effect=record), seen

    def test_research_runs_tagged_with_business_id_and_stage(self):
        research, seen = self._tag_capturing(RESEARCHED)
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', research), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent',
                   return_value={'status': 'analyzed', 'analysis': {}}):
            _run()

        assert seen == [{'biz-1', 'research'}]

    def test_drafting_runs_tagged_with_business_id_and_stage(self):
        drafting, seen = self._tag_capturing(DRAFTED)
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', drafting), \
             patch('flow.run_agents.analysis_agent',
                   return_value={'status': 'analyzed', 'analysis': {}}):
            _run()

        assert seen == [{'biz-1', 'drafting'}]

    def test_analysis_runs_tagged_with_business_id_and_stage(self):
        analysis, seen = self._tag_capturing(
            {'status': 'analyzed', 'analysis': {}})
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent', analysis):
            _run()

        assert seen == [{'biz-1', 'analysis'}]

    def test_each_business_gets_its_own_tag_so_artifacts_do_not_collide(self):
        """Three businesses must produce three distinct artifact name sets."""
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(3)]
        research, seen = self._tag_capturing(RESEARCHED)
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent', research), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent',
                   return_value={'status': 'analyzed', 'analysis': {}}):
            _run()

        assert seen == [{'b0', 'research'}, {'b1', 'research'},
                        {'b2', 'research'}]

    def test_tags_do_not_leak_past_the_business(self):
        """A stage's tags must not still be ambient on the next business."""
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(2)]
        research, seen = self._tag_capturing(RESEARCHED)
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent', research), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent',
                   return_value={'status': 'analyzed', 'analysis': {}}):
            _run()

        assert 'b0' not in seen[1]
        assert 'drafting' not in seen[1]
        assert 'analysis' not in seen[1]

    def test_tagging_survives_a_stage_raising(self):
        """An errored business must not leave its tags on the next one."""
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(2)]
        seen = []

        def record(*args, **kwargs):
            from prefect.context import TagsContext
            seen.append(set(TagsContext.get().current_tags))
            if len(seen) == 1:
                raise RuntimeError('boom')
            return RESEARCHED

        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent',
                   MagicMock(side_effect=record)), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent',
                   return_value={'status': 'analyzed', 'analysis': {}}):
            result = _run()

        assert result['errored'] == 1
        assert seen[1] == {'b1', 'research'}


class TestFailureIsolation:
    def test_one_business_failing_does_not_abort_the_batch(self):
        """A batch of 50 must not be lost to one bad business."""
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(3)]
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent',
                   side_effect=[RESEARCHED, RuntimeError('boom'), RESEARCHED]), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED):
            result = _run()

        assert result['researched'] == 2
        assert result['errored'] == 1
        errored = [o for o in result['outcomes']
                   if o['research_status'] == 'error']
        assert 'boom' in errored[0]['error']


class TestImport:
    """Every drafted business is imported to Warmbly, unassigned to a campaign."""

    def _batch(self, research=RESEARCHED, draft=DRAFTED, **extra):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=research), \
             patch('flow.run_agents.drafting_agent', return_value=draft), \
             patch('flow.run_agents.import_contacts', **extra) as imp:
            result = _run()
        return result, imp

    def test_drafted_business_is_imported_with_its_context(self):
        result, imp = self._batch(return_value=IMPORTED)
        imp.assert_called_once()
        kwargs = imp.call_args.kwargs
        assert kwargs['business'] == CANDIDATE['business']
        assert kwargs['research'] == RESEARCHED['research']
        assert kwargs['draft'] == DRAFTED['draft']
        assert result['outcomes'][0]['import_status'] == 'imported'
        assert result['imported'] == 1

    def test_rejected_research_is_not_imported(self):
        _, imp = self._batch(research=REJECTED, return_value=IMPORTED)
        imp.assert_not_called()

    def test_undrafted_business_is_not_imported(self):
        _, imp = self._batch(
            draft={'status': 'rejected', 'reason': 'no_usable_email'},
            return_value=IMPORTED)
        imp.assert_not_called()

    def test_import_runs_even_when_analysis_fails(self):
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=[CANDIDATE]), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.analysis_agent',
                   side_effect=RuntimeError('llm down')), \
             patch('flow.run_agents.import_contacts',
                   return_value=IMPORTED) as imp:
            result = _run()
        imp.assert_called_once()
        outcome = result['outcomes'][0]
        assert outcome['import_status'] == 'imported'
        assert outcome['analysis_status'] == 'error'

    def test_import_error_is_recorded_and_batch_continues(self):
        candidates = [dict(CANDIDATE, business={'id': f'b{i}',
                                                'business_name': f'n{i}'})
                      for i in range(2)]
        with patch('flow.run_agents.fetch_candidates',
                   new_callable=AsyncMock, return_value=candidates), \
             patch('flow.run_agents.research_agent', return_value=RESEARCHED), \
             patch('flow.run_agents.drafting_agent', return_value=DRAFTED), \
             patch('flow.run_agents.import_contacts',
                   side_effect=[RuntimeError('warmbly 500'), IMPORTED]) as imp:
            result = _run()
        assert imp.call_count == 2
        assert result['outcomes'][0]['import_status'] == 'error'
        assert 'warmbly 500' in result['outcomes'][0]['import_error']
        assert result['outcomes'][1]['import_status'] == 'imported'
        assert result['imported'] == 1

    def test_rejected_import_is_counted_separately(self):
        result, _ = self._batch(return_value={
            'status': 'rejected', 'reason': 'warmbly_error', 'error': '401'})
        outcome = result['outcomes'][0]
        assert outcome['import_status'] == 'rejected'
        assert outcome['import_reason'] == 'warmbly_error'
        assert result['imported'] == 0
        assert result['import_failed'] == 1

    def test_import_is_tagged_for_its_artifacts(self):
        """Like the other stages: tags name the import's artifact files."""
        seen = []

        def record(**kwargs):
            from prefect.context import TagsContext
            seen.append(set(TagsContext.get().current_tags))
            return IMPORTED

        self._batch(side_effect=record)
        assert {'biz-1', 'import'} <= seen[0]
