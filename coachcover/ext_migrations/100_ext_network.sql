-- Extension lane (EXT), schema 100: a network of venues, each with its own agent, rules and manager;
-- staff loans between them over A2A; cover proposals from outside assistants (MCP); team chat posts
-- (simulated); limits for the public MCP endpoint. No table of the core schema is changed.

CREATE TABLE ext_network (
    id   text PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> '')
);

-- A venue in a network: its own agent, its own rules (policy) and its own manager.
CREATE TABLE ext_network_member (
    venue_id     text PRIMARY KEY REFERENCES venue (id),
    network_id   text NOT NULL REFERENCES ext_network (id),
    policy_id    text NOT NULL REFERENCES policy (id),
    manager_name text NOT NULL CHECK (btrim(manager_name) <> ''),
    guard_role   text NOT NULL,                                   -- the role counted on site while lending
    min_guards   smallint NOT NULL DEFAULT 2 CHECK (min_guards >= 0),  -- on site or free and cleared, after a loan
    lends        boolean NOT NULL DEFAULT true
);

-- One loan: the borrower's agent asks, the lender's agent offers one of its people,
-- the borrower checks them by its own rules, both managers approve, then the borrower books.
CREATE TABLE ext_loan (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code                 text NOT NULL UNIQUE,
    network_id           text NOT NULL REFERENCES ext_network (id),
    borrower_venue       text NOT NULL REFERENCES venue (id),
    lender_venue         text REFERENCES venue (id),
    shift_id             text NOT NULL REFERENCES shift (id),
    case_id              bigint REFERENCES cover_case (id),
    staff_id             text REFERENCES staff (id),
    offer                jsonb,  -- what the lender sent about the person, as the borrower checked it
    status               text NOT NULL
                         CHECK (status IN ('asking', 'declined', 'rejected', 'awaiting_approval', 'assigned', 'failed')),
    a2a_context_id       text NOT NULL,
    a2a_task_id          text,
    borrower_approved_by text,
    borrower_approved_at timestamptz,
    lender_approved_by   text,
    lender_approved_at   timestamptz,
    created_at           timestamptz NOT NULL,
    closed_at            timestamptz,
    CHECK (lender_venue IS NULL OR lender_venue <> borrower_venue),
    CHECK ((borrower_approved_by IS NULL) = (borrower_approved_at IS NULL)),
    CHECK ((lender_approved_by IS NULL) = (lender_approved_at IS NULL)),
    CHECK (status <> 'assigned'
           OR (borrower_approved_by IS NOT NULL AND lender_approved_by IS NOT NULL AND staff_id IS NOT NULL)),
    CHECK ((status IN ('asking', 'awaiting_approval')) = (closed_at IS NULL))
);

CREATE UNIQUE INDEX ext_loan_one_live_per_shift ON ext_loan (shift_id) WHERE status IN ('asking', 'awaiting_approval');

-- Every step of a loan, written by the side that took it. Append-only.
CREATE TABLE ext_loan_step (
    id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loan_id  bigint NOT NULL REFERENCES ext_loan (id),
    seq      integer NOT NULL CHECK (seq >= 1),
    at       timestamptz NOT NULL,
    venue_id text NOT NULL REFERENCES venue (id),
    actor    text NOT NULL CHECK (actor IN ('agent', 'manager')),
    kind     text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    data     jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    UNIQUE (loan_id, seq)
);

CREATE FUNCTION ext_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER ext_loan_step_no_change BEFORE UPDATE OR DELETE ON ext_loan_step
    FOR EACH ROW EXECUTE FUNCTION ext_append_only();

-- A2A tasks held by the agents of this server (the lender side of a loan).
CREATE TABLE ext_a2a_task (
    id             text PRIMARY KEY,
    context_id     text NOT NULL,
    venue_id       text NOT NULL REFERENCES venue (id),
    loan_id        bigint REFERENCES ext_loan (id),
    state          text NOT NULL CHECK (state ~ '^TASK_STATE_[A-Z_]+$'),
    status_message jsonb,
    artifacts      jsonb NOT NULL DEFAULT '[]',
    history        jsonb NOT NULL DEFAULT '[]',
    updated_at     timestamptz NOT NULL
);

-- A cover asked for by an outside assistant (MCP): a proposal for the manager. Nothing is sent or booked.
CREATE TABLE ext_cover_request (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shift_id     text NOT NULL REFERENCES shift (id),
    requested_by text NOT NULL,
    created_at   timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'dismissed', 'used')),
    candidates   jsonb NOT NULL
);

-- Team chat posts after a closed cover. Recorded, never sent: delivery is always simulated.
CREATE TABLE ext_team_post (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue_id   text NOT NULL REFERENCES venue (id),
    channel    text NOT NULL CHECK (channel ~ '^#[a-z0-9-]+$'),
    case_id    bigint UNIQUE REFERENCES cover_case (id),
    loan_id    bigint REFERENCES ext_loan (id),
    body       text NOT NULL CHECK (btrim(body) <> ''),
    link       text,
    delivery   text NOT NULL DEFAULT 'simulated' CHECK (delivery = 'simulated'),
    created_at timestamptz NOT NULL,
    CHECK (case_id IS NOT NULL OR loan_id IS NOT NULL)
);

CREATE UNIQUE INDEX ext_team_post_one_per_loan_venue ON ext_team_post (loan_id, venue_id) WHERE loan_id IS NOT NULL;

-- Calls to the public MCP endpoint per client address, for the hourly limit (survives a demo reset).
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.ext_mcp_call (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ext_mcp_call_client_at ON ops.ext_mcp_call (client, at);
