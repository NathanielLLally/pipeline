"""
Unit tests for the Warmbly webhook receiver flow.

The receiver consumes the Prefect event emitted by the HTTP listener. It
deliberately does no signature validation: that needs the raw request bytes,
which only the listener holds.
"""

import json
from unittest.mock import patch

import pytest

from flow.warmbly_webhook_receiver import (
    classify_event,
    seen_event_ids,
    warmbly_webhook_receiver,
)

CONTACT_EVENT = {
    "id": "aa11", "event_type": "contact.created",
    "organization_id": "org-1", "data": {"contact_id": "c1"},
}


@pytest.fixture(autouse=True)
def clear_seen():
    seen_event_ids.clear()
    yield
    seen_event_ids.clear()


class TestClassifyEvent:
    def test_classifies_contact_events(self):
        assert classify_event(CONTACT_EVENT) == 'contact'

    def test_classifies_campaign_events(self):
        assert classify_event({"event_type": "campaign.reply_received"}) == 'campaign'

    def test_unknown_prefix_is_other(self):
        assert classify_event({"event_type": "weird.thing"}) == 'other'

    def test_missing_event_type_is_other(self):
        assert classify_event({}) == 'other'

    def test_null_event_type_is_other(self):
        assert classify_event({"event_type": None}) == 'other'


class TestReceiverFlow:
    def test_returns_event_identity(self):
        result = warmbly_webhook_receiver(CONTACT_EVENT)

        assert result['status'] == 'processed'
        assert result['event_type'] == 'contact.created'
        assert result['event_id'] == 'aa11'
        assert result['organization_id'] == 'org-1'

    def test_duplicate_event_id_is_not_processed_twice(self):
        """Warmbly retries on timeout, so the same id can arrive twice."""
        first = warmbly_webhook_receiver(CONTACT_EVENT)
        second = warmbly_webhook_receiver(CONTACT_EVENT)

        assert first['status'] == 'processed'
        assert second['status'] == 'duplicate'

    def test_distinct_ids_both_processed(self):
        other = dict(CONTACT_EVENT, id="bb22")

        assert warmbly_webhook_receiver(CONTACT_EVENT)['status'] == 'processed'
        assert warmbly_webhook_receiver(other)['status'] == 'processed'

    def test_payload_without_id_is_still_processed(self):
        """A missing id cannot be deduplicated; process rather than drop."""
        result = warmbly_webhook_receiver({"event_type": "contact.created"})

        assert result['status'] == 'processed'
