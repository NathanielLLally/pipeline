"""
What the pipeline knows about a pool address, beyond the address itself.

No I/O, so research, drafting and the Warmbly import can all share it.

A pool entry's `confidence` is how sure the scraper was that it read an
address off a page -- not whether mail to it arrives. That is `verified`,
from mxCheck.pl, and a boolean is not enough: False covers three different
outcomes, and only the stored error says which (scripts/mxCheck.pl):

  invalid    the server refused the mailbox (5.1.x, 5.2.x, 5.4.1), there is
             no MX for the domain, or the address is malformed
  catch_all  the server accepted a random decoy first ("false positive check
             failed"), so it accepts anything and nothing is known
  unknown    the check did not finish: connection closed or refused, an
             exception, or a 5.7.x policy refusal of *our* probe

plus `valid` (the server accepted it) and `unchecked` (no verdict yet).
Only `invalid` is a reason to drop an address.
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

STATUSES = ('valid', 'invalid', 'catch_all', 'unknown', 'unchecked')

# Enhanced status codes that say the mailbox, not the probe, is the problem:
# 5.1.x bad address, 5.2.x mailbox disabled or full, and 5.4.1, which
# Microsoft 365 returns for a recipient not in the tenant's directory.
_PERMANENT = re.compile(r'^\s*5\.(1\.\d+|2\.\d+|4\.1)\b')
_NO_SUCH_USER = re.compile(
    r'no mx record|invalid email format|does not exist|no such user|'
    r'user unknown|unknown user|mailbox not found|recipient not found',
    re.IGNORECASE)
_CATCH_ALL = 'false positive check failed'

STATUS_NOTES = {
    'valid': 'the mailbox accepted it',
    'catch_all': 'the domain accepts any address, so unconfirmed',
    'unknown': 'the check did not finish',
    'unchecked': 'not checked',
    'invalid': 'the mailbox does not exist',
}

FREE_MAIL_DOMAINS = frozenset({
    'gmail.com', 'googlemail.com', 'yahoo.com', 'ymail.com', 'hotmail.com',
    'outlook.com', 'live.com', 'msn.com', 'icloud.com', 'me.com', 'mac.com',
    'aol.com', 'comcast.net', 'att.net', 'sbcglobal.net', 'verizon.net',
    'protonmail.com', 'proton.me', 'gmx.com', 'mail.com', 'zoho.com',
})


def deliverability(entry: Dict[str, Any]) -> str:
    """One of STATUSES for a pool entry."""
    verified = entry.get('verified')
    if verified is True:
        return 'valid'
    if verified is None:
        return 'unchecked'
    error = entry.get('verification_error') or ''
    if _CATCH_ALL in error:
        return 'catch_all'
    if _PERMANENT.search(error) or _NO_SUCH_USER.search(error):
        return 'invalid'
    return 'unknown'


def email_domain(email: str) -> str:
    """The lowercased domain of an address, or '' if it has none."""
    _, at, domain = (email or '').strip().rpartition('@')
    return domain.lower() if at and domain else ''


def site_domain(business: Dict[str, Any]) -> str:
    """The business's own domain: the domain column, else the website host."""
    domain = (business.get('domain') or '').strip().lower()
    if not domain:
        website = (business.get('website') or '').strip()
        if website and '//' not in website:
            website = '//' + website
        domain = (urlparse(website).hostname or '').lower()
    return domain[4:] if domain.startswith('www.') else domain


def is_free_mail(domain: str) -> bool:
    return (domain or '').lower() in FREE_MAIL_DOMAINS


def describe_address(entry: Dict[str, Any], business: Dict[str, Any]) -> str:
    """One prompt line: the address, what the check found, what kind it is."""
    status = deliverability(entry)
    notes = [f'{status}: {STATUS_NOTES[status]}']
    if entry.get('is_role') is True:
        notes.append('shared inbox')
    elif entry.get('is_role') is False:
        notes.append("a person's own address")
    domain = email_domain(entry.get('email') or '')
    own = site_domain(business)
    if own and domain and domain != own and not is_free_mail(domain):
        notes.append("not on their website's domain")
    return f"{entry.get('email')} ({'; '.join(notes)})"


def person_for(email: str, contacts: List[Dict[str, Any]]
               ) -> Optional[Dict[str, Any]]:
    """
    The named contact an address belongs to: the one research tied it to,
    else the one whose first name is the local part's first segment
    (wyatt@, wyatt.smith@). None for a shared inbox or anyone unnamed.
    """
    address = (email or '').lower()
    for c in contacts or []:
        if (c.get('email') or '').lower() == address:
            return c
    local = re.split(r'[._+-]', address.split('@', 1)[0])[0]
    for c in contacts or []:
        first = (c.get('name') or '').split(' ')[0].lower()
        if len(first) >= 3 and first == local:
            return c
    return None


def address_rank(entry: Dict[str, Any], contacts: List[Dict[str, Any]]
                 ) -> Tuple[int, int]:
    """
    Lower is better. First what the email check found -- an accepted mailbox
    beats everything unconfirmed, a dead one is last -- then whose it is: a
    decision maker's own address, then a shared inbox, then anyone else's.
    """
    status = deliverability(entry)
    confidence = 0 if status == 'valid' else 2 if status == 'invalid' else 1
    owner = person_for(entry.get('email') or '', contacts)
    if owner is not None and owner.get('is_decision_maker'):
        kind = 0
    elif owner is None and entry.get('is_role') is not False:
        kind = 1
    else:
        kind = 2
    return confidence, kind


def choose_recipient(pool: List[Dict[str, Any]],
                     contacts: List[Dict[str, Any]]
                     ) -> Optional[Dict[str, Any]]:
    """The best-ranked address; the earliest wins a tie. None if no pool."""
    if not pool:
        return None
    return min(pool, key=lambda e: address_rank(e, contacts))


def greeting_name(email: str, contacts: List[Dict[str, Any]]) -> Optional[str]:
    """
    Who the email greets: the person the address belongs to; for a shared
    inbox, the first decision maker. Otherwise nobody -- whoever happens to
    be listed first could be a trainer.
    """
    person = person_for(email, contacts) or next(
        (c for c in contacts or [] if c.get('is_decision_maker')), None)
    if person is None or not (person.get('name') or '').strip():
        return None
    return person['name'].split()[0]
