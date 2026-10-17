import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.agents.analysis import qa_metrics


def test_qa_lives_in_the_analysis_module():
    """draft_qa was merged into flow.agents.analysis; the old module is gone."""
    import importlib
    import pytest
    from flow.agents import analysis

    assert qa_metrics.__module__ == analysis.__name__
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module('flow.draft_qa')

# The draft that reached Warmbly for Maximum Canine on 2026-10-06: the model
# drifted into Thai mid-word and stopped.
TRUNCATED = (
    'Your new Riverhead location at Hydrant Pet Hotel and Deer Park facility '
    'both run $749+ evaluations that testimonials show prospects hesitate on '
    'due to cost. We deliver live, qualified Long Island dog-owner leads who '
    'are actively engaged, so you only payต่อ'
)

GOOD_BODY = (
    'Noticed your board-and-train program books out three weeks ahead. '
    'We send live, qualified dog-owner leads in your area, and you only pay '
    'for the ones you accept.\n\n'
    'Worth a look at ten sample leads this week?'
)
GOOD = {'subject': 'Dog-owner leads for Riverhead', 'body': GOOD_BODY}


def _qa(subject='Dog-owner leads for Riverhead', body=GOOD_BODY, **business):
    return qa_metrics({'subject': subject, 'body': body}, business)


class TestCleanDraft:
    def test_a_finished_draft_passes(self):
        qa = _qa()

        assert qa['passed'] is True
        assert qa['failures'] == []


class TestTruncation:
    def test_the_maximum_canine_draft_fails(self):
        qa = _qa(body=TRUNCATED)

        assert qa['passed'] is False
        assert 'non_latin_script' in qa['failures']
        assert 'unterminated_body' in qa['failures']

    def test_ending_mid_sentence_is_unterminated(self):
        qa = _qa(body='We deliver qualified leads, so you only pay')

        assert qa['ends_with_terminal_punctuation'] is False
        assert 'unterminated_body' in qa['failures']

    def test_a_short_sign_off_line_is_not_a_cutoff(self):
        """Seen on prod: '...we'll take it from there.\\n\\nThanks'."""
        qa = _qa(body=GOOD_BODY + '\n\nThanks')

        assert qa['ends_with_terminal_punctuation'] is True

    def test_a_cutoff_on_its_own_line_still_fails(self):
        qa = _qa(body=GOOD_BODY + '\n\nWe also deliver leads for grooming and')

        assert 'unterminated_body' in qa['failures']

    def test_closing_quote_or_paren_after_the_stop_is_fine(self):
        qa = _qa(body='Reply "yes" and I will send them (no strings.)')

        assert qa['ends_with_terminal_punctuation'] is True


class TestScript:
    def test_non_latin_text_is_reported_with_its_characters(self):
        qa = _qa(subject='Leads ต่อ you')

        assert qa['non_latin_chars'] == sorted('ต่อ')
        assert 'non_latin_script' in qa['failures']

    def test_accents_curly_quotes_and_dashes_are_not_flagged(self):
        qa = _qa(body='Café owners — “premium” grooming ’s fine.')

        assert qa['non_latin_chars'] == []


class TestSignOff:
    """The campaign template appends {{.Signature}}, so a drafted closing line
    prints above it: 'Best regards,' then the signature block."""

    def test_a_trailing_sign_off_fails(self):
        for closing in ('Thank you,', 'Best regards,', 'Thanks', 'Cheers!'):
            qa = _qa(body=GOOD_BODY + '\n\n' + closing)

            assert 'sign_off' in qa['failures'], closing

    def test_a_sign_off_followed_by_a_name_fails(self):
        """'Best,\nNate' -- the name line hid the closing above it."""
        qa = _qa(body=GOOD_BODY + '\n\nBest,\nNate')
        assert 'sign_off' in qa['failures']

    def test_a_body_ending_on_the_ask_has_no_sign_off(self):
        assert 'sign_off' not in _qa()['failures']


class TestLeftovers:
    def test_html_entities_are_flagged(self):
        qa = _qa(subject='Leads for Deer Park &amp; Riverhead')

        assert qa['html_entities'] == ['&amp;']
        assert 'html_entity' in qa['failures']

    def test_markdown_is_flagged(self):
        qa = _qa(body='**Live leads** for you.\n\n## Why\n- cheap.')

        assert 'markdown' in qa['failures']

    def test_an_empty_body_fails(self):
        qa = _qa(body='   ')

        assert 'empty_body' in qa['failures']
        assert qa['passed'] is False


class TestLength:
    def test_counts_words_and_subject_length(self):
        qa = _qa(subject='Five words in this subject', body='One two three four.')

        assert qa['body_words'] == 4
        assert qa['subject_chars'] == len('Five words in this subject')

    def test_an_overlong_subject_fails(self):
        qa = _qa(subject='x' * 200)

        assert 'subject_too_long' in qa['failures']

    def test_a_body_too_short_to_be_an_email_fails(self):
        qa = _qa(body='Hi.')

        assert 'body_too_short' in qa['failures']

    def test_limits_come_from_env(self, monkeypatch):
        monkeypatch.setenv('QA_MAX_SUBJECT_CHARS', '10')

        qa = _qa(subject='Eleven chars')

        assert 'subject_too_long' in qa['failures']


class TestCallToAction:
    def test_a_question_counts_as_an_ask(self):
        assert _qa()['has_question'] is True

    def test_a_body_with_no_question_is_reported_not_failed(self):
        """Every scored draft so far lacks a CTA; that is the analysis
        dimension's job to grade, so QA reports it rather than failing."""
        qa = _qa(body=GOOD_BODY.replace('Worth a look at ten sample leads '
                                        'this week?', 'Reply and I will send '
                                        'ten sample leads this week.'))

        assert qa['has_question'] is False
        assert qa['passed'] is True


class TestBusinessName:
    def test_reports_whether_the_business_is_named(self):
        named = _qa(business_name='Maximum Canine',
                    body='Maximum Canine books out weeks ahead. Want leads?')
        unnamed = _qa(business_name='Maximum Canine')

        assert named['mentions_business_name'] is True
        assert unnamed['mentions_business_name'] is False

    def test_unknown_business_name_is_none_not_false(self):
        assert _qa()['mentions_business_name'] is None


class TestAttachedToAnalysis:
    """QA runs whether or not the LLM half does: it needs no model."""

    RESEARCH = {'personalization_hook': 'hook', 'pain_signals': [],
                'inferred_tone': 'warm'}
    BUSINESS = {'id': 'b1', 'business_name': 'Maximum Canine'}

    def test_present_on_a_failed_llm_analysis(self):
        from flow.agents.analysis import analysis_agent
        from flow.llm import LLMSchemaError

        with patch('flow.agents.analysis.complete_structured',
                   side_effect=LLMSchemaError('never validated')), \
             patch('flow.agents.analysis.write_artifact'), \
             patch('flow.agents.analysis.register_artifact'), \
             patch('flow.agents.analysis._set_analysis_output',
                   side_effect=lambda r, _id: r):
            result = analysis_agent.fn(
                research=self.RESEARCH, business=self.BUSINESS,
                draft={'subject': 'Leads', 'body': TRUNCATED})

        assert result['status'] == 'error'
        assert result['qa']['passed'] is False
        assert 'unterminated_body' in result['qa']['failures']
