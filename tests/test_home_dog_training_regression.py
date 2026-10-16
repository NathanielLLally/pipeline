"""
The 2026-10-14 run for Home Dog Training (flow run 0d13f644), replayed with
its real data: the three pool addresses with the verdicts mxCheck.pl stored,
the passages of the crawl that matter, and the research the model returned.

What that run got wrong, each asserted fixed here:
  * hello@k9culturedogtraining.com (5.1.1 does not exist; another business)
    reached research, drafting and Warmbly
  * wyatt@homedogtrainingllc.com -- their own domain, a named trainer -- was
    dropped because the page did not print it
  * 'Co-owner / Trainer', on no page, was kept and written back
  * the drafter never saw Laura or Charles
  * the drafter was steered to customer pain and "we run the ads", for a
    business a click-fraud attack had just cost its ad budget
"""

from unittest.mock import patch

from flow.agents.drafting import build_drafting_prompt
from flow.agents.research import research_agent
from flow.schemas import ResearchOutput
from flow.warmbly_contacts import build_contact_payloads

BUSINESS = {
    'id': '1029ae27-4aa8-43f8-ac7a-57cd2bcd44de',
    'business_name': 'Home Dog Training',
    'website': 'https://www.homedogtrainingllc.com/',
    'domain': 'homedogtrainingllc.com', 'city': 'Rockwall', 'state': 'TX',
    'decision_maker_name': 'Charles', 'decision_maker_title': None,
}

POOL = [
    {'email': 'training@homedogtraining.com', 'source': 'crawl',
     'confidence': 65, 'is_role': True, 'verified': False,
     'verification_error': '[Net::SMTP] Connection closed'},
    {'email': 'hello@k9culturedogtraining.com', 'source': 'crawl',
     'confidence': 65, 'is_role': True, 'verified': False,
     'verification_error':
         "5.1.1 The email account that you tried to reach does not exist. "
         "Please try\n5.1.1 double-checking the recipient's email address"},
    {'email': 'wyatt@homedogtrainingllc.com', 'source': 'crawl',
     'confidence': 97, 'is_role': False, 'verified': False,
     'verification_error': 'false positive check failed for mx smtp.google.com'},
]

CRAWL = (
    "--- contact: https://www.homedogtrainingllc.com/book-a-session ---\n"
    "Get in touch 972-245-1587 hello@k9culturedogtraining.com Schedule Your "
    "Complimentary Session Today ... 972-245-3386 Training@HomeDogTraining.com "
    "Rockwall, TX 75087\n\n"
    "--- home: https://www.homedogtrainingllc.com/ ---\n"
    "George is 13 now. The dog who quietly started everything for Laura and "
    "me. ... WHO’S TEACHING: Charles & Wyatt will be leading all group "
    "classes, and Laura will be joining in often as well! ... After a "
    "criminal click-fraud attack drained our ad budget and made operations "
    "impossible, we had to close our Carrollton training facility. ... In "
    "today's world of Amazon, DoorDash, and Uber deliveries arriving "
    "multiple times a week, we emphasize door manners."
)

MODEL_RESEARCH = ResearchOutput(
    business_name='Home Dog Training',
    contacts=[
        {'name': 'Laura', 'title': 'Co-owner / Trainer',
         'is_decision_maker': True,
         'evidence': 'The dog who quietly started everything for Laura and '
                     'me.'},
        {'name': 'Charles', 'title': 'Co-owner / Trainer',
         'is_decision_maker': True},
        {'name': 'Wyatt', 'title': 'Trainer', 'is_decision_maker': False},
    ],
    pain_signals=[
        'Criminal click-fraud attack drained ad budget and forced closure of '
        'Carrollton facility',
        'Clients struggle with door reactivity due to frequent deliveries',
    ],
    personalization_hook='You recently rebuilt after a click-fraud attack.',
    inferred_tone='warm', confidence=0.82,
    evidence=['After a criminal click-fraud attack drained our ad budget and '
              'made operations impossible, we had to close our Carrollton '
              'training facility.'],
    suggested_email='training@homedogtraining.com',
    outreach_angle={
        'problem': 'Click fraud drained their ad budget and closed their '
                   'Carrollton facility; they are rebuilding client flow '
                   'from a smaller home base.',
        'evidence': 'After a criminal click-fraud attack drained our ad '
                    'budget and made operations impossible',
        'caution': 'Paid advertising just cost them a facility; do not '
                   'present running ads as the benefit.',
    },
)

DRAFT = {'selected_emails': ['training@homedogtraining.com'],
         'subject': 's', 'body': 'b', 'rationale': 'r'}


def _research():
    with patch('flow.agents.research.complete_structured',
               return_value=MODEL_RESEARCH) as llm, \
         patch('flow.agents.research.record_decision_maker_sync') as record:
        result = research_agent.fn(BUSINESS, POOL, CRAWL)
    return result, llm.call_args.args[0], record


def test_the_dead_address_never_reaches_the_model():
    result, prompt, _ = _research()

    # The site header still prints it, and research may quote the page; the
    # pool it is told to choose from must not offer it.
    pool = prompt.split('Email pool', 1)[1].split('Site text:', 1)[0]
    assert 'k9culture' not in pool
    assert result['undeliverable_emails'] == ['hello@k9culturedogtraining.com']


def test_wyatts_own_address_survives():
    result, prompt, _ = _research()

    assert 'wyatt@homedogtrainingllc.com' in \
        prompt.split('Email pool', 1)[1].split('Site text:', 1)[0]
    assert [e['email'] for e in result['verified_emails']] == [
        'training@homedogtraining.com', 'wyatt@homedogtrainingllc.com']


def test_invented_titles_are_nulled_and_not_written_back():
    result, _, record = _research()

    titles = {c['name']: c['title'] for c in result['research']['contacts']}
    assert titles['Laura'] is None and titles['Charles'] is None
    # The title was invented; the ownership was not -- the page says so in
    # its own words, and that is what keeps Laura a decision maker.
    flags = {c['name']: c['is_decision_maker']
             for c in result['research']['contacts']}
    assert flags == {'Laura': True, 'Charles': False, 'Wyatt': False}
    assert record.call_args.args[1:3] == ('Laura', None)


def test_the_draft_goes_to_the_shared_inbox_and_greets_laura_not_the_trainer():
    result, _, _ = _research()

    prompt = build_drafting_prompt(result['research'], result['verified_emails'],
                                   website=BUSINESS['website'])

    assert 'Write to: training@homedogtraining.com' in prompt
    assert 'Greet Laura by first name.' in prompt


def test_the_drafter_sees_the_people_the_angle_and_the_statuses():
    result, _, _ = _research()

    prompt = build_drafting_prompt(result['research'], result['verified_emails'])

    assert 'Laura' in prompt and 'Charles' in prompt
    assert 'Click fraud drained their ad budget' in prompt
    assert 'do not present running ads as the benefit' in prompt
    assert 'we run the ads' not in prompt.lower()
    assert 'k9culture' not in prompt
    assert 'catch_all' in prompt


def test_import_creates_no_contact_for_the_other_business():
    result, _, _ = _research()

    payloads = build_contact_payloads(BUSINESS, result['research'], DRAFT,
                                      candidate_emails=POOL)

    emails = [p['email'] for p in payloads]
    assert 'hello@k9culturedogtraining.com' not in emails
    wyatt = next(p for p in payloads
                 if p['email'] == 'wyatt@homedogtrainingllc.com')
    assert wyatt['first_name'] == 'Wyatt'
