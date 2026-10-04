"""
Unit tests for Prefect variables with structured JSON data.

Variables are the mechanism for agents to persist their output across flows and
deployments. Each agent (research, drafting) stores its output as a keyed
variable: 'research-agent-output-{business_id}' or 'drafting-agent-output-{business_id}'.

This test suite covers:
- Setting and retrieving structured JSON data (research output, draft output)
- Variable lifecycle (set, get, delete, overwrite)
- Graceful handling when variable storage is unavailable
- Pretty-printing and data integrity of complex nested structures
"""

import json
from datetime import datetime
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from prefect.variables import Variable
from flow.schemas import ResearchOutput, DraftingOutput, VerifiedEmail


# Mock data scaffolded from the schemas

# Dumped with mode='json' deliberately -- see TestDatetimeRoundTrip for why a
# raw datetime must never be handed to Variable.set.
VERIFIED_EMAIL = VerifiedEmail(
    email='owner@happytails.example',
    verified_at=datetime(2026, 10, 4, 12, 30),
    source='website',
    mx_server='mail.happytails.example',
).model_dump(mode='json')

RESEARCH_OUTPUT = {
    'business_name': 'Happy Tails Dog Training',
    'contact_name': 'Sarah Johnson',
    'contact_title': 'Owner',
    'pain_signals': [
        'no online booking',
        'no lead form',
        'calls only inquiry method',
    ],
    'personalization_hook': 'board-and-train specialized in reactive dogs',
    'inferred_tone': 'warm',
    'confidence': 0.92,
    'evidence': [
        'homepage emphasizes "reactive dog specialists"',
        'website shows appointment booking by phone only',
        'no online inquiry form visible',
    ],
    'suggested_email': 'sarah@happytails.example',
    'next_url_to_check': None,
}

DRAFT_OUTPUT = {
    'selected_emails': [
        'sarah@happytails.example',
        'training@happytails.example',
    ],
    'subject': 'Ready-to-train dog leads for Happy Tails',
    'body': (
        'Hi Sarah,\n\n'
        'I noticed Happy Tails specializes in reactive dog training. '
        'We source board-and-train ready dog-owner leads that are actively seeking '
        'behavior modification training.\n\n'
        'Would you like to discuss further?\n\n'
        'Best,\nLeads Team'
    ),
    'rationale': (
        'warm tone matches brand; board-and-train + reactive specialization = '
        'high ICP fit; no online booking = customer acquisition need'
    ),
}

FLOW_RESULT = {
    'status': 'researched',
    'research': RESEARCH_OUTPUT,
    'passes': 1,
}

DRAFT_RESULT = {
    'status': 'drafted',
    'draft': DRAFT_OUTPUT,
    'outside_known_pool': [],
}


def _pretty_json(data: dict) -> str:
    """
    Pretty-print a variable payload.

    No `default=` hook on purpose: anything that json.dumps cannot serialize
    here is also something Variable.set would mangle rather than reject (a
    datetime silently becomes a float timestamp), so raising on it is the
    useful behavior. Callers dump models with `model_dump(mode='json')`.
    """
    return json.dumps(data, indent=2)


class TestBasicVariableLIFecycle:
    """Test setting, getting, and deleting simple and complex variables."""

    def test_set_and_get_string_variable(self):
        """Simple string variables persist and retrieve."""
        Variable.set("test_string", "hello_world", overwrite=True)

        value = Variable.get("test_string")

        assert value == "hello_world"

    def test_get_with_fallback_default(self):
        """Missing variables return the provided default."""
        value = Variable.get("nonexistent_key", default="fallback_value")

        assert value == "fallback_value"

    def test_overwrite_flag_updates_existing(self):
        """Setting with overwrite=True replaces the old value."""
        Variable.set("my_key", "first_value", overwrite=True)
        Variable.set("my_key", "second_value", overwrite=True)

        value = Variable.get("my_key")

        assert value == "second_value"

    def test_delete_variable(self):
        """Variables can be deleted and subsequent gets return default."""
        Variable.set("to_delete", "temporary", overwrite=True)
        Variable.unset("to_delete")

        value = Variable.get("to_delete", default="gone")

        assert value == "gone"


class TestStructuredJSONVariables:
    """Test setting and retrieving structured data (agent outputs)."""

    def test_set_research_output_variable(self):
        """Research agent output can be stored as a variable."""
        var_name = "research_agent_output_biz_1"
        Variable.set(var_name, FLOW_RESULT, overwrite=True)

        retrieved = Variable.get(var_name)

        assert retrieved['status'] == 'researched'
        assert retrieved['research']['business_name'] == 'Happy Tails Dog Training'
        assert retrieved['research']['confidence'] == 0.92

    def test_set_draft_output_variable(self):
        """Drafting agent output can be stored as a variable."""
        var_name = "drafting_agent_output_biz_1"
        Variable.set(var_name, DRAFT_RESULT, overwrite=True)

        retrieved = Variable.get(var_name)

        assert retrieved['status'] == 'drafted'
        assert len(retrieved['draft']['selected_emails']) == 2
        assert 'subject' in retrieved['draft']

    def test_nested_lists_and_dicts_preserved(self):
        """Complex nested structures (lists, dicts) remain intact."""
        var_name = "complex_output_biz_1"
        complex_data = {
            'outcomes': [
                {
                    'business_id': 'b1',
                    'research_status': 'researched',
                    'draft': {
                        'subject': 'Test',
                        'body': 'Test body',
                        'selected_emails': ['a@b.com', 'c@d.com'],
                    },
                },
                {
                    'business_id': 'b2',
                    'research_status': 'rejected',
                    'rejection_reason': 'low_confidence_after_two_passes',
                },
            ],
            'counts': {'researched': 1, 'rejected': 1, 'drafted': 1},
        }

        Variable.set(var_name, complex_data, overwrite=True)
        retrieved = Variable.get(var_name)

        assert len(retrieved['outcomes']) == 2
        assert retrieved['outcomes'][0]['draft']['subject'] == 'Test'
        assert retrieved['counts']['researched'] == 1

    def test_variable_naming_by_business_id(self):
        """Variables are keyed by business_id for easy retrieval."""
        business_ids = ['biz_alpha', 'biz_beta', 'biz_gamma']

        for bid in business_ids:
            var_name = f"research_agent_output_{bid}"
            output = dict(FLOW_RESULT, research={
                **FLOW_RESULT['research'],
                'business_name': f'Business {bid}',
            })
            Variable.set(var_name, output, overwrite=True)

        # Retrieve and verify each one independently
        for bid in business_ids:
            var_name = f"research_agent_output_{bid}"
            retrieved = Variable.get(var_name)
            assert f'Business {bid}' in retrieved['research']['business_name']


class TestErrorHandlingAndEdgeCases:
    """Test graceful handling of missing variables and edge cases."""

    def test_variable_storage_failure_is_graceful(self):
        """If variable storage fails, the agent should still return output."""
        with patch('prefect.variables.Variable.set') as mock_set:
            mock_set.side_effect = Exception("Storage unavailable")

            # Simulate the agent's error handling pattern
            var_name = "research-agent-output-biz-1"
            try:
                Variable.set(var_name, FLOW_RESULT, overwrite=True)
                assert False, "Should have raised"
            except Exception as e:
                # The agent catches this and continues
                assert "Storage unavailable" in str(e)
                # The output is still returned to the caller
                assert FLOW_RESULT['status'] == 'researched'

    def test_empty_variable_name_raises(self):
        """Empty variable names are rejected."""
        with pytest.raises(Exception):
            Variable.set("", "value", overwrite=True)

    def test_special_characters_in_variable_name(self):
        """Variable names with underscores work."""
        var_name = "research_agent_output_biz_1_test_v2"
        Variable.set(var_name, FLOW_RESULT, overwrite=True)

        retrieved = Variable.get(var_name)
        assert retrieved['status'] == 'researched'


class TestDatetimeRoundTrip:
    """
    Variable storage is JSON, so datetimes need dumping before they go in.

    Verified against the local Prefect server, not assumed: a raw datetime is
    accepted and comes back a float, which is the dangerous case -- nothing
    raises, the type just quietly changes under you.
    """

    def test_a_raw_datetime_comes_back_as_a_float(self):
        Variable.set('probe_raw_datetime',
                     {'verified_at': datetime(2026, 10, 4, 12, 30)},
                     overwrite=True)

        retrieved = Variable.get('probe_raw_datetime')

        assert isinstance(retrieved['verified_at'], float)

    def test_a_json_dumped_model_round_trips_intact(self):
        Variable.set('probe_verified_email', VERIFIED_EMAIL, overwrite=True)

        retrieved = Variable.get('probe_verified_email')

        assert retrieved['verified_at'] == '2026-10-04T12:30:00'
        assert VerifiedEmail.model_validate(retrieved).verified_at \
            == datetime(2026, 10, 4, 12, 30)

    def test_pretty_print_refuses_an_undumped_datetime(self):
        """Better a TypeError in a test than a float in the database."""
        with pytest.raises(TypeError):
            _pretty_json({'verified_at': datetime(2026, 10, 4, 12, 30)})


class TestPrettyPrintedOutput:
    """Test formatting of structured data for logging and debugging."""

    def test_research_output_pretty_print(self):
        """Research output is readable when pretty-printed."""
        output_str = _pretty_json(FLOW_RESULT)

        print("Research Output (Pretty-Printed):")
        print(output_str)

        # Verify the JSON can be parsed back
        parsed = json.loads(output_str)
        assert parsed['research']['business_name'] == 'Happy Tails Dog Training'
        assert len(parsed['research']['pain_signals']) == 3

    def test_draft_output_pretty_print(self):
        """Draft output is readable when pretty-printed."""
        output_str = _pretty_json(DRAFT_RESULT)

        print("Draft Output (Pretty-Printed):")
        print(output_str)

        parsed = json.loads(output_str)
        assert len(parsed['draft']['selected_emails']) == 2
        assert 'subject' in parsed['draft']

    def test_complex_nested_structure_pretty_print(self):
        """Complex nested structures are human-readable."""
        run_result = {
            'selected': 2,
            'researched': 1,
            'rejected': 1,
            'drafted': 1,
            'errored': 0,
            'outcomes': [
                {
                    'business_id': 'biz-1',
                    'business_name': 'Happy Tails',
                    'research_status': 'researched',
                    'draft_status': 'drafted',
                    'draft': DRAFT_OUTPUT,
                },
                {
                    'business_id': 'biz-2',
                    'business_name': 'Paws & Treats',
                    'research_status': 'rejected',
                    'draft_status': None,
                    'rejection_reason': 'low_confidence_after_two_passes',
                },
            ],
        }

        output_str = _pretty_json(run_result)

        print("Full Pipeline Result (Pretty-Printed):")
        print(output_str)

        # Verify structure and key fields
        parsed = json.loads(output_str)
        assert parsed['selected'] == 2
        assert parsed['researched'] == 1
        assert parsed['rejected'] == 1
        assert len(parsed['outcomes']) == 2
        assert parsed['outcomes'][0]['draft_status'] == 'drafted'
        assert parsed['outcomes'][1]['rejection_reason'] == 'low_confidence_after_two_passes'


class TestSchemaValidation:
    """Test that stored data matches schema expectations."""

    def test_research_output_survives_a_roundtrip_as_the_model(self):
        """
        The round-tripped dict re-validates as ResearchOutput.

        Asserting through the model rather than on dict keys is the point: it
        catches a field the storage layer coerced or dropped, which a
        `'confidence' in data` check would pass straight over.
        """
        var_name = "research_agent_output_biz_1"
        Variable.set(var_name, FLOW_RESULT, overwrite=True)

        retrieved = Variable.get(var_name)

        research = ResearchOutput.model_validate(retrieved['research'])
        assert research.business_name == 'Happy Tails Dog Training'
        assert research.confidence == 0.92
        assert research.inferred_tone == 'warm'
        assert research.pain_signals == RESEARCH_OUTPUT['pain_signals']

    def test_draft_output_survives_a_roundtrip_as_the_model(self):
        """The round-tripped dict re-validates as DraftingOutput."""
        var_name = "drafting_agent_output_biz_1"
        Variable.set(var_name, DRAFT_RESULT, overwrite=True)

        retrieved = Variable.get(var_name)

        draft = DraftingOutput.model_validate(retrieved['draft'])
        assert draft.selected_emails == DRAFT_OUTPUT['selected_emails']
        assert draft.subject == 'Ready-to-train dog leads for Happy Tails'

    def test_a_stored_draft_with_a_placeholder_fails_validation(self):
        """
        The placeholder guard still bites after a storage round-trip.

        Round-tripping through Variable storage must not become a way for
        '[Your Name]' to reach a prospect: the validator is the thing standing
        between the model's unfinished copy and a real send.
        """
        var_name = "drafting_agent_output_placeholder"
        bad = dict(DRAFT_RESULT,
                   draft=dict(DRAFT_OUTPUT, body='Hi [Owner Name], ...'))
        Variable.set(var_name, bad, overwrite=True)

        retrieved = Variable.get(var_name)

        with pytest.raises(ValidationError, match='unfilled placeholder'):
            DraftingOutput.model_validate(retrieved['draft'])

