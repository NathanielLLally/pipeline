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
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class Contact(BaseModel):
    """A person the crawled pages actually name."""

    name: str
    title: Optional[str] = None
    # Only an address the page states or the live verified pool holds; the
    # research node nulls anything else after parsing.
    email: Optional[str] = None
    # Set from the title: owner, founder, director, practice manager and the
    # like decide whether to buy; a trainer or groomer usually does not.
    is_decision_maker: bool = False

    @field_validator('name', 'title')
    @classmethod
    def no_invented_person(cls, value: Optional[str]) -> Optional[str]:
        """Reject '[Owner Name]' and friends; no name beats a fake one."""
        if value is None:
            return value
        found = find_placeholder(value)
        if found:
            raise ValueError(
                f'invented person {found!r}: leave contacts empty unless '
                'the page actually names someone'
            )
        return value


class ResearchOutput(BaseModel):
    business_name: str
    # The people to write to, extracted from the crawled pages. Empty is
    # normal: plenty of sites never name anyone, and an invented name is
    # worse than none -- hence the placeholder validator on Contact.
    contacts: List[Contact] = Field(default_factory=list)
    pain_signals: List[str]
    personalization_hook: str
    inferred_tone: Literal['clinical', 'warm', 'premium', 'casual', 'sparse']
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str]
    suggested_email: str
    next_url_to_check: Optional[str] = None


def primary_contact(research: Dict[str, Any]) -> Tuple[Optional[str],
                                                       Optional[str]]:
    """
    The (name, title) downstream writes to: the first decision maker, else
    the first named contact.

    Takes the dumped dict, not the model, because that is what flows between
    nodes and what earlier runs stored -- and those carry the single
    contact_name/contact_title pair this list replaced, so it is read too.
    """
    contacts = research.get('contacts') or []
    chosen = next((c for c in contacts if c.get('is_decision_maker')), None)
    if chosen is None and contacts:
        chosen = contacts[0]
    if chosen is not None:
        return chosen.get('name'), chosen.get('title')
    return research.get('contact_name'), research.get('contact_title')


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
    # The LLM sees the body as `email_text`; everything downstream, and every
    # stored draft, keeps `body`. nemotron-3-ultra behind the proxy cuts any
    # string field named `body` (or `message`) at 256 characters under
    # json_schema, mid-sentence and with finish_reason=stop, while the same
    # model writes 700+ characters into `email_text`.
    model_config = ConfigDict(populate_by_name=True)

    selected_emails: List[str] = Field(min_length=1)
    subject: str
    body: str = Field(alias='email_text')
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


class DraftAnalysisOutput(BaseModel):
    """Analysis metrics for a draft email on a 0-10 scale."""

    personalization_score: float = Field(
        ge=0.0, le=10.0,
        description="How specifically tailored is the content to THIS business? "
                    "0=completely generic/could send to anyone, 10=obviously unique to this business"
    )
    personalization_rationale: str = Field(
        description="Why the personalization score: what specific details make it unique or generic?"
    )

    pain_points_score: float = Field(
        ge=0.0, le=10.0,
        description="How well does it address the business's specific pain points? "
                    "0=misses the mark entirely, 10=perfectly articulates their exact problem"
    )
    pain_points_rationale: str = Field(
        description="Which pain points are addressed and how well? Any gaps or misses?"
    )

    call_to_action_score: float = Field(
        ge=0.0, le=10.0,
        description="How compelling is the hook/CTA? Does it create urgency or desire? "
                    "0=no hook/reason to respond, 10=recipient feels compelled to reply immediately"
    )
    call_to_action_rationale: str = Field(
        description="Analyze the CTA mechanism: what creates urgency or appeal? Is it compelling?"
    )

    business_relevance_score: float = Field(
        ge=0.0, le=10.0,
        description="How relevant is the offer/solution to THIS business type? "
                    "0=irrelevant to their model, 10=perfect fit for their business"
    )
    business_relevance_rationale: str = Field(
        description="Is the offer a strong fit for this business model and revenue stream?"
    )

    authenticity_score: float = Field(
        ge=0.0, le=10.0,
        description="Does this sound like a real person or a template? "
                    "0=obvious form letter, 10=genuine, conversational, specific voice"
    )
    authenticity_rationale: str = Field(
        description="Does the writing feel authentic and human, or templated and robotic?"
    )

    clarity_score: float = Field(
        ge=0.0, le=10.0,
        description="How clear and easy to understand? "
                    "0=confusing/hard to parse, 10=crystal clear what is being offered"
    )
    clarity_rationale: str = Field(
        description="Is the message easy to understand? Any confusing or unclear sections?"
    )

    trust_signals_score: float = Field(
        ge=0.0, le=10.0,
        description="Does it include credibility/trust signals? "
                    "0=no trust elements, 10=includes strong proof points or credibility indicators"
    )
    trust_signals_rationale: str = Field(
        description="What trust signals or credibility elements are present (or missing)?"
    )

    overall_quality_score: float = Field(
        ge=0.0, le=10.0,
        description="Overall quality and likely response rate potential. "
                    "0=likely to be deleted immediately, 10=likely to get a response"
    )
    overall_quality_rationale: str = Field(
        description="Summary: is this a strong outreach? What's the likelihood it gets a positive response?"
    )

    key_strengths: List[str] = Field(
        description="Top 2-3 things this draft does well"
    )
    improvement_areas: List[str] = Field(
        description="Top 2-3 things that could be improved"
    )
