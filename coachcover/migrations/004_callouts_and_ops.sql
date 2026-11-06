-- Schema 4: call-out grouping, one pending offer per case, judge runs outside the demo data.

-- One call-out can open several cases (two lessons in one voice note); model calls made before the
-- cases exist carry the same id in ref->>'callout_id'.
ALTER TABLE cover_case ADD COLUMN callout_id uuid;
CREATE INDEX cover_case_callout ON cover_case (callout_id);

-- Offers go one at a time.
CREATE UNIQUE INDEX offer_one_pending_per_case ON offer (case_id) WHERE status = 'pending';

-- The judge limit must survive a demo reset, which drops and rebuilds the public schema.
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.judge_run (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS judge_run_client_at ON ops.judge_run (client, at);
DROP TABLE judge_run;
