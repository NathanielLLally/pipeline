"""
The analysis-agent deployment.

Its contract: a researched business, a draft email, and research context in,
a DraftAnalysisOutput with comprehensive quality metrics out.

Evaluates draft emails on 9 dimensions with 0-10 scales:
- Personalization: How specific to THIS business
- Pain point addressing: How well does it solve their problems
- Call-to-action hook: How compelling is the urgency/appeal
- Business relevance: How well it fits their business model
- Authenticity: Does it sound human or templated
- Clarity: Is the message easy to understand
- Trust signals: Does it build credibility
- Overall quality: Likelihood of getting a response
- Plus key strengths and improvement areas

Can be called with dict params or from a JSON input artifact:
  analysis_agent(
      research={...},
      draft={...},
      business={...}
  )
  analysis_agent(json_input_file='biz-1-analysis-input.json')
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.artifacts import read_artifact, variable_safe_name, write_artifact
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.schemas import DraftAnalysisOutput

SYSTEM_PROMPT = (
    'You are an expert email copywriter and sales consultant evaluating '
    'outreach emails to small business owners. Analyze emails on multiple '
    'dimensions with structured metrics. Be specific and evidence-based in '
    'your analysis. Reference actual phrases from the email when explaining '
    'scores. Consider the business context when evaluating relevance.'
)


def build_analysis_prompt(
    research: dict,
    draft: dict,
    business: dict,
) -> str:
    """Assemble the analysis prompt. Pure, so it can be asserted on."""
    draft_subject = draft.get('subject', '(no subject)')
    draft_body = draft.get('body', '(no body)')
    draft_rationale = draft.get('rationale', '(no rationale)')

    business_name = business.get('business_name', 'Unknown')
    business_desc = business.get('business_description', '(no description)')

    personalization_hook = research.get('personalization_hook', '(none provided)')
    pain_signals = research.get('pain_signals', [])
    pain_text = '; '.join(pain_signals) if pain_signals else '(none identified)'
    inferred_tone = research.get('inferred_tone', 'unknown')

    return '\n'.join([
        f"Business Being Targeted: {business_name}",
        f"Business Description: {business_desc}",
        f"Observed Tone: {inferred_tone}",
        '',
        f"Researcher's Personalization Hook: {personalization_hook}",
        f"Identified Pain Signals: {pain_text}",
        '',
        f"Email Subject: {draft_subject}",
        f"Email Body: {draft_body}",
        f"Drafter's Rationale: {draft_rationale}",
        '',
        'Analyze this draft across multiple dimensions:',
        '',
        '1. PERSONALIZATION (0-10): How obviously is this tailored to THIS specific business?',
        '   Not just mentioning their name, but showing real knowledge of their specific situation.',
        '',
        '2. PAIN POINTS (0-10): How directly does it address the business\'s actual pain signals?',
        '   Does it articulate the specific problems they face? Does the solution match their needs?',
        '',
        '3. CALL-TO-ACTION HOOK (0-10): How compelling is the urgency/appeal?',
        '   Would a busy business owner feel compelled to respond? Is there a clear reason to engage?',
        '',
        '4. BUSINESS RELEVANCE (0-10): How well does the offer fit THIS business model?',
        '   Is it a natural fit for their revenue stream and customer acquisition strategy?',
        '',
        '5. AUTHENTICITY (0-10): Does this sound like a real person, or a form letter?',
        '   Is the voice genuine and conversational, or templated and robotic?',
        '',
        '6. CLARITY (0-10): Is the message crystal clear?',
        '   Could someone read it once and understand exactly what\'s being offered?',
        '',
        '7. TRUST SIGNALS (0-10): Does it build credibility?',
        '   Does it include proof points, specific examples, or elements that build trust?',
        '',
        '8. OVERALL QUALITY (0-10): What\'s the likelihood this gets a response?',
        '   Synthesize all factors into overall response-rate potential.',
        '',
        'Provide specific examples/quotes from the email in your rationales.',
        'Identify 2-3 key strengths and 2-3 areas for improvement.',
    ])


def _set_analysis_output(result: dict, business_id: str) -> dict:
    """Store analysis output as a Prefect variable and return it."""
    var_name = variable_safe_name(f'analysis_agent_output_{business_id}')
    try:
        from prefect.variables import Variable
        Variable.set(var_name, result, overwrite=True)
    except Exception as e:
        # Variable storage is optional; don't fail the flow if unavailable
        print(f'Note: could not persist output variable {var_name}: {e}')
    return result


@flow(log_prints=True)
def analysis_agent(
    research: Optional[dict] = None,
    draft: Optional[dict] = None,
    business: Optional[dict] = None,
    json_input_file: Optional[str] = None,
) -> dict:
    """
    Analyze a draft email across multiple quality dimensions.

    Can be called with explicit dict params or from a JSON input artifact:
      analysis_agent(research={...}, draft={...}, business={...})
      analysis_agent(json_input_file='biz-1-analysis-input.json')

    If json_input_file is provided, it takes precedence and dict params are ignored.
    Writes output and input artifacts to the current directory, tagged by flow run tags.
    """
    # Load from JSON if provided
    if json_input_file:
        try:
            input_data = read_artifact(json_input_file, suffix='input')
            research = input_data.get('research')
            draft = input_data.get('draft')
            business = input_data.get('business')
        except Exception as e:
            print(f'Failed to load input artifact: {e}')
            raise
    else:
        # Validate that required params are present
        if research is None or draft is None or business is None:
            raise ValueError(
                'Either json_input_file or (research, draft, business) required'
            )
        input_data = {
            'research': research,
            'draft': draft,
            'business': business,
        }

    # Write input artifact
    try:
        write_artifact(input_data, suffix='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    business_id = business.get('id', 'unknown')
    business_name = business.get('business_name', 'Unknown')
    prompt = build_analysis_prompt(research, draft, business)

    try:
        analysis = complete_structured(
            prompt, DraftAnalysisOutput,
            system=SYSTEM_PROMPT,
            agent_model_env='ANALYSIS_MODEL'
        )
    except (LLMSchemaError, LLMTransportError) as exc:
        print(f'analysis failed for {business_name}: {type(exc).__name__}: {exc}')
        result = {
            'status': 'error',
            'error': f'{type(exc).__name__}: {exc}',
            'analysis': None
        }
        _set_analysis_output(result, business_id)
        write_artifact(result, suffix='output')
        return result

    print(
        f'analyzed {business_name}: '
        f'personalization={analysis.personalization_score:.1f}, '
        f'pain_points={analysis.pain_points_score:.1f}, '
        f'cta={analysis.call_to_action_score:.1f}, '
        f'overall={analysis.overall_quality_score:.1f}'
    )

    result = {
        'status': 'analyzed',
        'analysis': analysis.model_dump(mode='json')
    }
    _set_analysis_output(result, business_id)
    write_artifact(result, suffix='output')
    return result
