"""
The research node writes the person it found back to leads.businesses.

Research extracts contact_name/contact_title from the live pages, and that is
the better source: decision_maker_name is populated for only 16% of Tier 1
and 5% of Tier 2 businesses (measured 2026-10-06). Writing it back means the
next run -- and every other consumer of leads.businesses -- gets the benefit,
instead of the name living only in a flow run's output.

The write is isolated in its own module so research_agent stays unit-testable
without a database.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from flow.decision_maker import build_decision_maker_update


class TestUpdateStatement:
    def test_sets_name_title_and_source(self):
        sql, params = build_decision_maker_update(
            'biz-1', 'Wyatt Smith', 'Owner', 0.9)

        assert 'leads.businesses' in sql
        assert 'decision_maker_name' in sql
        assert 'decision_maker_title' in sql
        assert 'decision_maker_source' in sql

    def test_source_is_a_url_matching_the_existing_convention(self):
        """The column already holds the page a name came from, not a label.

        Existing values are URLs like http://canine-u.com/, so writing
        'research_agent' there would make the column mean two things.
        """
        _, params = build_decision_maker_update(
            'biz-1', 'Wyatt Smith', 'Owner', 0.9,
            source_url='https://happytails.example/about')

        assert 'https://happytails.example/about' in params

    def test_binds_the_business_id_not_interpolates_it(self):
        sql, params = build_decision_maker_update(
            'biz-1', 'Wyatt Smith', None, 0.9)

        assert 'biz-1' not in sql
        assert 'biz-1' in params

    def test_confidence_uses_the_existing_text_vocabulary(self):
        """The column is text holding high/medium, not a float."""
        from flow.decision_maker import confidence_label

        assert confidence_label(0.95) == 'high'
        assert confidence_label(0.7) == 'medium'
        assert confidence_label(0.2) == 'low'

    def test_the_written_confidence_is_a_label(self):
        _, params = build_decision_maker_update(
            'biz-1', 'Wyatt Smith', 'Owner', 0.88)

        assert 'high' in params
        assert 0.88 not in params


class TestWriteGuards:
    """What must NOT be written."""

    @pytest.mark.parametrize('name', [None, '', '   '])
    def test_no_name_means_no_write(self, name):
        from flow.decision_maker import record_decision_maker

        with patch('flow.decision_maker._connect', new_callable=AsyncMock) as c:
            import asyncio
            written = asyncio.run(record_decision_maker('biz-1', name, 'Owner'))

        assert written is False
        c.assert_not_awaited()

    def test_missing_business_id_means_no_write(self):
        from flow.decision_maker import record_decision_maker
        import asyncio

        with patch('flow.decision_maker._connect', new_callable=AsyncMock) as c:
            written = asyncio.run(record_decision_maker(None, 'Wyatt', 'Owner'))

        assert written is False
        c.assert_not_awaited()

    def test_a_real_name_is_written(self):
        from flow.decision_maker import record_decision_maker
        import asyncio

        conn = AsyncMock()
        with patch('flow.decision_maker._connect',
                   new_callable=AsyncMock, return_value=conn):
            written = asyncio.run(
                record_decision_maker('biz-1', 'Wyatt Smith', 'Owner', 0.9))

        assert written is True
        conn.execute.assert_awaited_once()

    def test_a_database_failure_is_swallowed(self):
        """A failed writeback must not fail the research that produced it."""
        from flow.decision_maker import record_decision_maker
        import asyncio

        with patch('flow.decision_maker._connect',
                   new_callable=AsyncMock,
                   side_effect=RuntimeError('db down')):
            written = asyncio.run(
                record_decision_maker('biz-1', 'Wyatt Smith', 'Owner'))

        assert written is False


class TestCallableFromEitherContext:
    """The writeback is invoked from a SYNC flow inside an ASYNC parent.

    research_agent is sync; run_agents is async and calls it as a subflow, so
    an event loop is already running on that thread. A bare asyncio.run()
    therefore raised "asyncio.run() cannot be called from a running event
    loop" and the write silently never happened -- caught only because the
    writeback logs its own failures.
    """

    def test_works_with_no_running_loop(self):
        from flow.decision_maker import record_decision_maker_sync

        conn = AsyncMock()
        with patch('flow.decision_maker._connect',
                   new_callable=AsyncMock, return_value=conn):
            assert record_decision_maker_sync('biz-1', 'Wyatt', 'Owner') is True

    def test_works_inside_a_running_loop(self):
        from flow.decision_maker import record_decision_maker_sync
        import asyncio

        conn = AsyncMock()

        async def caller():
            # Exactly the situation in the pipeline: a loop is already running
            # on this thread when the sync helper is called.
            with patch('flow.decision_maker._connect',
                       new_callable=AsyncMock, return_value=conn):
                return record_decision_maker_sync('biz-1', 'Wyatt', 'Owner')

        assert asyncio.run(caller()) is True

    def test_a_skipped_write_needs_no_loop_gymnastics(self):
        from flow.decision_maker import record_decision_maker_sync
        import asyncio

        async def caller():
            return record_decision_maker_sync('biz-1', None, 'Owner')

        assert asyncio.run(caller()) is False
