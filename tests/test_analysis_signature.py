import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.agents.analysis import build_analysis_prompt

RESEARCH = {'personalization_hook': 'click fraud closed their facility',
            'pain_signals': ['lost ad budget'], 'inferred_tone': 'warm'}
DRAFT = {'subject': 'Screened inquiries in Rockwall',
         'body': 'Hi Laura,\n\nWe send screened local dog owners.\n\nThanks,',
         'rationale': 'Opens with their story.'}
BUSINESS = {'business_name': 'Home Dog Training'}


class TestSignatureContext:
    """The campaign template appends {{.Signature}} after the body. Not told,
    the model scored a draft 3/10 on trust for having no sender."""

    def _prompt(self):
        return build_analysis_prompt(RESEARCH, DRAFT, BUSINESS)

    def test_the_model_is_told_a_signature_follows_the_body(self):
        prompt = self._prompt()
        assert 'signature' in prompt.lower()
        assert 'after the body' in prompt.lower()

    def test_a_missing_sender_is_not_to_be_scored_down(self):
        prompt = self._prompt().lower()
        assert 'do not score' in prompt
        assert 'sender' in prompt

    def test_a_sign_off_in_the_body_is_named_a_defect(self):
        """qa_metrics fails 'sign_off' for the same reason: it would sit
        above the signature."""
        prompt = self._prompt().lower()
        assert 'sign-off' in prompt
        assert 'defect' in prompt

    def test_the_note_comes_before_the_scoring_rubric(self):
        prompt = self._prompt()
        assert prompt.lower().index('signature') < prompt.index('1. PERSONALIZATION')
