"""
flow.offer.build_outreach_goal: the drafting goal, built per business from
research.

The fixed OUTREACH_GOAL told every draft to open with their customers'
problem and to describe the offer as "we run the ads". For Home Dog Training
-- whose ad budget a click-fraud attack had just drained -- that produced a
draft about dog owners' doorbell manners pitching more ads, and analysis
scored its business relevance 2/10. The angle research now returns is the
business's own situation, a quote proving it, and what the pitch must not
trip over; the goal is assembled around it, while the rules that hold for
every cold email stay fixed.
"""

from flow.offer import OUTREACH_GOAL, build_outreach_goal
from flow.schemas import find_placeholder

ANGLE = {
    'problem': 'A click-fraud attack drained their ad budget and closed '
               'their Carrollton facility; they are rebuilding from home.',
    'evidence': 'After a criminal click-fraud attack drained our ad budget',
    'caution': 'Paid ads burned them; do not lead with running ads.',
}


def _goal(**angle):
    return build_outreach_goal({'outreach_angle': {**ANGLE, **angle}})


class TestFallback:
    def test_no_research_is_the_fixed_goal(self):
        assert build_outreach_goal(None) == OUTREACH_GOAL

    def test_no_angle_is_the_fixed_goal(self):
        assert build_outreach_goal({'outreach_angle': None}) == OUTREACH_GOAL

    def test_an_angle_without_a_problem_is_the_fixed_goal(self):
        assert _goal(problem='  ') == OUTREACH_GOAL


class TestBuiltGoal:
    def test_opens_with_their_problem_not_with_us(self):
        assert 'open with a problem they already have' in _goal().lower()

    def test_carries_the_problem_and_its_evidence(self):
        goal = _goal()

        assert ANGLE['problem'] in goal
        assert ANGLE['evidence'] in goal

    def test_the_caution_also_governs_the_offer_sentence(self):
        """Opening on click fraud then saying 'we run the ads' is the
        exact failure; the caution has to reach the offer, not just the
        opening."""
        goal = _goal()
        offer_part = goal[goal.lower().index('then one sentence on the offer'):]

        assert ANGLE['caution'] in goal
        assert 'caution' in offer_part.lower()

    def test_without_a_caution_there_is_no_care_instruction(self):
        assert 'Handle it with care' not in _goal(caution=None)

    def test_does_not_hard_code_we_run_the_ads(self):
        assert 'we run the ads' not in _goal().lower()

    def test_keeps_the_rules_every_cold_email_follows(self):
        goal = _goal().lower()

        assert 'cold email' in goal
        assert 'reply' in goal
        assert 'no prices' in goal

    def test_says_their_town_rule_like_the_fixed_goal(self):
        assert '"your area"' in _goal()

    def test_carries_no_placeholder_for_the_model_to_echo(self):
        assert find_placeholder(_goal()) is None


class TestNoPaymentTermsOrPromises:
    """Customers pay upfront. The angle goal let the draft say they 'pay
    only for screened inquiries', and the model turned that into 'no upfront
    ad spend' -- false. Neither goal may invite payment terms or promises."""

    def test_the_built_goal_no_longer_invites_payment_terms(self):
        goal = _goal().lower()

        assert 'pay only' not in goal
        assert 'is not a price' not in goal

    def test_both_goals_forbid_payment_terms_and_promises(self):
        for goal in (_goal(), OUTREACH_GOAL):
            lowered = goal.lower()
            assert 'how or when they pay' in lowered, goal
            assert 'promise nothing' in lowered, goal
