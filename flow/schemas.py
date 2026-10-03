"""
Pydantic models for the research and drafting pipeline (spec section 3).

No I/O and no Prefect imports: these are the contract between the nodes, and
they must stay importable anywhere, including inside a Prefect worker.

Written for Python 3.9 -- `Optional[str]`, never `str | None`. The spec's
snippets use PEP 604 unions illustratively; they would crash on the prod
interpreter.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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
    pain_signals: List[str]
    personalization_hook: str
    inferred_tone: Literal['clinical', 'warm', 'premium', 'casual', 'sparse']
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str]
    suggested_email: str
    next_url_to_check: Optional[str] = None


class DraftingOutput(BaseModel):
    selected_emails: List[str] = Field(min_length=1)
    subject: str
    body: str
    rationale: str
