"""
What we are selling, and what the outreach is trying to achieve.

Kept here rather than inline in a prompt because the pipeline is
vertical-agnostic: the mechanics of advertising, qualifying and nurturing are
identical across verticals, and only the end-consumer persona, the creatives
and this pitch change. Overridable per run (a run_agents parameter) and per
environment (OUTREACH_OFFER), so a second vertical needs no code change.
The goal is not overridable: that the email is cold, and how a cold email
opens, holds for every vertical.

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
    'This is a cold email to an owner who has never heard of us. Open with a '
    'problem they already have, not with us, drawn from the research: one of '
    'their pain signals, or the specific problem their own customers come to '
    'them for, put as people in their area looking for help with it right '
    'now; failing those, the inquiries that go nowhere -- tire-kickers and '
    'window-shoppers. Say "your area" unless the research names their town. '
    'Then one sentence on the offer: we run the ads, screen the inquiries, '
    'and pass them only the serious ones. Then ask them to reply if '
    'interested. The goal is a reply carrying any signal of interest -- by '
    'email, text or phone. Booking the call and everything after it is '
    'handled separately, so the email does not need to arrange anything. '
    'Write it for this business, not as a template. No pricing, and no '
    'mechanics beyond that one sentence.'
)


def resolve_offer(offer: Optional[str] = None) -> str:
    """The offer for this run: explicit, then environment, then default."""
    return offer or os.environ.get('OUTREACH_OFFER') or DEFAULT_OFFER
