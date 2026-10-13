import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.agents.research import (
    CONFIDENCE_THRESHOLD,
    build_research_prompt,
    live_pool,
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


class TestPromptStatesTheVocabulary:
    """A real run burned a retry on each of these.

    The model answered inferred_tone='professional, friendly, community-focused'
    (free text against a Literal) and evidence as a dict instead of a list.
    The validation-retry loop recovered both, but each recovery is an extra
    LLM call, and the prompt never stated the allowed values.
    """

    def test_prompt_enumerates_the_allowed_tones(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text")

        for tone in ('clinical', 'warm', 'premium', 'casual', 'sparse'):
            assert tone in prompt

    def test_prompt_says_exactly_one_tone(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text").lower()

        assert 'exactly one' in prompt

    def test_prompt_says_evidence_is_a_list_of_strings(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text").lower()

        assert 'list of' in prompt


class TestDecisionMakerWriteback:
    """A person found on the page is written back to leads.businesses.

    decision_maker_name exists for only 16% of Tier 1 and 5% of Tier 2, so
    research reading the live site is where most names will ever come from.
    Keeping the name only in the flow output wastes it.
    """

    def test_writes_back_an_extracted_person(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=[
                       {'name': 'Wyatt Smith', 'title': 'Owner',
                        'is_decision_maker': True}])), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            research_agent.fn(BUSINESS, EMAILS, "text")

        record.assert_called_once()
        args = record.call_args.args
        assert args[0] == BUSINESS['id']
        assert args[1] == 'Wyatt Smith'

    def test_no_person_means_no_write(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=[])), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            research_agent.fn(BUSINESS, EMAILS, "text")

        record.assert_not_called()

    def test_a_rejected_business_still_writes_back_a_found_person(self):
        """The name is true regardless of whether the hook was good enough."""
        low = _out(confidence=0.2, contacts=[{'name': 'Megan Fields'}],
                   next_url_to_check=None)
        with patch('flow.agents.research.complete_structured',
                   return_value=low), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        record.assert_called_once()

    def test_a_writeback_failure_does_not_fail_research(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9,
                                     contacts=[{'name': 'Wyatt Smith'}])), \
             patch('flow.agents.research.record_decision_maker_sync',
                   side_effect=RuntimeError('db down')):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'researched'

    def test_writes_back_the_decision_maker_not_the_first_name_listed(self):
        """leads.businesses has one decision_maker column; the owner gets it."""
        contacts = [
            {'name': 'Megan Fields', 'title': 'Head Trainer'},
            {'name': 'Wyatt Smith', 'title': 'Owner',
             'is_decision_maker': True},
        ]
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            research_agent.fn(BUSINESS, EMAILS, "text")

        record.assert_called_once()
        assert record.call_args.args[1:3] == ('Wyatt Smith', 'Owner')


def _emails(*addresses):
    return [{"email": a, "verified_at": "2026-10-03T00:00:00Z",
             "source": "leads.business_email"} for a in addresses]


POOL = _emails("owner@happytails.example", "info@happytails.example",
               "old@happytails.example")


class TestLivePool:
    """
    A verified address the site no longer shows is probably dead.

    Verification is a point-in-time MX check; the site is the current truth.
    When the page shows some of the pool, it is evidently listing its
    addresses, so the ones it omits are treated as gone. When it shows none,
    there is no signal either way and the whole pool stands.
    """

    def test_partial_match_keeps_only_the_addresses_on_the_page(self):
        kept, excluded = live_pool(
            POOL, "Email owner@happytails.example or info@happytails.example")

        assert [e['email'] for e in kept] == [
            "owner@happytails.example", "info@happytails.example"]
        assert excluded == ["old@happytails.example"]

    def test_no_match_keeps_the_whole_pool(self):
        kept, excluded = live_pool(POOL, "Call us to book.")

        assert kept == POOL
        assert excluded == []

    def test_full_match_excludes_nothing(self):
        text = ("owner@happytails.example info@happytails.example "
                "old@happytails.example")
        kept, excluded = live_pool(POOL, text)

        assert kept == POOL
        assert excluded == []

    def test_matching_ignores_case(self):
        kept, excluded = live_pool(POOL, "Write to OWNER@HappyTails.example")

        assert [e['email'] for e in kept] == ["owner@happytails.example"]

    def test_an_address_inside_a_longer_one_is_not_a_match(self):
        """'info@x' must not match inside 'myinfo@x'."""
        kept, excluded = live_pool(
            POOL, "myinfo@happytails.example and owner@happytails.example")

        assert [e['email'] for e in kept] == ["owner@happytails.example"]
        assert "info@happytails.example" in excluded

    def test_any_of_several_texts_counts(self):
        kept, _ = live_pool(POOL, "owner@happytails.example",
                            "info@happytails.example")

        assert {e['email'] for e in kept} == {
            "owner@happytails.example", "info@happytails.example"}

    def test_none_text_is_tolerated(self):
        kept, excluded = live_pool(POOL, None)

        assert kept == POOL
        assert excluded == []


class TestStaleEmailsAreExcluded:
    TEXT = "Contact owner@happytails.example to book."

    def test_prompt_shows_only_the_live_pool(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)) as llm:
            research_agent.fn(BUSINESS, POOL, self.TEXT)

        prompt = llm.call_args.args[0]
        assert "owner@happytails.example" in prompt
        assert "old@happytails.example" not in prompt
        assert "info@happytails.example" not in prompt

    def test_result_carries_the_live_pool_and_the_exclusions(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9)):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        assert [e['email'] for e in result['verified_emails']] == [
            "owner@happytails.example"]
        assert sorted(result['excluded_emails']) == [
            "info@happytails.example", "old@happytails.example"]

    def test_rejections_carry_the_live_pool_too(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.1, next_url_to_check=None)):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        assert result['status'] == 'rejected'
        assert [e['email'] for e in result['verified_emails']] == [
            "owner@happytails.example"]

    def test_a_stale_suggested_email_is_replaced_from_the_live_pool(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9,
                                     suggested_email="old@happytails.example")):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        assert result['research']['suggested_email'] == \
            "owner@happytails.example"

    def test_a_contact_email_that_is_stale_is_dropped(self):
        contacts = [{'name': 'Wyatt Smith', 'title': 'Owner',
                     'email': 'old@happytails.example',
                     'is_decision_maker': True}]
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        assert result['research']['contacts'][0]['email'] is None
        assert result['research']['contacts'][0]['name'] == 'Wyatt Smith'

    def test_a_contact_email_from_the_page_is_kept(self):
        """An address the page states is evidence, pool or not."""
        text = self.TEXT + " Trainer Megan: megan@happytails.example"
        contacts = [{'name': 'Megan Fields',
                     'email': 'megan@happytails.example'}]
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)):
            result = research_agent.fn(BUSINESS, POOL, text)

        assert result['research']['contacts'][0]['email'] == \
            'megan@happytails.example'

    def test_an_invented_contact_email_is_dropped(self):
        contacts = [{'name': 'Wyatt Smith',
                     'email': 'wyatt@happytails.example'}]
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        assert result['research']['contacts'][0]['email'] is None

    def test_pass_two_recomputes_the_pool_with_the_extra_page(self):
        """An address shown only on the escalation page is live too."""
        low = _out(confidence=0.3,
                   next_url_to_check="https://happytails.example/contact")
        with patch('flow.agents.research.complete_structured',
                   side_effect=[low, _out(confidence=0.9)]) as llm, \
             patch('flow.agents.research.fetch_html',
                   return_value="info@happytails.example"):
            result = research_agent.fn(BUSINESS, POOL, self.TEXT)

        second_prompt = llm.call_args_list[1].args[0]
        assert "info@happytails.example" in second_prompt
        assert "old@happytails.example" not in second_prompt
        assert result['excluded_emails'] == ["old@happytails.example"]


class TestPromptAsksForEveryPerson:
    def test_prompt_asks_for_contacts_and_decision_makers(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text")

        assert 'contacts' in prompt
        assert 'is_decision_maker' in prompt

    def test_prompt_still_forbids_inventing_a_person(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text").lower()

        assert 'rather than inventing' in prompt
