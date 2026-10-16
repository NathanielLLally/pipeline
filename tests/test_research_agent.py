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
        assert result['reason'] == 'llm_unavailable'

    def test_transport_failure_on_pass_one_is_llm_unavailable(self):
        """A 503 that outlasted the retries is the model being down, not
        the business failing: a distinct reason, so it is offered again."""
        from flow.llm import LLMTransportError

        with patch('flow.agents.research.complete_structured',
                   side_effect=LLMTransportError("503", status_code=503)):
            result = research_agent.fn(BUSINESS, EMAILS, "text")

        assert result['status'] == 'rejected'
        assert result['reason'] == 'llm_unavailable'


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
        # The page has to state the titles: an unstated one is nulled, and
        # with it the decision-maker flag (TestTitlesMustBeOnThePage).
        text = "Megan Fields, Head Trainer. Wyatt Smith, Owner."
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            research_agent.fn(BUSINESS, EMAILS, text)

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


def _checked(address, verified, error=None, is_role=None):
    entry = {"email": address, "verified_at": "2026-10-03T00:00:00Z",
             "source": "crawl", "verified": verified,
             "verification_error": error}
    if is_role is not None:
        entry["is_role"] = is_role
    return entry


def _pool_section(prompt):
    return prompt.split('Email pool', 1)[1].split('Site text:', 1)[0]


DEAD = _checked("hello@otherbiz.example", False,
                "5.1.1 The email account that you tried to reach does not exist.",
                is_role=True)
SHARED = _checked("info@happytails.example", False,
                  "[Net::SMTP] Connection closed", is_role=True)
PERSONAL = _checked("wyatt@happytails.example", False,
                    "false positive check failed for mx smtp.google.com",
                    is_role=False)


class TestUndeliverableAddressesAreDropped:
    """An address the email check proved dead never reaches a model.

    The 65 on a pool entry is how sure the scraper was that it read an
    address off the page -- not whether mail to it arrives. A 5.1.1
    'does not exist' address scored exactly the same as a live one, and was
    passed to research, drafting and import as if it were usable.
    """

    TEXT = "Write to info@happytails.example or hello@otherbiz.example."

    def test_research_prompt_omits_an_invalid_address(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9,
                                     suggested_email=SHARED['email'])) as llm:
            research_agent.fn(BUSINESS, [SHARED, DEAD], self.TEXT)

        # The page itself still prints the address; the pool must not.
        pool = _pool_section(llm.call_args.args[0])
        assert SHARED['email'] in pool
        assert DEAD['email'] not in pool

    def test_result_reports_it_as_undeliverable_not_in_the_pool(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9,
                                     suggested_email=SHARED['email'])):
            result = research_agent.fn(BUSINESS, [SHARED, DEAD], self.TEXT)

        assert [e['email'] for e in result['verified_emails']] == [
            SHARED['email']]
        assert result['undeliverable_emails'] == [DEAD['email']]

    def test_an_inconclusive_check_is_not_dropped(self):
        """Connection closed, catch-all: unknown, not dead."""
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9,
                                     suggested_email=SHARED['email'])):
            result = research_agent.fn(BUSINESS, [SHARED], "text")

        assert [e['email'] for e in result['verified_emails']] == [
            SHARED['email']]

    def test_a_pool_that_is_all_invalid_is_rejected_without_a_model_call(self):
        """Nothing left to write to: drafting would have to invent one."""
        with patch('flow.agents.research.complete_structured') as llm:
            result = research_agent.fn(BUSINESS, [DEAD], self.TEXT)

        llm.assert_not_called()
        assert result['status'] == 'rejected'
        assert result['reason'] == 'no_verified_emails'
        assert result['undeliverable_emails'] == [DEAD['email']]

    def test_the_prompt_shows_what_the_check_found(self):
        prompt = build_research_prompt(BUSINESS, [SHARED, PERSONAL], "text")

        assert 'Verified email pool' not in prompt
        assert 'catch_all' in prompt
        assert 'unknown' in prompt


class TestLivePoolKeepsNamedAndValidAddresses:
    """The page omitting an address is weak evidence it is retired.

    Home Dog Training's site printed a typo'd shared inbox and another
    business's address, and not wyatt@ -- the one personal address on their
    own domain, for a trainer the same pages name three times.
    """

    def test_a_personal_address_for_someone_the_page_names_is_kept(self):
        text = "Write to info@happytails.example. Charles & Wyatt teach."

        kept, excluded = live_pool([SHARED, PERSONAL], text)

        assert [e['email'] for e in kept] == [SHARED['email'], PERSONAL['email']]
        assert excluded == []

    def test_the_name_must_be_a_whole_word(self):
        text = "Write to info@happytails.example. Wyatton Park."

        kept, excluded = live_pool([SHARED, PERSONAL], text)

        assert excluded == [PERSONAL['email']]

    def test_a_role_address_is_not_rescued_by_its_word_appearing(self):
        """'info', 'training', 'hello' are on every page as plain words."""
        text = "hello@happytails.example. Get info about training here."
        hello = _checked("hello@happytails.example", None, is_role=True)

        kept, excluded = live_pool([hello, SHARED], text)

        assert excluded == [SHARED['email']]

    def test_an_address_the_mailbox_accepted_is_kept(self):
        valid = _checked("old@happytails.example", True, is_role=False)

        kept, excluded = live_pool(
            [SHARED, valid], "Write to info@happytails.example.")

        assert excluded == []

    def test_an_entry_without_role_information_is_not_rescued_by_name(self):
        """The legacy pool has no is_role; behave as before for it."""
        kept, excluded = live_pool(
            POOL, "Email owner@happytails.example. Old friends welcome.")

        assert "old@happytails.example" in excluded


class TestTitlesMustBeOnThePage:
    """'title exactly as the page states it, or null' -- enforced.

    The model gave Laura and Charles 'Co-owner / Trainer', which appears
    nowhere in the crawl, and the invented title marked both as decision
    makers and was written back to leads.businesses.
    """

    TEXT = ("George started everything for Laura and me. "
            "Wyatt, Lead Trainer, runs group class. "
            "Happy dog owners love us.")

    def _run(self, contacts, text=None):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            result = research_agent.fn(BUSINESS, EMAILS, text or self.TEXT)
        return result, record

    def test_an_unstated_title_is_nulled_with_its_decision_maker_flag(self):
        result, _ = self._run([{'name': 'Laura', 'title': 'Co-owner / Trainer',
                                'is_decision_maker': True}])

        contact = result['research']['contacts'][0]
        assert contact['name'] == 'Laura'
        assert contact['title'] is None
        assert contact['is_decision_maker'] is False

    def test_a_title_stated_beside_the_name_is_kept(self):
        result, _ = self._run([{'name': 'Wyatt', 'title': 'Lead Trainer'}])

        assert result['research']['contacts'][0]['title'] == 'Lead Trainer'

    def test_a_title_elsewhere_on_the_page_does_not_count(self):
        """'Owner' is on every pet site as 'dog owners'."""
        result, _ = self._run([{'name': 'Laura', 'title': 'Owner',
                                'is_decision_maker': True}])

        assert result['research']['contacts'][0]['title'] is None

    def test_matching_ignores_case_and_entities(self):
        text = "Charles &amp; Laura -- CO-OWNER’S NOTE"
        result, _ = self._run([{'name': 'Charles', 'title': "Co-owner's note"}],
                              text)

        assert result['research']['contacts'][0]['title'] == "Co-owner's note"

    def test_the_writeback_gets_the_enforced_title(self):
        _, record = self._run([{'name': 'Laura', 'title': 'Co-owner / Trainer',
                                'is_decision_maker': True}])

        record.assert_called_once()
        assert record.call_args.args[1:3] == ('Laura', None)


class TestDecisionMakerNeedsEvidenceNotATitle:
    """A family business rarely prints 'Owner' beside a name. Ownership can
    be shown in other words -- 'started everything for Laura and me' -- and
    a quote that names the person and is on the page is enough to keep them
    a decision maker. The title itself is still never invented."""

    TEXT = ("George is 13 now. The dog who quietly started everything for "
            "Laura and me. Charles & Wyatt will be leading all group classes.")

    def _contacts(self, contacts):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)), \
             patch('flow.agents.research.record_decision_maker_sync'):
            result = research_agent.fn(BUSINESS, EMAILS, self.TEXT)
        return result['research']['contacts']

    def test_a_quote_naming_them_keeps_the_flag_without_a_title(self):
        [laura] = self._contacts([{
            'name': 'Laura', 'title': 'Co-owner', 'is_decision_maker': True,
            'evidence': 'The dog who quietly started everything for Laura and me.'}])

        assert laura['is_decision_maker'] is True
        assert laura['title'] is None
        assert laura['evidence'].startswith('The dog who')

    def test_a_quote_not_on_the_page_does_not(self):
        [laura] = self._contacts([{
            'name': 'Laura', 'is_decision_maker': True,
            'evidence': 'Laura founded Home Dog Training in 2015.'}])

        assert laura['is_decision_maker'] is False
        assert laura['evidence'] is None

    def test_a_quote_that_does_not_name_them_does_not(self):
        [charles] = self._contacts([{
            'name': 'Charles', 'is_decision_maker': True,
            'evidence': 'The dog who quietly started everything for Laura and me.'}])

        assert charles['is_decision_maker'] is False

    def test_a_quote_that_only_shows_them_working_there_does_not(self):
        """Live replay: the model cited 'leading all group classes' to make
        a trainer a decision maker. Teaching is not owning."""
        [wyatt] = self._contacts([{
            'name': 'Wyatt', 'is_decision_maker': True,
            'evidence': 'Charles & Wyatt will be leading all group classes.'}])

        assert wyatt['is_decision_maker'] is False
        assert wyatt['evidence'] is not None

    def test_a_quote_that_drops_an_emoji_still_matches(self):
        """The site wrote 'fresh start. \U0001f305 We\u2019re now...'; the
        model quoted it without the emoji, and the quote was thrown out."""
        text = ("It\u2019s a fresh start. \U0001f305 We\u2019re training from "
                "home, which Laura and I started together.")
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=[{
                       'name': 'Laura', 'is_decision_maker': True,
                       'evidence': "It's a fresh start. We're training from "
                                   "home, which Laura and I started together."
                   }])), \
             patch('flow.agents.research.record_decision_maker_sync'):
            result = research_agent.fn(BUSINESS, EMAILS, text)

        assert result['research']['contacts'][0]['is_decision_maker'] is True

    def test_passages_joined_with_an_ellipsis_match_piece_by_piece(self):
        """Live replay: every quote stitched two passages with '...', so
        none was verbatim and all were thrown out."""
        [laura] = self._contacts([{
            'name': 'Laura', 'is_decision_maker': True,
            'evidence': 'George is 13 now. ... started everything for Laura '
                        'and me. \u2026 Charles & Wyatt will be leading'}])

        assert laura['is_decision_maker'] is True

    def test_one_invented_piece_rejects_the_whole_quote(self):
        [laura] = self._contacts([{
            'name': 'Laura', 'is_decision_maker': True,
            'evidence': 'started everything for Laura and me. ... '
                        'Laura owns the business outright.'}])

        assert laura['evidence'] is None
        assert laura['is_decision_maker'] is False

    def test_the_name_and_the_ownership_word_must_share_a_piece(self):
        """Otherwise 'Wyatt' from one passage and 'started' from another
        make a trainer an owner."""
        [wyatt] = self._contacts([{
            'name': 'Wyatt', 'is_decision_maker': True,
            'evidence': 'started everything for Laura and me. ... '
                        'Charles & Wyatt will be leading all group classes.'}])

        assert wyatt['is_decision_maker'] is False

    def test_the_prompt_asks_for_the_quote(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text")

        assert 'evidence' in prompt
        assert 'own or run the business' in prompt


ANGLE = {
    'problem': 'A click-fraud attack drained their ad budget and closed '
               'their Carrollton facility; they are rebuilding from home.',
    'evidence': 'After a criminal click-fraud attack drained our ad budget, '
                'we had to close our Carrollton training facility.',
    'caution': 'Paid ads burned them; do not lead with running ads.',
}
ANGLE_TEXT = ("Big news. After a criminal click-fraud attack drained our ad "
              "budget,   we had to close our Carrollton training facility.")


class TestWritebackDoesNotDowngradeAKnownName:
    """With titles enforced, most small sites yield no decision maker, and
    the primary contact falls back to whoever is listed first -- a trainer
    as easily as an owner. That guess must not replace a name the business
    already has."""

    KNOWN = dict(BUSINESS, decision_maker_name='Charles Smith')

    def _run(self, business, contacts, text="Laura and Wyatt teach."):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, contacts=contacts)), \
             patch('flow.agents.research.record_decision_maker_sync') as record:
            research_agent.fn(business, EMAILS, text)
        return record

    def test_no_decision_maker_and_a_known_name_writes_nothing(self):
        record = self._run(self.KNOWN, [{'name': 'Wyatt'}])

        record.assert_not_called()

    def test_no_decision_maker_and_no_known_name_still_writes(self):
        record = self._run(BUSINESS, [{'name': 'Wyatt'}])

        record.assert_called_once()

    def test_a_supported_decision_maker_replaces_a_known_name(self):
        record = self._run(self.KNOWN,
                           [{'name': 'Laura', 'title': 'Owner',
                             'is_decision_maker': True}],
                           text="Laura, Owner, and Wyatt teach.")

        assert record.call_args.args[1:3] == ('Laura', 'Owner')


class TestOutreachAngle:
    """Research names the one thing about *their business* the pitch should
    open on, quoted from the site, so drafting stops defaulting to their
    customers' problems."""

    def test_prompt_asks_for_an_outreach_angle(self):
        prompt = build_research_prompt(BUSINESS, EMAILS, "text")

        assert 'outreach_angle' in prompt
        assert 'caution' in prompt

    def test_the_caution_is_told_not_to_carry_payment_terms(self):
        """The caution said 'emphasize pay-per-qualified-lead with no upfront
        budget risk', and the draft repeated it. Customers pay upfront."""
        prompt = build_research_prompt(BUSINESS, EMAILS, "text").lower()

        assert 'what to avoid, not what to promise' in prompt
        assert 'never payment terms' in prompt

    def test_a_caution_carrying_payment_terms_is_dropped(self):
        """Told not to, the model still wrote 'pay-per-lead safety' into the
        caution, and the drafter copied it ('only pay') into the email."""
        angle = dict(ANGLE, caution='Burned by ads; stress pay-per-lead '
                                    'safety and no upfront risk.')
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=angle)):
            result = research_agent.fn(BUSINESS, EMAILS, ANGLE_TEXT)

        kept = result['research']['outreach_angle']
        assert kept['problem'] == ANGLE['problem']
        assert kept['caution'] is None

    def test_a_caution_warning_against_ads_and_guarantees_is_kept(self):
        """Live replay: 'do not ... guarantee ad results' was dropped. A
        caution exists to name what to avoid; only payment terms go."""
        angle = dict(ANGLE, caution='Victims of click fraud: do not pitch '
                                    'paid advertising, ad spend, or guarantee '
                                    'ad results.')
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=angle)):
            result = research_agent.fn(BUSINESS, EMAILS, ANGLE_TEXT)

        assert result['research']['outreach_angle']['caution'] == \
            angle['caution']

    def test_a_clean_caution_is_kept(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=ANGLE)):
            result = research_agent.fn(BUSINESS, EMAILS, ANGLE_TEXT)

        assert result['research']['outreach_angle']['caution'] == \
            ANGLE['caution']

    def test_the_schema_accepts_research_without_one(self):
        """Stored research and replay artifacts predate the field."""
        assert _out().outreach_angle is None

    def test_an_angle_quoted_from_the_page_is_kept(self):
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=ANGLE)):
            result = research_agent.fn(BUSINESS, EMAILS, ANGLE_TEXT)

        assert result['research']['outreach_angle']['problem'] == \
            ANGLE['problem']

    def test_an_angle_quote_joined_with_an_ellipsis_is_kept(self):
        angle = dict(ANGLE, evidence='Big news. ... After a criminal '
                                     'click-fraud attack drained our ad budget')
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=angle)):
            result = research_agent.fn(BUSINESS, EMAILS, ANGLE_TEXT)

        assert result['research']['outreach_angle'] is not None

    def test_an_angle_whose_quote_is_not_on_the_page_is_dropped(self):
        """An unsupported angle is an invented fact in the opening line."""
        with patch('flow.agents.research.complete_structured',
                   return_value=_out(confidence=0.9, outreach_angle=ANGLE)):
            result = research_agent.fn(BUSINESS, EMAILS, "Call us to book.")

        assert result['research']['outreach_angle'] is None
