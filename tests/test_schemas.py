import pytest
from pydantic import ValidationError

from flow.schemas import DraftingOutput, ResearchOutput, VerifiedEmail


def _research(**over):
    base = dict(
        business_name="Happy Tails", pain_signals=["no online booking"],
        personalization_hook="they book by phone only",
        inferred_tone="warm", confidence=0.8, evidence=["Call us to book"],
        suggested_email="owner@happytails.com",
    )
    base.update(over)
    return base


class TestResearchOutput:
    def test_accepts_a_complete_payload(self):
        out = ResearchOutput(**_research())
        assert out.confidence == 0.8
        assert out.next_url_to_check is None

    def test_rejects_tone_outside_the_vocabulary(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(inferred_tone="enthusiastic"))

    def test_rejects_confidence_above_one(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(confidence=1.4))

    def test_rejects_confidence_below_zero(self):
        with pytest.raises(ValidationError):
            ResearchOutput(**_research(confidence=-0.1))

    def test_coerces_numeric_string_confidence(self):
        """Models often return numbers as strings; that is not a failure."""
        assert ResearchOutput(**_research(confidence="0.75")).confidence == 0.75


class TestVerifiedEmail:
    def test_mx_server_is_optional(self):
        ve = VerifiedEmail(
            email="a@b.com", verified_at="2026-10-03T00:00:00Z",
            source="leads.business_email",
        )
        assert ve.mx_server is None


class TestDraftingOutput:
    def test_requires_at_least_one_selected_email(self):
        with pytest.raises(ValidationError):
            DraftingOutput(selected_emails=[], subject="s", body="b",
                           rationale="r")


class TestNoPlaceholders:
    """A draft containing [Your Name] is not a finished email.

    Rejecting it in the schema means complete_structured's retry loop re-asks
    the model with the validation error attached, rather than the pipeline
    passing an obviously unfinished draft downstream.
    """

    def _draft(self, **over):
        base = dict(selected_emails=['a@b.com'], subject='Hello',
                    body='Noticed you take bookings by phone only.',
                    rationale='booking friction')
        base.update(over)
        return base

    def test_accepts_finished_copy(self):
        assert DraftingOutput(**self._draft()).subject == 'Hello'

    def test_rejects_a_placeholder_in_the_body(self):
        with pytest.raises(ValidationError):
            DraftingOutput(**self._draft(body='Hi there,\n\nBest,\n[Your Name]'))

    def test_rejects_a_placeholder_in_the_subject(self):
        with pytest.raises(ValidationError):
            DraftingOutput(**self._draft(subject='A note for [Business]'))

    def test_rejects_curly_brace_placeholders_too(self):
        with pytest.raises(ValidationError):
            DraftingOutput(**self._draft(body='Hi {{first_name}}, quick note.'))

    def test_tolerates_incidental_brackets(self):
        """Real prose occasionally brackets an aside; do not reject that."""
        body = ('Noticed your site [the services page especially] leans on '
                'phone bookings.')
        assert DraftingOutput(**self._draft(body=body)).body == body
