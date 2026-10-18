"""
The import-campaign deployment: a variant of import-contacts.

Imports the same Warmbly contacts, but instead of a Unibox compose draft it
creates a campaign named after the business -- filed in the 'initial'
folder, sending from the mailboxes tagged "october '26 htpc primary" --
whose first step is the drafted email, and enrolls the drafted contact as
its one lead. The campaign is left a draft: nothing sends until someone
starts it.

Can be called with dict params or from a JSON input artifact:
  import_campaign(business={...}, research={...}, draft={...}, ...)
  import_campaign(json_input_file='biz-1-import-input.json')

Writes output artifacts tagged by flow run tags.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.agents.import_contacts import drafted_only
from flow.artifacts import read_artifact, write_artifact
from flow.warmbly_contacts import (
    build_campaign,
    build_contact_payloads,
    contact_idempotency_key,
    create_campaign,
    create_contacts,
)


@flow(log_prints=True)
def import_campaign(
    business: Optional[Dict[str, Any]] = None,
    research: Optional[Dict[str, Any]] = None,
    draft: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    candidate_emails: Optional[List[Dict[str, Any]]] = None,
    json_input_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import a business's contacts and give it its own Warmbly campaign.

    Same inputs as import_contacts. Only addresses the draft is written to
    are imported (import_contacts.drafted_only); the first is enrolled, any
    other selected address is imported outside the campaign.

    Returns:
        {'status': 'imported', 'campaign_status': 'created'|'existing'|'error',
         'campaign_id': ..., 'enrolled': [...], 'not_enrolled': [...],
         'created': [...contact dicts...], ...}
        or
        {'status': 'rejected', 'reason': '...', 'error': '...'}
    """
    if json_input_file:
        try:
            input_data = read_artifact(json_input_file, suffix='input')
            business = input_data.get('business')
            research = input_data.get('research')
            draft = input_data.get('draft')
            idempotency_key = input_data.get('idempotency_key')
            candidate_emails = input_data.get('candidate_emails')
        except Exception as e:
            print(f'Failed to load input artifact: {e}')
            raise
    else:
        if business is None or research is None or draft is None:
            raise ValueError(
                'Either json_input_file or (business, research, draft) required'
            )
        input_data = {
            'business': business,
            'research': research,
            'draft': draft,
            'idempotency_key': idempotency_key,
            'candidate_emails': candidate_emails,
        }

    try:
        write_artifact(input_data, suffix='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    business_id = business.get('id', 'unknown')
    business_name = business.get('business_name', 'unknown')

    # Only addresses the draft is written to, as in import_contacts.
    try:
        payloads = build_contact_payloads(
            business, research, draft,
            candidate_emails=drafted_only(candidate_emails, draft))
    except Exception as e:
        print(f'Failed to build contact payloads: {e}')
        result = {'status': 'rejected', 'reason': 'payload_build_failed', 'error': str(e)}
        write_artifact(result, suffix='output')
        return result

    if not payloads:
        print('No email addresses to import')
        result = {'status': 'rejected', 'reason': 'no_emails', 'payloads_sent': 0}
        write_artifact(result, suffix='output')
        return result

    # The campaign first: a contact joins a campaign through the `campaigns`
    # ids on its create, which adds the lead without touching any other
    # campaign it is in (update would replace the whole list). A campaign
    # failure is reported rather than losing the import, as import_contacts
    # does with its draft.
    campaign_status, campaign, campaign_error = 'created', None, None
    try:
        campaign, created_new = create_campaign(build_campaign(business, draft))
        campaign_status = 'created' if created_new else 'existing'
        print(f"Campaign {campaign_status}: {campaign.get('name')!r} "
              f"({campaign.get('id')})")
    except Exception as e:
        campaign_status, campaign_error = 'error', str(e)
        print(f'Warmbly campaign creation failed: {e}')

    # One lead: the first address drafting selected, which leads payloads.
    selected = [e.lower() for e in draft.get('selected_emails') or []]
    enrolled, not_enrolled = [], []
    for p in payloads:
        if p['email'].lower() not in selected:
            continue
        if campaign and not enrolled:
            p['campaigns'] = [campaign['id']]
            enrolled.append(p['email'])
        else:
            not_enrolled.append(p['email'])

    # Derived after enrollment, so the key covers the campaign too.
    if not idempotency_key:
        idempotency_key = contact_idempotency_key(business, payloads)

    print(f'Importing {len(payloads)} contact(s) for {business_name} '
          f'(business_id={business_id})')

    campaign_fields = {
        'campaign_status': campaign_status,
        'campaign_id': campaign.get('id') if campaign else None,
        'campaign_name': campaign.get('name') if campaign else None,
    }
    if campaign_error:
        campaign_fields['campaign_error'] = campaign_error

    try:
        created = create_contacts(payloads, idempotency_key=idempotency_key)
    except Exception as e:
        print(f'Warmbly import failed: {e}')
        result = {'status': 'rejected', 'reason': 'warmbly_error',
                  'error': str(e), **campaign_fields}
        write_artifact(result, suffix='output')
        return result

    print(f'Successfully imported {len(created)} contact(s); '
          f'enrolled {enrolled or "none"}')

    result = {
        'status': 'imported',
        'business_id': business_id,
        'business_name': business_name,
        'payloads_sent': len(payloads),
        'created': created,
        'idempotency_key': idempotency_key,
        **campaign_fields,
        'enrolled': enrolled,
        'not_enrolled': not_enrolled,
    }
    write_artifact(result, suffix='output')
    return result
