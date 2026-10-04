import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class TestThresholdBoundary:
    def test_confidence_exactly_at_threshold_passes(self):
        """Spec 3b says 'confidence >= threshold'. Equality must pass.

        Getting this backwards sends every borderline business to the
        rejection pool silently.
        """
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=CONFIDENCE_THRESHOLD)) as llm:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert llm.call_count == 1

    def test_confidence_just_below_threshold_escalates(self):
        low = _out(confidence=CONFIDENCE_THRESHOLD - 0.01,
                   next_url_to_check="https://happytails.example/about")
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low, _out(confidence=0.95)]), \
             patch('flow.agents.research.fetch_html', return_value="about text"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'
        assert result['passes'] == 2

    def test_still_low_after_two_passes_is_rejected(self):
        low1 = _out(confidence=0.3,
                    next_url_to_check="https://happytails.example/about")
        low2 = _out(confidence=0.4)
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low1, low2]), \
             patch('flow.agents.research.fetch_html', return_value="more text"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'low_confidence_after_two_passes'
        assert result['passes'] == 2
        # The research is kept: the rejection pool needs it (spec 2.2).
        assert result['research']['confidence'] == 0.4

    def test_low_confidence_without_a_url_rejects_without_refetching(self):
        """No URL means nothing to escalate to.

        Re-running pass 1 unchanged would cost a second call for an identical
        prompt, and looping would cost unboundedly.
        """
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.2,
                                     next_url_to_check=None)) as llm, \
             patch('flow.agents.research.fetch_html') as fetch:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'low_confidence_no_escalation_url'
        assert llm.call_count == 1
        fetch.assert_not_called()


class TestLLMFailures:
    def test_schema_failure_on_pass_one_is_a_rejection_not_a_crash(self):
        from flow.llm import LLMSchemaError

        with patch('flow.agents.research.complete_structured',
                   side_effect=LLMSchemaError("never validated")):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'llm_schema_failure'

    def test_transport_failure_on_pass_two_keeps_pass_one_research(self):
        from flow.llm import LLMTransportError

        low = _out(confidence=0.3,
                   next_url_to_check="https://happytails.example/about")
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low, LLMTransportError("proxy down")]), \
             patch('flow.agents.research.fetch_html', return_value="t"):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['research']['confidence'] == 0.3


class TestJSONArtifactInput:
    """Test loading research_agent inputs from JSON files."""

    def test_loads_from_json_input_file(self):
        """Loads all params from a JSON artifact when json_input_file is provided"""
        import json

        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "biz-1-research-input.json"
            input_data = {
                "business": BUSINESS,
                "verified_emails": EMAILS,
                "crawl_excerpt": "training text",
                "offer": "dog training leads",
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.research.complete_structured',
                       return_value=_out(confidence=0.9)), \
                 patch('flow.agents.research.write_artifact'):
                result = research_agent.fn(json_input_file=str(input_file))

            assert result['status'] == 'researched'
            assert result['passes'] == 1

    def test_raises_when_json_input_file_missing(self):
        """Raises FileNotFoundError if json_input_file does not exist"""
        with pytest.raises(FileNotFoundError):
            research_agent.fn(json_input_file="/nonexistent/file.json")

    def test_writes_input_artifact(self):
        """Writes input artifact when dict params are provided"""
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)), \
             patch('flow.agents.research.write_artifact') as mock_write:
            research_agent.fn(BUSINESS, EMAILS, "text")

            # Should be called twice: once for input, once for output
            assert mock_write.call_count == 2
            input_call = mock_write.call_args_list[0]
            assert input_call.kwargs['suffix'] == 'input'

    def test_writes_output_artifact(self):
        """Writes output artifact after successful research"""
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)), \
             patch('flow.agents.research.write_artifact') as mock_write:
            research_agent.fn(BUSINESS, EMAILS, "text")

            output_call = mock_write.call_args_list[1]
            assert output_call.kwargs['suffix'] == 'output'
            data = output_call.args[0]
            assert data['status'] == 'researched'

    def test_requires_either_dict_params_or_json_file(self):
        """Raises ValueError if neither dict params nor json_input_file provided"""
        with pytest.raises(ValueError, match='Either json_input_file or'):
            research_agent.fn()

    def test_json_input_takes_precedence_over_dict_params(self):
        """json_input_file overrides dict params when both provided"""
        import json

        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "test-input.json"
            input_data = {
                "business": {"id": "json-biz", "business_name": "JSON Business"},
                "verified_emails": [{"email": "json@example.com", "verified_at": "2026-10-04T00:00:00Z", "source": "json"}],
                "crawl_excerpt": "json text",
                "offer": None,
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.research.complete_structured',
                       return_value=_out(confidence=0.9)), \
                 patch('flow.agents.research.write_artifact') as mock_write:
                # Pass both: json_input_file should win
                research_agent.fn(
                    business=BUSINESS,
                    verified_emails=EMAILS,
                    json_input_file=str(input_file)
                )

            # Check that the input artifact written was from the JSON file, not the dict params
            input_call = mock_write.call_args_list[0]
            data = input_call.args[0]
            assert data['business']['id'] == 'json-biz'
