"""
Test the analysis agent with mock data.

Run with: pytest flow/test_analysis_agent.py -v
Or directly: python -m pytest flow/test_analysis_agent.py -v
Or from project root: python -c "import sys; sys.path.insert(0, '.'); exec(open('flow/test_analysis_agent.py').read())"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from flow.agents.analysis import build_analysis_prompt
from flow.schemas import DraftAnalysisOutput


def test_analysis_prompt_building():
    """Test that the analysis prompt is correctly built."""
    research = {
        'business_name': 'Acme Dog Training',
        'personalization_hook': 'They focus on board-and-train programs',
        'pain_signals': ['New client acquisition', 'High staff turnover'],
        'inferred_tone': 'premium',
    }
    draft = {
        'subject': 'New client pipeline for Acme Dog Training',
        'body': 'I noticed you specialize in board-and-train programs...',
        'rationale': 'Targeting their core service offering',
    }
    business = {
        'business_name': 'Acme Dog Training',
        'business_description': 'Premium board-and-train dog training',
    }

    prompt = build_analysis_prompt(research, draft, business)

    # Verify key elements are in the prompt
    assert 'Acme Dog Training' in prompt
    assert 'board-and-train' in prompt
    assert 'New client acquisition' in prompt
    assert 'New client pipeline for Acme Dog Training' in prompt
    assert 'I noticed you specialize in board-and-train programs' in prompt
    print('✓ Prompt building test passed')


def test_draft_analysis_output_schema():
    """Test that DraftAnalysisOutput validates correct data."""
    valid_data = {
        'personalization_score': 8.5,
        'personalization_rationale': 'Mentions their specific training niche.',
        'pain_points_score': 7.0,
        'pain_points_rationale': 'Addresses new client acquisition.',
        'call_to_action_score': 7.5,
        'call_to_action_rationale': 'Creates curiosity about results.',
        'business_relevance_score': 8.0,
        'business_relevance_rationale': 'Perfect fit for their model.',
        'authenticity_score': 7.0,
        'authenticity_rationale': 'Conversational tone.',
        'clarity_score': 8.5,
        'clarity_rationale': 'Clear offer.',
        'trust_signals_score': 6.5,
        'trust_signals_rationale': 'Some proof points included.',
        'overall_quality_score': 7.5,
        'overall_quality_rationale': 'Strong overall email.',
        'key_strengths': ['Specific to their niche', 'Clear CTA'],
        'improvement_areas': ['Add more trust signals', 'Create more urgency'],
    }

    # Should validate successfully
    analysis = DraftAnalysisOutput(**valid_data)
    assert analysis.personalization_score == 8.5
    assert analysis.overall_quality_score == 7.5
    assert len(analysis.key_strengths) == 2
    print('✓ Schema validation test passed')


def test_draft_analysis_score_boundaries():
    """Test that score boundaries are enforced (0-10)."""
    valid_data = {
        'personalization_score': 0.0,  # Min
        'personalization_rationale': 'Test',
        'pain_points_score': 10.0,  # Max
        'pain_points_rationale': 'Test',
        'call_to_action_score': 5.5,  # Middle
        'call_to_action_rationale': 'Test',
        'business_relevance_score': 7.2,
        'business_relevance_rationale': 'Test',
        'authenticity_score': 3.1,
        'authenticity_rationale': 'Test',
        'clarity_score': 8.9,
        'clarity_rationale': 'Test',
        'trust_signals_score': 4.0,
        'trust_signals_rationale': 'Test',
        'overall_quality_score': 6.7,
        'overall_quality_rationale': 'Test',
        'key_strengths': ['A'],
        'improvement_areas': ['B'],
    }

    # Should validate successfully
    analysis = DraftAnalysisOutput(**valid_data)
    assert analysis.personalization_score == 0.0
    assert analysis.pain_points_score == 10.0
    print('✓ Score boundary test passed')


def test_draft_analysis_invalid_score():
    """Test that out-of-range scores are rejected."""
    invalid_data = {
        'personalization_score': 10.5,  # Out of range
        'personalization_rationale': 'Test',
        'pain_points_score': 5.0,
        'pain_points_rationale': 'Test',
        'call_to_action_score': 5.0,
        'call_to_action_rationale': 'Test',
        'business_relevance_score': 5.0,
        'business_relevance_rationale': 'Test',
        'authenticity_score': 5.0,
        'authenticity_rationale': 'Test',
        'clarity_score': 5.0,
        'clarity_rationale': 'Test',
        'trust_signals_score': 5.0,
        'trust_signals_rationale': 'Test',
        'overall_quality_score': 5.0,
        'overall_quality_rationale': 'Test',
        'key_strengths': ['A'],
        'improvement_areas': ['B'],
    }

    # Should raise validation error
    with pytest.raises(Exception):  # Pydantic ValidationError
        DraftAnalysisOutput(**invalid_data)
    print('✓ Invalid score rejection test passed')


def test_all_metrics_present():
    """Test that all 8 quality metrics are in the schema."""
    metrics = [
        'personalization_score',
        'pain_points_score',
        'call_to_action_score',
        'business_relevance_score',
        'authenticity_score',
        'clarity_score',
        'trust_signals_score',
        'overall_quality_score',
    ]

    for metric in metrics:
        assert metric in DraftAnalysisOutput.model_fields, f"Missing metric: {metric}"

    print(f'✓ All {len(metrics)} metrics present')


def test_rationale_for_each_score():
    """Test that each score has a corresponding rationale."""
    metrics = [
        'personalization',
        'pain_points',
        'call_to_action',
        'business_relevance',
        'authenticity',
        'clarity',
        'trust_signals',
        'overall_quality',
    ]

    for metric in metrics:
        score_field = f'{metric}_score'
        rationale_field = f'{metric}_rationale'
        assert score_field in DraftAnalysisOutput.model_fields, f"Missing: {score_field}"
        assert rationale_field in DraftAnalysisOutput.model_fields, f"Missing: {rationale_field}"

    print(f'✓ All {len(metrics)} metrics have rationales')


if __name__ == '__main__':
    print('Running analysis agent tests...\n')
    test_analysis_prompt_building()
    test_draft_analysis_output_schema()
    test_draft_analysis_score_boundaries()
    test_all_metrics_present()
    test_rationale_for_each_score()
    print('\n✓ All tests passed!')
