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

Alongside the LLM's scores it computes deterministic QA metrics (qa_metrics):
no model call, so they cost nothing, never time out, and give the same answer
every run. They catch broken output an LLM grader may only half-notice -- the
Maximum Canine draft that reached Warmbly ended mid-word in Thai ("you only
payต่อ") and was still status=drafted. `failures` lists what makes a draft
unsendable as written; signals that are a matter of quality rather than
breakage (no question asked, business not named) are reported but never fail
a draft: grading those is the LLM dimensions' job.

Can be called with dict params or from a JSON input artifact:
  analysis_agent(
      research={...},
      draft={...},
      business={...}
  )
  analysis_agent(json_input_file='biz-1-analysis-input.json')
"""

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.artifacts import (
    read_artifact,
    register_artifact,
    variable_safe_name,
    write_artifact,
)
from flow.llm import LLMSchemaError, LLMTransportError, complete_structured
from flow.schemas import DraftAnalysisOutput, find_sign_off

# Scripts an English cold email may legitimately contain: Latin letters
# (accents included) plus the punctuation, symbols and digits every script
# shares. Anything else -- Thai, CJK, Cyrillic -- is a model drifting language.
_ALLOWED_SCRIPTS = ('LATIN',)

_TERMINAL = '.!?'
# Closers that may follow the terminal stop: 'pay (no strings.)' or '"yes."'
_TRAILING_CLOSERS = '"\')]”’'

_HTML_ENTITY = re.compile(r'&(?:[a-zA-Z]{2,8}|#\d{1,6}|#x[0-9a-fA-F]{1,6});')
_MARKDOWN = re.compile(r'(\*\*[^*\n]+\*\*|^#{1,6}\s|^\s*[-*]\s+\S|\[[^\]\n]+\]\([^)\n]+\))',
                       re.MULTILINE)


def _limit(name: str, default: int) -> int:
    """Read per call, so a limit can change without a process restart."""
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _is_foreign(char: str) -> bool:
    if ord(char) < 128 or not char.isalpha():
        return False
    try:
        name = unicodedata.name(char)
    except ValueError:
        return True
    return not name.startswith(_ALLOWED_SCRIPTS)


def _foreign_chars(text: str) -> list:
    # Combining marks attached to a foreign letter (the Thai tone mark in
    # "ต่อ") are not alpha on their own; include them alongside it.
    found = set()
    for i, char in enumerate(text):
        if _is_foreign(char):
            found.add(char)
        elif unicodedata.combining(char) and i and _is_foreign(text[i - 1]):
            found.add(char)
    return sorted(found)


def _signs_off(body: str) -> bool:
    return find_sign_off(body) is not None


def _ends_terminated(body: str) -> bool:
    lines = [ln for ln in body.rstrip().splitlines() if ln.strip()]
    # A closing line like "Thanks" or "Best, Nate" carries no stop; judge
    # the line above it instead. Three words keeps "and you only pay" -- a
    # cutoff on its own line -- from passing as a sign-off.
    if len(lines) > 1 and len(lines[-1].split()) <= 3:
        lines.pop()
    tail = (lines[-1] if lines else '').rstrip().rstrip(_TRAILING_CLOSERS)
    return bool(tail) and tail[-1] in _TERMINAL


def qa_metrics(draft: Dict[str, Any],
               business: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute QA metrics for a draft {subject, body}."""
    subject = draft.get('subject') or ''
    body = draft.get('body') or ''
    text = f'{subject}\n{body}'
    name = (business or {}).get('business_name')

    metrics: Dict[str, Any] = {
        'subject_chars': len(subject),
        'body_chars': len(body),
        'body_words': len(body.split()),
        'paragraphs': len([p for p in re.split(r'\n\s*\n', body) if p.strip()]),
        'ends_with_terminal_punctuation': _ends_terminated(body),
        'non_latin_chars': _foreign_chars(text),
        'html_entities': sorted(set(_HTML_ENTITY.findall(text))),
        'has_markdown': bool(_MARKDOWN.search(body)),
        'has_question': '?' in body,
        'has_sign_off': _signs_off(body),
        'mentions_business_name': (name.lower() in text.lower()) if name else None,
    }

    failures = []
    if not body.strip():
        failures.append('empty_body')
    else:
        if not metrics['ends_with_terminal_punctuation']:
            failures.append('unterminated_body')
        if metrics['body_words'] < _limit('QA_MIN_BODY_WORDS', 25):
            failures.append('body_too_short')
    if metrics['body_words'] > _limit('QA_MAX_BODY_WORDS', 250):
        failures.append('body_too_long')
    if metrics['subject_chars'] > _limit('QA_MAX_SUBJECT_CHARS', 90):
        failures.append('subject_too_long')
    if metrics['non_latin_chars']:
        failures.append('non_latin_script')
    if metrics['html_entities']:
        failures.append('html_entity')
    if metrics['has_markdown']:
        failures.append('markdown')
    # The campaign template appends {{.Signature}} after the body.
    if metrics['has_sign_off']:
        failures.append('sign_off')

    metrics['failures'] = failures
    metrics['passed'] = not failures
    return metrics


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
        # The same fact qa_metrics' 'sign_off' check rests on. Not told it,
        # the model scored trust 3/10 for a missing sender.
        'The campaign template appends the sender\'s signature after the body, '
        'so the body above is deliberately unsigned. Do not score it down for '
        'having no sender name, sign-off or contact details -- the signature '
        'supplies them. A sign-off line at the end of the body ("Thanks,", '
        '"Best regards") or a sender name is a defect: it would sit above '
        'the signature, and a drafted name is invented.',
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
        register_artifact(input_data, suffix='input', description='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    business_id = business.get('id', 'unknown')
    business_name = business.get('business_name', 'Unknown')
    prompt = build_analysis_prompt(research, draft, business)
    # Computed before the LLM call so it is present even when that call fails.
    qa = qa_metrics(draft, business)
    if not qa['passed']:
        print(f'QA failed for {business_name}: {qa["failures"]}')

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
            'analysis': None,
            'qa': qa,
        }
        _set_analysis_output(result, business_id)
        write_artifact(result, suffix='output')
        register_artifact(result, suffix='output', description='output')
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
        'analysis': analysis.model_dump(mode='json'),
        'qa': qa,
    }
    _set_analysis_output(result, business_id)
    write_artifact(result, suffix='output')
    register_artifact(result, suffix='output', description='output')
    return result
