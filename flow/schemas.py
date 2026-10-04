"""
Pydantic models for the research and drafting pipeline (spec section 3).

No I/O and no Prefect imports: these are the contract between the nodes, and
they must stay importable anywhere, including inside a Prefect worker.

Written for Python 3.9 -- `Optional[str]`, never `str | None`. The spec's
snippets use PEP 604 unions illustratively; they would crash on the prod
interpreter.
"""

from datetime import datetime
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

REJECTION_REASONS = (
    'no_verified_emails',
    'low_confidence_after_two_passes',
    'low_confidence_no_escalation_url',
    'llm_schema_failure',
)


class VerifiedEmail(BaseModel):
    email: str
    verified_at: datetime
    source: str
    mx_server: Optional[str] = None


class ResearchOutput(BaseModel):
    business_name: str
    # The person to write to, extracted from the crawled pages. Optional
    # because plenty of sites never name anyone, and an invented name is
    # worse than none -- hence the placeholder validator below.
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    pain_signals: List[str]
    personalization_hook: str
    inferred_tone: Literal['clinical', 'warm', 'premium', 'casual', 'sparse']
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str]
    suggested_email: str
    next_url_to_check: Optional[str] = None

    @field_validator('contact_name', 'contact_title')
    @classmethod
    def no_invented_person(cls, value: Optional[str]) -> Optional[str]:
        """Reject '[Owner Name]' and friends; no name beats a fake one."""
        if value is None:
            return value
        found = find_placeholder(value)
        if found:
            raise ValueError(
                f'invented person {found!r}: leave contact_name null unless '
                'the page actually names someone'
            )
        return value


# A placeholder is a token the model left for a human to fill: [Your Name],
# {{first_name}}, <COMPANY>. Distinguishing it from a real bracketed aside in
# prose ("[the services page especially]") needs more than a regex: the
# discriminator is that placeholders are short and either Title Case or
# snake/UPPER case, while asides are lowercase running text.
_BRACKETED = re.compile(r'\[([^\]\n]{1,60})\]'
                        r'|\{\{([^}\n]{1,60})\}\}'
                        r'|\{([^}\n]{1,60})\}'
                        r'|<([^>\n]{1,60})>')


def _looks_like_placeholder(token: str) -> bool:
    """True for 'Your Name' or 'first_name', false for a prose aside."""
    token = token.strip()
    if not token:
        return False

    words = token.split()
    if len(words) > 3:
        return False
    if '_' in token and token.replace('_', '').isalnum():
        return True
    if token.isupper():
        return True
    return all(w[:1].isupper() for w in words)


def find_placeholder(value: str) -> Optional[str]:
    """Return the first unfilled placeholder in `value`, or None."""
    for match in _BRACKETED.finditer(value):
        token = next(g for g in match.groups() if g is not None)
        if _looks_like_placeholder(token):
            return match.group(0)
    return None


class DraftingOutput(BaseModel):
    selected_emails: List[str] = Field(min_length=1)
    subject: str
    body: str
    rationale: str

    @field_validator('subject', 'body')
    @classmethod
    def no_placeholders(cls, value: str) -> str:
        """
        Reject copy the model left unfinished.

        Raising here rather than checking downstream is deliberate: the
        validation-retry loop in flow.llm re-asks the model with this error
        attached, so the model fixes its own draft instead of the pipeline
        forwarding '[Your Name]' to a prospect.
        """
        found = find_placeholder(value)
        if found:
            raise ValueError(
                f'unfilled placeholder {found!r}: write finished '
                'copy, and do not sign off with a name -- the sending system '
                'adds the signature'
            )
        return value
