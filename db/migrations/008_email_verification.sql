-- Email verification verdicts from scripts/mxCheck.pl.
--
-- For each address in leads.business_email, mxCheck.pl connects to the domain's
-- MX host, issues MAIL FROM / RCPT TO, and disconnects before DATA. Nothing is
-- ever sent; the mailbox owner observes only a connection. The result is stored
-- here: whether the mailbox accepted the RCPT (verified=true), rejected it
-- (verified=false, error set), or accepted everything at the domain because it's
-- a catch-all (verified=false, error="false positive check failed for mx ...").
--
-- One row per address per verification run. If the same address is re-verified
-- later, the unique index allows an upsert to replace the prior result.
--
-- Addresses with matches_site_domain=true are the highest priority to verify
-- first, since those are almost always deliverable. Free-mail addresses (gmail,
-- yahoo) cannot be verified against the site's domain but can still be checked
-- against the mail provider's SMTP server. Role addresses (info@, admin@) are
-- organizational, not personal, and deliver successfully when verified.

CREATE TABLE IF NOT EXISTS leads.email_verification (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_email_id  uuid NOT NULL REFERENCES leads.business_email(id) ON DELETE CASCADE,
  business_id        uuid NOT NULL REFERENCES leads.businesses(id) ON DELETE CASCADE,

  email              text NOT NULL,        -- denormalized for auditing
  verified           boolean NOT NULL,     -- true: mailbox exists; false: rejected or catch-all
  mx_server          text,                 -- lowest-preference MX this ran against
  error              text,                 -- if verified=false, why (e.g., SMTP rejection, no MX, catch-all)

  verified_at        timestamptz NOT NULL DEFAULT now()
);

-- One row per address per verification. A re-verification of the same address
-- replaces the prior result in place via an upsert.
CREATE UNIQUE INDEX IF NOT EXISTS email_verification_business_email_uidx
  ON leads.email_verification (business_email_id);

CREATE INDEX IF NOT EXISTS email_verification_business_idx ON leads.email_verification (business_id);
CREATE INDEX IF NOT EXISTS email_verification_verified_idx ON leads.email_verification (verified);

COMMENT ON TABLE leads.email_verification IS
  'SMTP-level verdicts for addresses in leads.business_email. Each row is a probe '
  'to the domain MX: the script opens a session, issues RCPT TO for a dummy local '
  'part first (to detect catch-all hosts), then RCPT TO for the real address, and '
  'disconnects before DATA so nothing is ever delivered. verified=true means the '
  'mailbox accepted RCPT. verified=false with error="false positive..." means the '
  'MX accepted the dummy address too, so it is a catch-all and the real address '
  'is unverifiable. verified=false with other error means the mailbox was rejected '
  'by SMTP (e.g., "user unknown"). No message was ever sent in any case.';

-- Rolled up onto businesses for quick filtering (e.g., "give me the 100 verified
-- Tier 1 prospects"). Mirrors the pattern of contact_email and contact_email_count.
ALTER TABLE leads.businesses
  ADD COLUMN IF NOT EXISTS email_verified_count integer,
  ADD COLUMN IF NOT EXISTS email_unverifiable_count integer,
  ADD COLUMN IF NOT EXISTS email_rejected_count integer;

COMMENT ON COLUMN leads.businesses.email_verified_count IS
  'Count of addresses in leads.email_verification with verified=true for this business.';
COMMENT ON COLUMN leads.businesses.email_unverifiable_count IS
  'Count of addresses whose MX is a catch-all, so verification was inconclusive '
  '(error contains "false positive").';
COMMENT ON COLUMN leads.businesses.email_rejected_count IS
  'Count of addresses SMTP-rejected outright (verified=false and not a catch-all).';
