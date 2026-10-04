"""
Prefect flow: test Prefect variables with structured JSON data.

Run as a deployment:
    prefect deployment run test-prefect-variables/test-prefect-variables

Or directly:
    python flow/test_prefect_variables.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow, task
from pydantic import ValidationError

from flow.schemas import ResearchOutput, DraftingOutput, VerifiedEmail
from prefect.variables import Variable


# Mock data scaffolded from the schemas
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


@task
def test_variable_set_and_get():
    """Test Variable.set() and Variable.get()."""
    print("🧪 Testing Variable set/get...")
    Variable.set("test_string", "hello_world", overwrite=True)
    value = Variable.get("test_string")
    assert value == "hello_world", f"Expected 'hello_world', got {value}"
    print("✅ Variable set/get works")
    return True


@task
def test_research_output_variable():
    """Test storing research output as a variable."""
    print("🧪 Testing research output variable...")
    var_name = "research_agent_output_biz_flow_test"
    Variable.set(var_name, FLOW_RESULT, overwrite=True)

    stored = Variable.get(var_name)
    assert stored is not None
    assert stored['status'] == 'researched'
    assert stored['research']['confidence'] == 0.92

    # Verify it re-validates as the schema
    research = ResearchOutput.model_validate(stored['research'])
    assert research.business_name == 'Happy Tails Dog Training'
    print("✅ Research output variable works")
    return True


@task
def test_draft_output_variable():
    """Test storing draft output as a variable."""
    print("🧪 Testing draft output variable...")
    var_name = "drafting_agent_output_biz_flow_test"
    Variable.set(var_name, DRAFT_RESULT, overwrite=True)

    stored = Variable.get(var_name)
    assert stored is not None
    assert stored['status'] == 'drafted'

    # Verify it re-validates as the schema
    draft = DraftingOutput.model_validate(stored['draft'])
    assert len(draft.selected_emails) == 2
    assert draft.subject == 'Ready-to-train dog leads for Happy Tails'
    print("✅ Draft output variable works")
    return True


@task
def test_variable_overwrite():
    """Test that variables are overwritten on second write."""
    print("🧪 Testing variable overwrite...")
    business_id = 'biz_flow_overwrite_test'

    # First write
    result_1 = {
        'status': 'researched',
        'research': {
            **RESEARCH_OUTPUT,
            'confidence': 0.8,
            'personalization_hook': 'first run',
        },
        'passes': 1,
    }
    var_name = f"research_agent_output_{business_id}"
    Variable.set(var_name, result_1, overwrite=True)

    stored_1 = Variable.get(var_name)
    assert stored_1['research']['confidence'] == 0.8
    assert stored_1['research']['personalization_hook'] == 'first run'

    # Second write (overwrite)
    result_2 = {
        'status': 'researched',
        'research': {
            **RESEARCH_OUTPUT,
            'confidence': 0.92,
            'personalization_hook': 'second run',
        },
        'passes': 2,
    }
    Variable.set(var_name, result_2, overwrite=True)

    stored_2 = Variable.get(var_name)
    assert stored_2['research']['confidence'] == 0.92
    assert stored_2['research']['personalization_hook'] == 'second run'
    assert stored_2['passes'] == 2
    print("✅ Variable overwrite works")
    return True


@task
def test_pretty_print():
    """Test JSON pretty-printing of variable payloads."""
    print("🧪 Testing pretty-print...")
    output_str = json.dumps(FLOW_RESULT, indent=2)

    parsed = json.loads(output_str)
    assert parsed['research']['business_name'] == 'Happy Tails Dog Training'
    assert parsed['research']['confidence'] == 0.92
    print("✅ Pretty-print works")
    return True


@flow(log_prints=True)
def test_prefect_variables() -> dict:
    """
    Test Prefect variables as a flow.

    Tests:
    - Variable.set() and Variable.get()
    - Storing and retrieving structured data (research, draft outputs)
    - Variable overwriting
    - JSON pretty-printing

    Returns:
        dict with test results and pass/fail counts
    """
    print("\n📋 Prefect Variables Flow Test Suite")
    print("=" * 50)

    results = {
        'passed': 0,
        'failed': 0,
        'tests': [],
    }

    tests = [
        ('Variable set/get', test_variable_set_and_get),
        ('Research output variable', test_research_output_variable),
        ('Draft output variable', test_draft_output_variable),
        ('Variable overwrite', test_variable_overwrite),
        ('Pretty-print', test_pretty_print),
    ]

    for test_name, test_fn in tests:
        try:
            result = test_fn()
            results['passed'] += 1
            results['tests'].append({'name': test_name, 'status': 'PASSED'})
            print()
        except Exception as e:
            results['failed'] += 1
            results['tests'].append({
                'name': test_name,
                'status': 'FAILED',
                'error': str(e),
            })
            print(f"❌ {test_name} failed: {e}\n")

    print("=" * 50)
    print(f"Results: {results['passed']} passed, {results['failed']} failed")

    return results


if __name__ == "__main__":
    result = test_prefect_variables()
    print(f"\nFinal result: {result}")
    sys.exit(0 if result['failed'] == 0 else 1)
