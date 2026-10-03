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
