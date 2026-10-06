"""
The import-contacts-agent deployment.

Takes drafting output (with research and business context), builds Warmbly
contact payloads, and imports them. Can be triggered standalone or as part of
the pipeline.

Can be called with dict params or from a JSON input artifact:
  import_contacts(business={...}, research={...}, draft={...}, ...)
  import_contacts(json_input_file='biz-1-import-input.json')

Writes output artifacts tagged by flow run tags.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from prefect import flow

from flow.artifacts import read_artifact, write_artifact
from flow.warmbly_contacts import (
    build_contact_payloads,
    contact_idempotency_key,
    create_contacts,
)


@flow(log_prints=True)
def import_contacts(
    business: Optional[Dict[str, Any]] = None,
    research: Optional[Dict[str, Any]] = None,
    draft: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    json_input_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build and import contacts to Warmbly for a researched & drafted business.

    Can be called with explicit dict params or from a JSON input artifact:
      import_contacts(business={...}, research={...}, draft={...})
      import_contacts(json_input_file='biz-1-import-input.json')

    If json_input_file is provided, it takes precedence and dict params are ignored.
    If idempotency_key is not provided, one is derived from business.id.

    Writes output and input artifacts to the current directory, tagged by flow run tags.

    Returns:
        {'status': 'imported', 'payloads_sent': N, 'created': [...contact dicts...]}
        or
        {'status': 'rejected', 'reason': '...', 'error': '...'}
    """
    # Load from JSON if provided
    if json_input_file:
        try:
            input_data = read_artifact(json_input_file, suffix='input')
            business = input_data.get('business')
            research = input_data.get('research')
            draft = input_data.get('draft')
            idempotency_key = input_data.get('idempotency_key')
        except Exception as e:
            print(f'Failed to load input artifact: {e}')
            raise
    else:
        # Validate that required params are present
        if business is None or research is None or draft is None:
            raise ValueError(
                'Either json_input_file or (business, research, draft) required'
            )
        input_data = {
            'business': business,
            'research': research,
            'draft': draft,
            'idempotency_key': idempotency_key,
        }

    # Write input artifact
    try:
        write_artifact(input_data, suffix='input')
    except Exception as e:
        print(f'Warning: could not write input artifact: {e}')

    business_id = business.get('id', 'unknown')
    business_name = business.get('business_name', 'unknown')

    # Build payloads
    try:
        payloads = build_contact_payloads(business, research, draft)
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

    # Derived here, not earlier: the key hashes the payload so an identical
    # re-run dedupes while a changed draft is a new request. Deriving it from
    # the business alone caused 409 "Idempotency-Key was already used with a
    # different request" on every re-import after a draft changed.
    if not idempotency_key:
        idempotency_key = contact_idempotency_key(business, payloads)

    print(f'Importing {len(payloads)} contact(s) for {business_name} '
          f'(business_id={business_id})')

    # Send to Warmbly
    try:
        created = create_contacts(payloads, idempotency_key=idempotency_key)
    except Exception as e:
        print(f'Warmbly import failed: {e}')
        result = {'status': 'rejected', 'reason': 'warmbly_error', 'error': str(e)}
        write_artifact(result, suffix='output')
        return result

    print(f'Successfully imported {len(created)} contact(s)')
    result = {
        'status': 'imported',
        'business_id': business_id,
        'business_name': business_name,
        'payloads_sent': len(payloads),
        'created': created,
        'idempotency_key': idempotency_key,
    }
    write_artifact(result, suffix='output')
    return result
