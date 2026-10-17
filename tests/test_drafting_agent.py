import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


class TestSignatureInstruction:
    """The sending system appends the signature, so the drafter must end on
    the ask: no closing, no name. Told nothing, it signed 'Best,\nNate'."""

    def test_the_prompt_says_a_signature_is_appended(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS, "default").lower()
        assert 'signature' in prompt
        assert 'no sign-off' in prompt
        assert 'no name' in prompt

    def test_the_system_prompt_says_it_too(self):
        from flow.agents.drafting import SYSTEM_PROMPT
        assert 'signature' in SYSTEM_PROMPT.lower()


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

    def test_transport_failure_is_llm_unavailable(self):
        from flow.llm import LLMTransportError

        with patch('flow.agents.drafting.complete_structured',
                   side_effect=LLMTransportError("503", status_code=503)):
            result = drafting_agent.fn(RESEARCH, EMAILS)

        assert result['status'] == 'rejected'
        assert result['reason'] == 'llm_unavailable'


class TestOfferContext:
    """Without the offer, the agent invents what we sell -- and gets it wrong.

    The first live run produced a content-marketing pitch because neither
    prompt said what the offer was. Spec 2.1 lists offer context as an input
    to this stage.
    """

    def test_prompt_states_the_offer(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS, 'default',
                                       offer='we send you live dog-owner leads')

        assert 'we send you live dog-owner leads' in prompt

    def test_prompt_states_the_goal_is_an_interest_reply(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS, 'default',
                                       offer='x')

        assert 'reply' in prompt.lower()

    def test_falls_back_to_the_configured_default_offer(self):
        from flow.offer import DEFAULT_OFFER

        prompt = build_drafting_prompt(RESEARCH, EMAILS)

        assert DEFAULT_OFFER in prompt

    def test_prompt_says_it_is_a_cold_email(self):
        prompt = build_drafting_prompt(RESEARCH, EMAILS)

        assert 'cold email' in prompt.lower()

    def test_cold_email_framing_survives_an_offer_override(self):
        """A second vertical overrides the offer, never the channel."""
        prompt = build_drafting_prompt(RESEARCH, EMAILS,
                                       offer='we send you roofing leads')

        assert 'cold email' in prompt.lower()

    def test_goal_opens_with_their_problem_not_with_us(self):
        from flow.offer import OUTREACH_GOAL

        assert 'open with a problem they already have' in OUTREACH_GOAL.lower()

    def test_offer_and_goal_carry_no_placeholder_for_the_model_to_echo(self):
        """An echoed '[City]' gets the whole draft rejected."""
        from flow.offer import DEFAULT_OFFER, OUTREACH_GOAL
        from flow.schemas import find_placeholder

        assert find_placeholder(OUTREACH_GOAL) is None
        assert find_placeholder(DEFAULT_OFFER) is None

    def test_prompt_names_the_wire_field_email_text(self):
        """The prompt must name the field the schema asks for, not `body`."""
        prompt = build_drafting_prompt(RESEARCH, EMAILS)

        assert 'email_text' in prompt
        assert 'subject, body' not in prompt


class TestJSONArtifactInput:
    """Test loading drafting_agent inputs from JSON files."""

    def test_loads_from_json_input_file(self):
        """Loads all params from a JSON artifact when json_input_file is provided"""
        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "biz-1-drafting-input.json"
            input_data = {
                "research": RESEARCH,
                "verified_emails": EMAILS,
                "template_slug": "default",
                "offer": None,
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.drafting.complete_structured',
                       return_value=_draft()), \
                 patch('flow.agents.drafting.write_artifact'):
                result = drafting_agent.fn(json_input_file=str(input_file))

            assert result['status'] == 'drafted'

    def test_raises_when_json_input_file_missing(self):
        """Raises FileNotFoundError if json_input_file does not exist"""
        with pytest.raises(FileNotFoundError):
            drafting_agent.fn(json_input_file="/nonexistent/file.json")

    def test_writes_input_artifact(self):
        """Writes input artifact when dict params are provided"""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact') as mock_write:
            drafting_agent.fn(RESEARCH, EMAILS)

            # Should be called twice: once for input, once for output
            assert mock_write.call_count == 2
            input_call = mock_write.call_args_list[0]
            assert input_call.kwargs['suffix'] == 'input'

    def test_writes_output_artifact(self):
        """Writes output artifact after successful drafting"""
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact') as mock_write:
            drafting_agent.fn(RESEARCH, EMAILS)

            output_call = mock_write.call_args_list[1]
            assert output_call.kwargs['suffix'] == 'output'
            data = output_call.args[0]
            assert data['status'] == 'drafted'

    def test_requires_either_dict_params_or_json_file(self):
        """Raises ValueError if neither dict params nor json_input_file provided"""
        with pytest.raises(ValueError, match='Either json_input_file or'):
            drafting_agent.fn()

    def test_json_input_takes_precedence_over_dict_params(self):
        """json_input_file overrides dict params when both provided"""
        with TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "test-input.json"
            json_research = dict(RESEARCH, business_id='json-biz')
            input_data = {
                "research": json_research,
                "verified_emails": EMAILS,
                "template_slug": "json-template",
                "offer": "json offer",
            }
            input_file.write_text(json.dumps(input_data))

            with patch('flow.agents.drafting.complete_structured',
                       return_value=_draft()), \
                 patch('flow.agents.drafting.write_artifact') as mock_write:
                # Pass both: json_input_file should win
                drafting_agent.fn(
                    research=RESEARCH,
                    verified_emails=EMAILS,
                    json_input_file=str(input_file)
                )

            # Check that the input artifact written was from the JSON file
            input_call = mock_write.call_args_list[0]
            data = input_call.args[0]
            assert data['template_slug'] == 'json-template'


CONTACTS = [
    {'name': 'Laura', 'title': None, 'email': None, 'is_decision_maker': False},
    {'name': 'Charles', 'title': 'Owner', 'email': None,
     'is_decision_maker': True},
    {'name': 'Wyatt', 'title': 'Trainer', 'email': 'wyatt@happytails.example',
     'is_decision_maker': False},
]


class TestDrafterSeesThePeople:
    """Research found the owners; the drafter was never told.

    build_drafting_prompt read hook, tone, pain and evidence but not
    contacts, so the 2026-10-14 Home Dog Training draft greeted nobody
    although research had named Laura and Charles.
    """

    def test_prompt_lists_every_named_person(self):
        prompt = build_drafting_prompt(dict(RESEARCH, contacts=CONTACTS), EMAILS)

        for name in ('Laura', 'Charles', 'Wyatt'):
            assert name in prompt

    def test_prompt_marks_who_decides_and_what_title_the_page_gives(self):
        prompt = build_drafting_prompt(dict(RESEARCH, contacts=CONTACTS), EMAILS)

        assert 'Charles (Owner; decides on spending)' in prompt
        assert 'Laura (no title stated)' in prompt
        assert 'wyatt@happytails.example' in prompt

    def test_prompt_says_who_to_greet(self):
        prompt = build_drafting_prompt(dict(RESEARCH, contacts=CONTACTS),
                                       EMAILS).lower()

        assert 'greet' in prompt
        assert 'first name' in prompt

    def test_no_contacts_says_so_and_asks_for_no_name(self):
        prompt = build_drafting_prompt(dict(RESEARCH, contacts=[]), EMAILS)

        assert 'People named on their site: none' in prompt

    def test_legacy_research_without_contacts_still_builds(self):
        assert 'none' in build_drafting_prompt(RESEARCH, EMAILS)


class TestDrafterSeesWhatTheEmailCheckFound:
    POOL = [
        {'email': 'info@happytails.example', 'verified': False,
         'verification_error': '[Net::SMTP] Connection closed', 'is_role': True},
        {'email': 'wyatt@happytails.example', 'verified': False,
         'verification_error': 'false positive check failed for mx x',
         'is_role': False},
    ]

    def test_pool_is_no_longer_called_verified(self):
        prompt = build_drafting_prompt(RESEARCH, self.POOL)

        assert 'Verified addresses' not in prompt

    def test_each_address_carries_its_status(self):
        prompt = build_drafting_prompt(RESEARCH, self.POOL)

        assert 'info@happytails.example' in prompt
        assert 'unknown' in prompt
        assert 'catch_all' in prompt
        assert 'shared inbox' in prompt


ANGLE = {
    'problem': 'A click-fraud attack drained their ad budget and closed '
               'their Carrollton facility.',
    'evidence': 'After a criminal click-fraud attack drained our ad budget',
    'caution': 'Paid ads burned them; do not lead with running ads.',
}


class TestGoalIsBuiltFromTheResearch:
    """With an outreach_angle the opening is theirs, not a generic chain."""

    def test_prompt_opens_on_the_angle(self):
        prompt = build_drafting_prompt(dict(RESEARCH, outreach_angle=ANGLE),
                                       EMAILS)

        assert ANGLE['problem'] in prompt
        assert ANGLE['caution'] in prompt

    def test_the_hook_is_not_a_second_competing_steer(self):
        prompt = build_drafting_prompt(dict(RESEARCH, outreach_angle=ANGLE),
                                       EMAILS)

        assert 'Why this matters to them' not in prompt

    def test_without_an_angle_the_hook_and_default_goal_stay(self):
        from flow.offer import OUTREACH_GOAL

        prompt = build_drafting_prompt(RESEARCH, EMAILS)

        assert 'Why this matters to them: phone only' in prompt
        assert OUTREACH_GOAL in prompt


class TestOffDomainNoteNeedsTheWebsite:
    POOL = [{'email': 'wyatt@homedogtrainingllc.com', 'verified': None},
            {'email': 'hello@k9culture.example', 'verified': None}]

    def test_with_the_website_only_the_foreign_address_is_flagged(self):
        prompt = build_drafting_prompt(
            RESEARCH, self.POOL, website='https://www.homedogtrainingllc.com/')
        # The recipient has its own 'Write to:' line; the rest are listed.
        lines = {a: next(l for l in prompt.splitlines() if a + ' (' in l)
                 for a in ('hello@k9culture.example',
                           'wyatt@homedogtrainingllc.com')}

        assert "not on their website's domain" in lines['hello@k9culture.example']
        assert "not on their website's domain" not in \
            lines['wyatt@homedogtrainingllc.com']

    def test_without_the_website_nothing_is_flagged(self):
        """Guessing the domain from the suggested address flags the wrong one."""
        prompt = build_drafting_prompt(RESEARCH, self.POOL)

        assert "not on their website's domain" not in prompt

    def test_the_website_is_kept_in_the_input_artifact_for_replay(self):
        with TemporaryDirectory() as tmp, \
             patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact') as write:
            drafting_agent.fn(RESEARCH, EMAILS, website='https://x.example')

        assert write.call_args_list[0].args[0]['website'] == 'https://x.example'


class TestOutputVariableIsPerBusiness:
    """Every business wrote drafting_agent_output_unknown: ResearchOutput
    has no business_id, which is where the key came from."""

    def _stored_under(self, **kwargs):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact'), \
             patch('flow.agents.drafting._set_drafting_output',
                   side_effect=lambda r, b: r) as store:
            drafting_agent.fn(RESEARCH, EMAILS, **kwargs)
        return store.call_args.args[1]

    def test_keyed_by_the_business_id_it_is_given(self):
        assert self._stored_under(business_id='biz-1') == 'biz-1'

    def test_research_carrying_an_id_still_works(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact'), \
             patch('flow.agents.drafting._set_drafting_output',
                   side_effect=lambda r, b: r) as store:
            drafting_agent.fn(dict(RESEARCH, business_id='biz-9'), EMAILS)

        assert store.call_args.args[1] == 'biz-9'

    def test_a_failed_draft_is_keyed_by_it_too(self):
        from flow.llm import LLMSchemaError

        with patch('flow.agents.drafting.complete_structured',
                   side_effect=LLMSchemaError('x')), \
             patch('flow.agents.drafting.write_artifact'), \
             patch('flow.agents.drafting._set_drafting_output',
                   side_effect=lambda r, b: r) as store:
            drafting_agent.fn(RESEARCH, EMAILS, business_id='biz-1')

        assert store.call_args.args[1] == 'biz-1'

    def test_the_id_is_kept_in_the_input_artifact_for_replay(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft()), \
             patch('flow.agents.drafting.write_artifact') as write:
            drafting_agent.fn(RESEARCH, EMAILS, business_id='biz-1')

        assert write.call_args_list[0].args[0]['business_id'] == 'biz-1'


SHARED_INBOX = {'email': 'training@homedogtraining.com', 'is_role': True,
                'verified': False,
                'verification_error': '[Net::SMTP] Connection closed'}
WYATTS = {'email': 'wyatt@homedogtrainingllc.com', 'is_role': False,
          'verified': False,
          'verification_error': 'false positive check failed for mx x'}
OWNERS = [{'name': 'Laura', 'title': None, 'email': None,
           'is_decision_maker': True},
          {'name': 'Wyatt', 'title': None, 'email': None,
           'is_decision_maker': False}]


class TestRecipientIsChosenInCode:
    """The prompt rule 'decision maker, then shared inbox, then anyone else'
    was ignored: with the owners known, the replay wrote to a trainer."""

    RESEARCH = dict(RESEARCH, contacts=OWNERS,
                    suggested_email='wyatt@homedogtrainingllc.com')
    POOL = [WYATTS, SHARED_INBOX]

    def test_prompt_names_the_recipient_and_who_to_greet(self):
        prompt = build_drafting_prompt(self.RESEARCH, self.POOL)

        assert 'Write to: training@homedogtraining.com' in prompt
        assert 'Greet Laura by first name' in prompt

    def test_no_decision_maker_on_a_shared_inbox_greets_nobody(self):
        research = dict(self.RESEARCH, contacts=[OWNERS[1]])

        prompt = build_drafting_prompt(research, self.POOL)

        assert 'Greet no one by name' in prompt

    def test_the_researcher_suggestion_is_not_a_competing_steer(self):
        prompt = build_drafting_prompt(self.RESEARCH, self.POOL)

        assert 'Researcher suggested' not in prompt

    def test_a_lower_ranked_pick_is_replaced_by_the_recipient(self):
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[WYATTS['email']])):
            result = drafting_agent.fn(self.RESEARCH, self.POOL)

        assert result['draft']['selected_emails'] == [SHARED_INBOX['email']]

    def test_an_equally_ranked_pick_is_kept(self):
        other = dict(SHARED_INBOX, email='info@homedogtraining.com')
        with patch('flow.agents.drafting.complete_structured',
                   return_value=_draft(selected_emails=[other['email']])):
            result = drafting_agent.fn(self.RESEARCH, [SHARED_INBOX, other])

        assert result['draft']['selected_emails'] == [other['email']]
