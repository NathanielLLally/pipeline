"""
Deterministic QA metrics for a draft email.

The analysis agent's eight scores are an LLM's judgment. These are computed:
no model call, so they cost nothing, never time out, and give the same answer
every run. They catch broken output an LLM grader may only half-notice -- the
Maximum Canine draft that reached Warmbly ended mid-word in Thai ("you only
payต่อ") and was still status=drafted.

`failures` lists what makes a draft unsendable as written. Signals that are a
matter of quality rather than breakage (no question asked, business not named)
are reported but never fail a draft: grading those is the analysis
dimensions' job.

No I/O and no Prefect imports, like flow/schemas.py.
"""

import os
import re
import unicodedata
from typing import Any, Dict, Optional

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


_SIGN_OFF = re.compile(
    r'^(thanks|thank you|best|best regards|regards|kind regards|warm regards|'
    r'warmly|sincerely|cheers|all the best|talk soon)\b[\s,.!]*$', re.IGNORECASE)


def _signs_off(body: str) -> bool:
    lines = [ln.strip() for ln in body.rstrip().splitlines() if ln.strip()]
    return len(lines) > 1 and bool(_SIGN_OFF.match(lines[-1]))


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
