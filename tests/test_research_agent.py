from unittest.mock import patch

import pytest

from flow.agents.research import (
    CONFIDENCE_THRESHOLD,
    build_research_prompt,
    research_agent,
)
from flow.schemas import ResearchOutput

BUSINESS = {"id": 1, "business_name": "Happy Tails",
            "website": "https://happytails.example"}
EMAILS = [{"email": "owner@happytails.example", "verified_at":
           "2026-10-03T00:00:00Z", "source": "leads.business_email"}]


def _out(**over):
    base = dict(
        business_name="Happy Tails", pain_signals=["no online booking"],
        personalization_hook="phone only", inferred_tone="warm",
        confidence=0.9, evidence=["Call us to book"],
        suggested_email="owner@happytails.example",
    )
    base.update(over)
    return ResearchOutput(**base)


class TestPromptConstruction:
    def test_prompt_names_the_business_and_the_verified_pool(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "some crawl text")

        assert "Happy Tails" in prompt
        assert "owner@happytails.example" in prompt
        assert "some crawl text" in prompt


class TestConfidentFirstPass:
    def test_high_confidence_returns_without_escalating(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)) as llm:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert result['passes'] == 1
        assert llm.call_count == 1
