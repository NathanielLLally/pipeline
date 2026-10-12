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
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

CONTACTS_PATH = '/v1/contacts'
# Compose drafts: what Warmbly's Drafts view lists. Owned by the API key's
# user, and keyed by a client-generated id, so the PUT is idempotent.
DRAFTS_PATH = '/v1/unibox/drafts'
# Fixed, so a business + address always maps to the same draft id and a
# re-run updates that draft instead of adding another.
_DRAFT_NAMESPACE = uuid.UUID('6f1c2b9e-5d3a-4c7e-9a41-2b8d0e7f3c55')

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
    candidate_emails: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    One Warmbly contact per address drafting selected, then one per remaining
    address in the research pool.

    Only the selected contacts carry the draft. The rest are imported without
    it and without our verification verdict: Warmbly verifies on its own and
    may judge deliverability differently.

    The person comes from the research node, which extracts it from the live
    pages, falling back to leads.businesses.decision_maker_name from the
    earlier enrichment pass.
    """
    person = research.get('contact_name') or business.get('decision_maker_name')
    title = research.get('contact_title') or business.get('decision_maker_title')
    first, last = split_person_name(person)

    draft_custom = {
        'draft_subject': draft.get('subject'),
        'draft_body': draft.get('body'),
        'draft_rationale': draft.get('rationale'),
    }
    custom = {
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

    def non_empty(fields: Dict[str, Any]) -> Dict[str, str]:
        # Omit empties rather than writing blank strings into the CRM.
        return {k: _as_text(v) for k, v in fields.items() if _as_text(v) != ''}

    def payload(email: str, fields: Dict[str, str]) -> Dict[str, Any]:
        return {
            'email': email,
            'first_name': first,
            'last_name': last,
            'company': business.get('business_name') or '',
            'phone': business.get('phone') or '',
            'subscribed': True,
            'custom_fields': fields,
        }

    drafted_fields = non_empty({**draft_custom, **custom})
    pool_fields = non_empty(custom)

    payloads = []
    seen = set()
    for email in draft.get('selected_emails') or []:
        seen.add(email.lower())
        payloads.append(payload(email, dict(drafted_fields)))
    for entry in candidate_emails or []:
        email = (entry.get('email') or '').strip()
        if email and email.lower() not in seen:
            seen.add(email.lower())
            payloads.append(payload(email, dict(pool_fields)))
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


def _warmbly_request_parts() -> tuple:
    """The API base URL and auth headers, or an explicit error if unset."""
    token = os.environ.get('WARMBLY_API_TOKEN')
    if not token:
        raise RuntimeError('WARMBLY_API_TOKEN is not set')
    base = (os.environ.get('WARMBLY_API_URL') or '').rstrip('/')
    if not base:
        raise RuntimeError('WARMBLY_API_URL is not set')
    return base, {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
    }


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

    base, headers = _warmbly_request_parts()
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


def compose_draft_id(business_id: Any, email: str) -> str:
    """A stable draft id for one business and one address."""
    return str(uuid.uuid5(_DRAFT_NAMESPACE,
                          f'{business_id}:{email.strip().lower()}'))


def build_compose_drafts(
    business: Dict[str, Any],
    draft: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    One compose draft per address the drafting agent selected, as
    [{'id': ..., 'body': {to, subject, body}}].

    No mailbox is named: Warmbly picks one when the draft is sent, as it does
    for a compose with email_account_id omitted.
    """
    return [
        {
            'id': compose_draft_id(business.get('id'), email),
            'body': {
                'to': [email],
                'subject': draft.get('subject') or '',
                'body': draft.get('body') or '',
            },
        }
        for email in draft.get('selected_emails') or []
    ]


def create_compose_drafts(drafts: List[Dict[str, Any]]) -> List[str]:
    """PUT each draft to Warmbly's Unibox Drafts and return their ids."""
    if not drafts:
        return []
    base, headers = _warmbly_request_parts()

    ids = []
    for d in drafts:
        response = httpx.put(f"{base}{DRAFTS_PATH}/{d['id']}", json=d['body'],
                             headers=headers, timeout=REQUEST_TIMEOUT)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f'Warmbly returned {exc.response.status_code} creating '
                f"draft {d['id']}: {exc.response.text[:400]}"
            ) from exc
        ids.append(d['id'])
    return ids
