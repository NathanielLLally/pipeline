"""
What we are selling, and what the outreach is trying to achieve.

Kept here rather than inline in a prompt because the pipeline is
vertical-agnostic: the mechanics of advertising, qualifying and nurturing are
identical across verticals, and only the end-consumer persona, the creatives
and this pitch change. Overridable per run (a run_agents parameter) and per
environment (OUTREACH_OFFER), so a second vertical needs no code change.

The first live run produced a content-marketing pitch because neither prompt
said what the offer was, so the model inferred it. This module exists to stop
that.
"""

import os
from typing import Optional

DEFAULT_OFFER = (
    'We generate and qualify local dog-owner leads through paid advertising '
    'and send them to you live while they are currently engaged; you pay per '
    'qualified lead.'
)

OUTREACH_GOAL = (
    'The goal is a reply carrying any signal of interest -- by email, text or '
    'phone. Booking the call and everything after it is handled separately, '
    'so the email does not need to arrange anything or explain how the '
    'service works. Give a terse overview of the offer and one specific hook '
    'drawn from the research, then ask them to reply if interested. No '
    'pricing, no mechanics, no specifics beyond the overview.'
)


def resolve_offer(offer: Optional[str] = None) -> str:
    """The offer for this run: explicit, then environment, then default."""
    return offer or os.environ.get('OUTREACH_OFFER') or DEFAULT_OFFER
