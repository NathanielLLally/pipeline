-- Which businesses the agent pipeline (flow/run_agents.py) has taken, and how
-- each one ended.
--
-- Without this, batch selection has no memory: every run re-selects the same
-- top-scored businesses, re-researches them and re-imports them to Warmbly.
-- With it, selection skips any business that has a row, unless that row is
-- 'failed' (retryable) or a 'claimed' row older than the stale-claim window
-- (a run that died mid-batch).
--
-- Claiming is one INSERT ... ON CONFLICT per batch, so two runs that select
-- the same businesses at the same moment cannot both take one: the loser's
-- RETURNING simply omits it.

CREATE TABLE IF NOT EXISTS leads.outreach_pipeline (
  business_id          uuid PRIMARY KEY REFERENCES leads.businesses(id) ON DELETE CASCADE,

  status               text NOT NULL
                       CHECK (status IN ('claimed', 'rejected', 'imported', 'failed')),
  reason               text,                 -- rejection reason or error, when not imported

  flow_run_id          text,                 -- the Prefect run that claimed it
  claimed_at           timestamptz NOT NULL DEFAULT now(),
  finished_at          timestamptz,

  warmbly_contact_ids  text[],
  warmbly_draft_ids    text[],

  updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Selection probes by business_id (the primary key). These serve reporting:
-- "how many imported today", "what failed and why".
CREATE INDEX IF NOT EXISTS outreach_pipeline_status_idx
  ON leads.outreach_pipeline (status, finished_at DESC);

COMMENT ON TABLE leads.outreach_pipeline IS
  'Agent pipeline state per business: claimed by a run, then rejected, imported or failed. '
  'Batch selection skips businesses with a row unless it is failed or a stale claim.';
