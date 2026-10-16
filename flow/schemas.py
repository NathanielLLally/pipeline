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
    # The model never answered: still down after flow.llm's retries. Not a
    # verdict on the business, so the outreach state records it as
    # 'failed' and a later batch takes it again.
    'llm_unavailable',
)


def failure_reason(exc: BaseException) -> str:
    """The rejection reason for an exception out of complete_structured:
    llm_unavailable for a transport failure, llm_schema_failure otherwise."""
    if type(exc).__name__ == 'LLMTransportError':
        return 'llm_unavailable'
    return 'llm_schema_failure'


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
    # Owner, founder, director, practice manager and the like decide whether
    # to buy; a trainer or groomer usually does not. Shown by the title, or
    # by `evidence` -- family businesses rarely print 'Owner' by a name.
    is_decision_maker: bool = False
    # A quote, verbatim from the pages, that names this person and shows
    # their role ('started everything for Laura and me'). Research nulls it
    # when it is not on the page, and a decision maker needs it or a title.
    evidence: Optional[str] = None

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


class OutreachAngle(BaseModel):
    """
    The one thing about the business itself -- not its customers -- that the
    offer speaks to. Drafting builds the email's opening and its offer
    sentence around it (flow.offer.build_outreach_goal).
    """

    # Their situation in one plain sentence: a stated problem (lost ad
    # budget, empty classes, a closed location) or a growth move (hiring, a
    # new location or program, free-lesson funnels that need filling).
    problem: str
    # Quoted verbatim from the crawled text; research drops the whole angle
    # when the quote is not there.
    evidence: str
    # What the pitch must not trip over, given what they have been through.
    caution: Optional[str] = None


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
    # Optional so research stored before it existed still validates.
    outreach_angle: Optional[OutreachAngle] = None


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
        claim = find_payment_or_promise(value)
        if claim:
            raise ValueError(
                f'payment terms or a promise {claim!r}: say nothing about how '
                'or when they pay, and promise nothing the offer does not '
                'state'
            )
        return value


# Payment terms and promises a draft must not make. Customers pay upfront;
# drafts said 'no upfront ad spend', 'you pay only for qualified leads' and
# 'fraud protection built in'. Matched case-insensitively on the subject
# and body.
_PAYMENT_OR_PROMISE = re.compile(
    r'up[\s-]?front|ad spend|\bpay(?:s|ing)? only\b|\bonly pay|'
    r'\bpay(?:s|ing)?[\s-]per\b|\bper[\s-](?:qualified[\s-])?lead\b|'
    r'risk[\s-]?free|'
    r'\bno risk\b|guarantee|money[\s-]?back|refund|free trial',
    re.IGNORECASE)


# Payment terms alone: what a research caution must not carry. Narrower than
# the draft check, because a caution names what to avoid -- 'do not pitch
# ad spend or guarantee results' is a good caution.
_PAYMENT_TERMS = re.compile(
    r'up[\s-]?front|\bpay(?:s|ing)? only\b|\bonly pay|'
    r'\bpay(?:s|ing)?[\s-]per\b|\bper[\s-](?:qualified[\s-])?lead\b|'
    r'risk[\s-]?free|\bno risk\b|money[\s-]?back|refund',
    re.IGNORECASE)


def find_payment_terms(value: str) -> Optional[str]:
    """The first payment term in `value`, or None."""
    match = _PAYMENT_TERMS.search(value or '')
    return match.group(0) if match else None


def find_payment_or_promise(value: str) -> Optional[str]:
    """The first payment term or promise in `value`, or None."""
    match = _PAYMENT_OR_PROMISE.search(value or '')
    return match.group(0) if match else None


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
