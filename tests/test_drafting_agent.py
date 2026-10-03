from unittest.mock import patch

import pytest

from flow.agents.drafting import build_drafting_prompt, drafting_agent
from flow.schemas import DraftingOutput

RESEARCH = {
    "business_name": "Happy Tails", "pain_signals": ["no online booking"],
    "personalization_hook": "phone only", "inferred_tone": "warm",
    "confidence": 0.9, "evidence": ["Call us to book"],
    "suggested_email": "owner@happytails.example", "next_url_to_check": None,
}
EMAILS = [
    {"email": "owner@happytails.example", "verified_at": "2026-10-03T00:00:00Z",
     "source": "leads.business_email"},
    {"email": "info@happytails.example", "verified_at": "2026-10-03T00:00:00Z",
     "source": "research_suggestion"},
]


def _draft(**over):
    base = dict(selected_emails=["owner@happytails.example"],
                subject="Booking enquiries you are missing",
                body="Hi -- noticed you take bookings by phone only.",
                rationale="warm tone, booking friction")
    base.update(over)
    return DraftingOutput(**base)


class TestPromptConstruction:
    def test_prompt_carries_the_hook_tone_and_pool(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS, "default")

        assert "phone only" in prompt
        assert "warm" in prompt
        assert "info@happytails.example" in prompt


class TestHappyPath:
    def test_returns_a_validated_draft(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['draft']['subject'].startswith("Booking")


class TestVerifiedPoolBinding:
    def test_address_outside_the_pool_is_rejected(self):
        """An invented address would mean mail to an unverified recipient.

        The prompt asks the model to choose from the pool; this test is here
        because asking is not enforcing.
        """
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'selected_unverified_email'
        assert result['unverified'] == ["ceo@happytails.example"]

    def test_partially_valid_selection_is_rejected_whole(self):
        """One good address does not license one bad one."""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example", "ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['unverified'] == ["ceo@happytails.example"]

    def test_agent_may_ignore_the_researcher_suggestion(self):
        """Spec 2.1: not bound by the suggestion, only by the pool."""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'

    def test_multiple_pool_addresses_are_allowed(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example",
                       "info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'

    def test_empty_pool_rejects_any_selection(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()):
            result = drafting_agent.fn(RESEARCH, [])

        assert result['status'] == 'rejected'
        assert result['reason'] == 'selected_unverified_email'
