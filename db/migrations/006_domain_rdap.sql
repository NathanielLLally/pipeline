-- RDAP probe results, one row per domain.
--
-- RDAP is the registry's structured replacement for WHOIS: a free, JSON, rate-limited
-- but unblocked lookup of who registered a domain. It is a genuinely different
-- acquisition channel from the website crawl -- it asks the registry, not the site --
-- so it reaches businesses whose sites publish no address at all.
--
-- The measured yield is low. A 100-domain sample answered for 92, of which 23 were
-- behind a privacy proxy (Domains By Proxy and similar), 62 exposed no contact entity
-- at all because ICANN's post-GDPR policy lets registrars redact it, and 7 carried an
-- address -- 4 of those usable after junk filtering. So expect roughly 3-4%. That is
-- worth running exactly once across the corpus and never worth re-running blind,
-- which is what this table is for.
--
-- Recording the misses is the point. Without it every re-run would re-probe ~1,100
-- domains to rediscover the same 60 redactions, and the registries would rightly
-- throttle us for it.
CREATE TABLE IF NOT EXISTS leads.domain_rdap (
  domain         text PRIMARY KEY,     -- registrable host, lowercased, no www.

  -- What the probe concluded. Only definitive outcomes are written:
  --   found     -- a usable contact address was extracted
  --   privacy   -- answered, but the contact is a privacy/proxy service
  --   redacted  -- answered, but carries no contact entity (the common case)
  --   junk      -- answered with an address, but it was the registrar's own or a
  --               placeholder, so nothing usable came of it
  --   notfound  -- the registry says the domain does not exist
  -- A timeout, a 429 or a transport failure asked the registry nothing and is NOT
  -- stored, so the next run retries it rather than recording a permanent miss.
  outcome        text NOT NULL,

  email          text,                 -- the extracted address, when outcome='found'
  registrant     text,                 -- vcard fn/org, for auditing what was matched
  rdap_url       text,                 -- the exact endpoint that answered
  http_status    integer,
  probed_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS domain_rdap_outcome_idx ON leads.domain_rdap (outcome);

COMMENT ON TABLE leads.domain_rdap IS
  'One row per domain probed via RDAP. Records misses as well as hits so the pass is '
  'resumable and never re-asks a registry a question it has already answered. '
  'Transport failures and rate-limit responses are deliberately not recorded.';
