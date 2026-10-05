"""
Standalone analysis flow: evaluate a batch of drafts across quality metrics.

This flow is useful for:
1. Analyzing drafts in isolation (before integration into run_agents)
2. Re-analyzing existing drafts with a new model/rubric
3. A/B testing different draft approaches
4. Quality control on generated content

Usage from Python:
  from flow.analyze_drafts import analyze_batch_drafts
  results = await analyze_batch_drafts([
    {
        'business': {...},
        'research': {...},
        'draft': {...}
    }
  ])

Usage from CLI:
  python -m prefect run analyze_drafts.py
  prefect deployment run analyze_batch_drafts/run

Can also be called with JSON input file containing array of draft objects.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import flow, task

from flow.agents.analysis import analysis_agent
from flow.artifacts import read_artifact, write_artifact

@task(log_prints=True)
def analyze_single_draft(
    business: dict,
    research: dict,
    draft: dict,
) -> dict:
    """Analyze a single draft and return structured metrics."""
    try:
        result = analysis_agent(
            research=research,
            draft=draft,
            business=business
        )
        return {
            'business_id': business.get('id'),
            'business_name': business.get('business_name'),
            'status': result.get('status'),
            'analysis': result.get('analysis'),
            'error': result.get('error'),
        }
    except Exception as exc:
        return {
            'business_id': business.get('id'),
            'business_name': business.get('business_name'),
            'status': 'error',
            'error': f'{type(exc).__name__}: {exc}',
        }


@flow(log_prints=True)
def analyze_batch_drafts(
    drafts: Optional[List[Dict[str, Any]]] = None,
    json_input_file: Optional[str] = None,
    output_format: str = 'summary',
) -> dict:
    """
    Analyze a batch of draft emails.

    Args:
        drafts: List of dicts with 'business', 'research', 'draft' keys
        json_input_file: Path to JSON file containing array of drafts
        output_format: 'summary' for high-level stats, 'detailed' for all metrics

    Returns:
        Dict with counts, scores breakdown, and individual results
    """
    if json_input_file:
        try:
            drafts = read_artifact(json_input_file, suffix='batch')
            if not isinstance(drafts, list):
                # Handle single-object wrapped in artifact
                drafts = [drafts]
        except Exception as e:
            print(f'Failed to load input file: {e}')
            raise
    elif not drafts:
        raise ValueError('Either drafts or json_input_file required')

    print(f'analyzing {len(drafts)} draft(s)...')

    results = []
    for draft_data in drafts:
        result = analyze_single_draft(
            business=draft_data.get('business', {}),
            research=draft_data.get('research', {}),
            draft=draft_data.get('draft', {})
        )
        results.append(result)

    # Compute aggregate statistics
    successful = [r for r in results if r['status'] == 'analyzed']
    errored = [r for r in results if r['status'] == 'error']

    stats = {
        'total': len(results),
        'analyzed': len(successful),
        'errored': len(errored),
    }

    # Extract all scores for aggregation
    score_keys = [
        'personalization_score',
        'pain_points_score',
        'call_to_action_score',
        'business_relevance_score',
        'authenticity_score',
        'clarity_score',
        'trust_signals_score',
        'overall_quality_score',
    ]

    aggregates = {}
    for key in score_keys:
        scores = [
            r['analysis'][key]
            for r in successful
            if r.get('analysis') and key in r['analysis']
        ]
        if scores:
            aggregates[key] = {
                'mean': sum(scores) / len(scores),
                'min': min(scores),
                'max': max(scores),
                'count': len(scores),
            }

    output = {
        'stats': stats,
        'score_aggregates': aggregates,
        'results': results if output_format == 'detailed' else None,
    }

    # Print summary
    print(f"Analyzed: {stats['analyzed']}, Errored: {stats['errored']}")
    for key, agg in aggregates.items():
        print(
            f"  {key}: mean={agg['mean']:.2f} "
            f"(min={agg['min']:.1f}, max={agg['max']:.1f})"
        )

    # Write output artifact
    try:
        write_artifact(output, suffix='analysis-batch')
    except Exception as e:
        print(f'Warning: could not write output artifact: {e}')

    return output


if __name__ == '__main__':
    # Quick test: can be invoked directly for testing
    print('Run with: prefect run analyze_drafts.py')
    print('Or deploy and run: prefect deployment run analyze_batch_drafts/run')
