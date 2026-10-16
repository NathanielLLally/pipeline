"""
What we are selling, and what the outreach is trying to achieve.

Kept here rather than inline in a prompt because the pipeline is
vertical-agnostic: the mechanics of advertising, qualifying and nurturing are
identical across verticals, and only the end-consumer persona, the creatives
and this pitch change. Overridable per run (a run_agents parameter) and per
environment (OUTREACH_OFFER), so a second vertical needs no code change.
The goal is not overridable: that the email is cold, and how a cold email
opens, holds for every vertical. Which problem it opens on is per business:
build_outreach_goal assembles it from research's outreach_angle, and
OUTREACH_GOAL is what it falls back to.

The first live run produced a content-marketing pitch because neither prompt
said what the offer was, so the model inferred it. This module exists to stop
that.
"""

import os
from typing import Any, Dict, Optional

#DEFAULT_OFFER = (
#    'We generate and qualify local dog-owner leads through paid advertising '
#    'and send them to you live while they are currently engaged; you pay per '
#    'qualified lead.'
#)

DEFAULT_OFFER = (
    'We generate local dog-owner leads through paid advertising who are ready to buy your service.'
    'No contract or setup fees.'
)

# Customers pay upfront. A goal that let the draft talk about paying 'only
# for screened inquiries' came back as 'no upfront ad spend' -- false -- so
# neither goal leaves room for payment terms, and the draft schema rejects
# them (flow.schemas.DraftingOutput).
NO_TERMS_OR_PROMISES = (
    'Say nothing about how or when they pay, and promise nothing the offer '
    'above does not state -- no guarantees, no "risk-free". do not offer anything for free.'
    'they will pay upfront, do not make any offer regarding trials.  they will have a minimum order'
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
    'mechanics beyond that one sentence. '
    + NO_TERMS_OR_PROMISES
)


def build_outreach_goal(research: Optional[Dict[str, Any]] = None) -> str:
    """
    The drafting goal for this business.

    With research's outreach_angle -- their own situation, a quote that
    shows it, and what the pitch must not trip over -- the email opens on
    that and the offer sentence is written to answer it. Without one, the
    fixed OUTREACH_GOAL.

    Fixed either way: it is a cold email, their problem comes before us,
    one sentence of offer, an ask for a reply, no prices. What changes is
    which problem, and how the offer is put -- the fixed goal's "we run the
    ads" is exactly wrong for a business that paid ads have just burned.
    """
    angle = (research or {}).get('outreach_angle') or {}
    problem = (angle.get('problem') or '').strip()
    if not problem:
        return OUTREACH_GOAL

    evidence = (angle.get('evidence') or '').strip()
    caution = (angle.get('caution') or '').strip()

    parts = [
        'This is a cold email to an owner who has never heard of us. Open '
        'with a problem they already have, not with us -- this one, which '
        f'their own site shows: {problem}',
    ]
    if evidence:
        parts.append(f'In their words: "{evidence}"')
    parts.append(
        'Say it plainly, as something you noticed, in a sentence or two; do '
        'not diagnose them or dwell on it. Say "your area" unless the '
        'research names their town.')
    if caution:
        parts.append(f'Handle it with care: {caution}')
    parts.append(
        'Then one sentence on the offer, put so it answers that problem'
        + (' and respects the caution above' if caution else '')
        + ': what they get, which is serious local inquiries we have already '
        'screened, rather than how we get them.')
    parts.append(
        'Then ask them to reply if interested. The goal is a reply carrying '
        'any signal of interest -- by email, text or phone. Booking the call '
        'and everything after it is handled separately, so the email does '
        'not need to arrange anything. Write it for this business, not as a '
        'template. No prices or rates, and no mechanics beyond that one '
        'sentence. ' + NO_TERMS_OR_PROMISES)
    return ' '.join(parts)


def resolve_offer(offer: Optional[str] = None) -> str:
    """The offer for this run: explicit, then environment, then default."""
    return offer or os.environ.get('OUTREACH_OFFER') or DEFAULT_OFFER
