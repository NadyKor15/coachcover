-- Schema 2: settings and records for the cover pipeline, the API and the worker.

ALTER TABLE role ADD COLUMN reserve boolean NOT NULL DEFAULT false;  -- kept out of the offer queue
ALTER TABLE cert_type ADD COLUMN match text[] NOT NULL DEFAULT '{}';  -- words on a card title naming the type

ALTER TABLE policy
    ADD COLUMN offer_timeout_minutes  integer NOT NULL DEFAULT 5 CHECK (offer_timeout_minutes > 0),
    ADD COLUMN overtime_cost_per_hour numeric(8, 2) NOT NULL DEFAULT 0 CHECK (overtime_cost_per_hour >= 0),
    ADD COLUMN default_remedy         text NOT NULL DEFAULT 'credit' CHECK (default_remedy IN ('credit', 'makeup')),
    ADD CONSTRAINT policy_overtime_never_auto CHECK (autonomy ->> 'overtime' <> 'auto');

-- A card also goes to a person when it carries flags (name differs, unknown type, a reader failed).
ALTER TABLE staff_cert ADD COLUMN flags text[] NOT NULL DEFAULT '{}';
ALTER TABLE staff_cert ADD COLUMN image_sha256 text CHECK (image_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE staff_cert DROP COLUMN needs_review;
ALTER TABLE staff_cert ADD COLUMN needs_review boolean GENERATED ALWAYS AS (
    verified_by_human IS NULL
    AND (
        cardinality(flags) > 0
        OR reader_a IS NULL
        OR reader_b IS NULL
        OR (reader_a -> 'fields') IS DISTINCT FROM (reader_b -> 'fields')
    )
) STORED;

ALTER TABLE offer ADD COLUMN overtime boolean NOT NULL DEFAULT false;  -- an approved plan B overtime offer
ALTER TABLE plan_b ADD COLUMN chosen_option smallint CHECK (chosen_option >= 0);
ALTER TABLE plan_b ADD CONSTRAINT plan_b_choice_when_approved CHECK ((status = 'approved') = (chosen_option IS NOT NULL));

-- An action the policy says to propose waits here for a person (plan B has its own table).
CREATE TABLE approval (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id    bigint NOT NULL REFERENCES cover_case (id),
    action     text NOT NULL CHECK (action IN ('assign_cover')),
    payload    jsonb NOT NULL,
    status     text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    created_at timestamptz NOT NULL,
    decided_by text,
    decided_at timestamptz,
    CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

CREATE UNIQUE INDEX approval_one_pending ON approval (case_id, action) WHERE status = 'pending';

-- What the families of a cancelled lesson get.
CREATE TABLE remedy (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shift_id    text NOT NULL REFERENCES shift (id),
    group_id    text NOT NULL REFERENCES lesson_group (id),
    kind        text NOT NULL CHECK (kind IN ('credit', 'makeup')),
    families    integer NOT NULL CHECK (families >= 0),
    valid_until date,  -- end of the makeup window
    closure_id  bigint REFERENCES closure (id),
    case_id     bigint REFERENCES cover_case (id),
    created_at  timestamptz NOT NULL,
    CHECK ((kind = 'makeup') = (valid_until IS NOT NULL)),
    CHECK (closure_id IS NOT NULL OR case_id IS NOT NULL),
    UNIQUE (shift_id, group_id)
);

-- One clock for API and worker in demo mode: now = base + (real time - set_at) * speed.
CREATE TABLE demo_clock (
    id     boolean PRIMARY KEY DEFAULT true CHECK (id),
    base   timestamptz NOT NULL,
    set_at timestamptz NOT NULL,
    speed  numeric(8, 2) NOT NULL DEFAULT 1 CHECK (speed > 0)
);

-- Judge runs per client address, for the hourly limit.
CREATE TABLE judge_run (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX judge_run_client_at ON judge_run (client, at);

-- The worker's nightly check, once per policy and day.
CREATE TABLE night_report (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    policy_id  text NOT NULL REFERENCES policy (id),
    for_date   date NOT NULL,
    created_at timestamptz NOT NULL,
    data       jsonb NOT NULL,
    UNIQUE (policy_id, for_date)
);
