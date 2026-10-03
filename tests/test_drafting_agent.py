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


class TestPoolIsReportedNotEnforced:
    """Warmbly will not send to anything it has not itself verified.

    The verified pool is an input and a prompt steer, not an allowlist.
    Rejecting a draft here because an address is missing from the pipeline's
    locally-known pool would discard a usable draft and throw away an address
    Warmbly could have verified itself, so out-of-pool selections are reported
    for observability and passed through.
    """

    def test_address_outside_the_pool_still_drafts(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['outside_known_pool'] == ["ceo@happytails.example"]

    def test_mixed_selection_drafts_and_reports_only_the_unknown(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example", "ceo@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['outside_known_pool'] == ["ceo@happytails.example"]

    def test_all_known_selection_reports_an_empty_list(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["owner@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['outside_known_pool'] == []

    def test_empty_pool_still_drafts(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()):
            result = drafting_agent.fn(RESEARCH, [])

        assert result['status'] == 'drafted'
        assert result['outside_known_pool'] == ["owner@happytails.example"]

    def test_agent_may_ignore_the_researcher_suggestion(self):
        """Spec 2.1: not bound by the suggestion."""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(
                       selected_emails=["info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'
        assert result['outside_known_pool'] == []

    def test_multiple_pool_addresses_are_allowed(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[
                       "owner@happytails.example",
                       "info@happytails.example"])):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'drafted'


class TestLLMFailuresStillReject:
    """A model that cannot produce the schema is still a rejection."""

    def test_schema_failure_is_rejected(self):
        from flow.llm import LLMSchemaError

        with patch('flow.agents.drafting.complete_structured',
                   side_effect=LLMSchemaError("never validated")):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'llm_schema_failure'
