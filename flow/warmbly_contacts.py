"""
Warmbly contact import (spec stage 5).

The wire contract here was captured from a real request, not inferred from
documentation, using `warmbly contact create --debug`:

    POST {WARMBLY_API_URL}/v1/contacts   -> HTTP 200
    body:     a single contact object, or an array of them
    response: a bare JSON array of created contacts, each with an `id`

Two things the published docs get wrong for this instance, both verified
against the live API:

  * the path is /v1/contacts, not /contacts
  * verification_status="unknown" does not persist -- it comes back as "" --
    so we do not send it rather than appear to have set something

Custom fields must be nested under `custom_fields` and are string-valued. A
top-level field (`-f research_hook=...`) was accepted by the CLI and then
silently dropped by the API, which is why everything non-standard goes into
that object and why every value is stringified here.

A contact is the durable object; assignment to a campaign is a separate step
and is required before anything sends. This module deliberately does not
assign one: that is the human review gate (spec 2.1).
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

CONTACTS_PATH = '/v1/contacts'

# Imported contacts arrive subscribed. Sent explicitly rather than left to the
# API default, so the state does not change silently if that default does.
REQUEST_TIMEOUT = float(os.environ.get('WARMBLY_REQUEST_TIMEOUT', '60'))


def split_person_name(name: Optional[str]) -> tuple:
    """
    Split a full name into (first, rest).

    Everything after the first token is the surname, so 'Mary Jane Watson'
    keeps 'Jane Watson' together rather than discarding a middle name.
    """
    if not name or not name.strip():
        return '', ''
    parts = name.strip().split()
    return parts[0], ' '.join(parts[1:])


def _as_text(value: Any) -> str:
    """Stringify for custom_fields, which stores strings only."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (list, tuple)):
        # Joined rather than JSON-dumped: a human reads these in the Warmbly
        # UI, and ["a","b"] is worse to read than "a; b".
        return '; '.join(_as_text(v) for v in value if v is not None)
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


def build_contact_payloads(
    business: Dict[str, Any],
    research: Dict[str, Any],
    draft: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    One Warmbly contact per address the drafting agent selected.

    The person comes from the research node, which extracts it from the live
    pages, falling back to leads.businesses.decision_maker_name from the
    earlier enrichment pass.
    """
    person = research.get('contact_name') or business.get('decision_maker_name')
    title = research.get('contact_title') or business.get('decision_maker_title')
    first, last = split_person_name(person)

    custom = {
        'draft_subject': draft.get('subject'),
        'draft_body': draft.get('body'),
        'draft_rationale': draft.get('rationale'),
        'research_hook': research.get('personalization_hook'),
        'research_tone': research.get('inferred_tone'),
        'research_confidence': research.get('confidence'),
        'research_pain_signals': research.get('pain_signals'),
        'research_evidence': research.get('evidence'),
        'decision_maker_title': title,
        'leads_business_id': business.get('id'),
        'business_website': business.get('website'),
        'business_city': business.get('city'),
        'business_state': business.get('state'),
        'business_category': business.get('primary_category'),
        'business_icp_tier': business.get('icp_tier'),
        'business_icp_score': business.get('icp_score'),
    }
    # Omit empties rather than writing blank strings into the CRM.
    custom_fields = {k: _as_text(v) for k, v in custom.items()
                     if _as_text(v) != ''}

    payloads = []
    for email in draft.get('selected_emails') or []:
        payloads.append({
            'email': email,
            'first_name': first,
            'last_name': last,
            'company': business.get('business_name') or '',
            'phone': business.get('phone') or '',
            'subscribed': True,
            'custom_fields': dict(custom_fields),
        })
    return payloads


def contact_idempotency_key(
    business: Dict[str, Any],
    payloads: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    A key that dedupes an identical re-run but not a changed draft.

    Keying on the business alone was wrong: Warmbly rejects a reused key whose
    request body differs, with 409 "Idempotency-Key was already used with a
    different request". So once a business had been imported, any later change
    to its draft collided with the earlier key forever.

    Hashing the payload fixes both directions -- the same content retries
    safely, changed content is a new request. The business id stays in the
    key so it is legible in a log or in Warmbly's own records.
    """
    digest = ''
    if payloads:
        canonical = json.dumps(payloads, sort_keys=True, separators=(',', ':'))
        digest = '-' + hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"leads-business-{business.get('id')}{digest}"


def create_contacts(
    payloads: List[Dict[str, Any]],
    idempotency_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    POST contacts to Warmbly and return the created objects.

    Sends the whole batch in one request: the endpoint takes an array and
    answers with an array either way.
    """
    if not payloads:
        return []

    token = os.environ.get('WARMBLY_API_TOKEN')
    if not token:
        raise RuntimeError('WARMBLY_API_TOKEN is not set')
    base = (os.environ.get('WARMBLY_API_URL') or '').rstrip('/')
    if not base:
        raise RuntimeError('WARMBLY_API_URL is not set')

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
    }
    if idempotency_key:
        headers['Idempotency-Key'] = idempotency_key

    response = httpx.post(base + CONTACTS_PATH, json=payloads,
                          headers=headers, timeout=REQUEST_TIMEOUT)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f'Warmbly returned {exc.response.status_code} creating '
            f'{len(payloads)} contact(s): {exc.response.text[:400]}'
        ) from exc

    created = response.json()
    return created if isinstance(created, list) else [created]
