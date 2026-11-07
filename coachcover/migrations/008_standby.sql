-- Schema 8: a standby proposal for a fragile shift. It only proposes (autonomy "propose"): nothing is booked
-- until the manager acts on it.
CREATE TABLE standby_proposal (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shift_id   text NOT NULL REFERENCES shift (id),
    staff_id   text NOT NULL REFERENCES staff (id),
    status     text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'accepted', 'declined', 'withdrawn')),
    mode       text NOT NULL DEFAULT 'propose' CHECK (mode = 'propose'),
    text       text NOT NULL CHECK (btrim(text) <> ''),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (shift_id, staff_id)
);
