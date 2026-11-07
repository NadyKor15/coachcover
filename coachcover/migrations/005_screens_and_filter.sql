-- Schema 5: data for the screens (API contract 2026-11-06), night cover cases, card notes, stored voice notes.

-- Groups know their children's ages (young children have a stricter norm);
-- a shift can need certificates on top of its role's (a combined post: a role plus one more certificate).
ALTER TABLE lesson_group ADD COLUMN age_min smallint CHECK (age_min >= 0);
ALTER TABLE lesson_group ADD COLUMN age_max smallint;
ALTER TABLE lesson_group ADD CONSTRAINT lesson_group_ages CHECK (age_max IS NULL OR age_min IS NULL OR age_max >= age_min);
ALTER TABLE shift ADD COLUMN extra_certs text[] NOT NULL DEFAULT '{}';

-- The manager the screens show.
ALTER TABLE policy ADD COLUMN manager_name text;

-- A fifth autonomy action: what the night check does when tomorrow's holder may not work.
UPDATE policy SET autonomy = autonomy || '{"night_cover": "propose"}' WHERE NOT autonomy ? 'night_cover';
ALTER TABLE policy DROP CONSTRAINT policy_autonomy_check;
ALTER TABLE policy ADD CONSTRAINT policy_autonomy_check CHECK (
    jsonb_typeof(autonomy) = 'object'
    AND autonomy ?& ARRAY['assign_cover', 'plan_b', 'merge_groups', 'overtime', 'night_cover']
    AND autonomy - ARRAY['assign_cover', 'plan_b', 'merge_groups', 'overtime', 'night_cover'] = '{}'::jsonb
    AND NOT jsonb_path_exists(autonomy, 'strict $.* ? (@ != "auto" && @ != "propose" && @ != "off")')
);
ALTER TABLE approval DROP CONSTRAINT approval_action_check;
ALTER TABLE approval ADD CONSTRAINT approval_action_check CHECK (action IN ('assign_cover', 'night_cover'));

-- Readable case codes for the screens: C-<mmdd>-<nn>.
ALTER TABLE cover_case ADD COLUMN code text UNIQUE;

-- The voice note of a call-out, played back on the screen.
CREATE TABLE callout_audio (
    callout_id uuid PRIMARY KEY,
    filename   text NOT NULL,
    mime       text NOT NULL,
    seconds    numeric(8, 2),
    data       bytea NOT NULL CHECK (octet_length(data) <= 10485760)
);

-- Card notes never block (the readers disagree only on the card number: "check ID").
-- Trust compares the two readings on type and dates; a date disagreement blocks until a person checks.
ALTER TABLE staff_cert ADD COLUMN notes text[] NOT NULL DEFAULT '{}';

CREATE OR REPLACE FUNCTION staff_cert_trust() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    a jsonb := NEW.reader_a -> 'fields';
    b jsonb := NEW.reader_b -> 'fields';
    type_code text := (SELECT code FROM cert_type WHERE id = NEW.cert_type_id);
BEGIN
    NEW.needs_review := NEW.verified_by_human IS NULL AND (
        cardinality(NEW.flags) > 0
        OR jsonb_typeof(a) IS DISTINCT FROM 'object'
        OR jsonb_typeof(b) IS DISTINCT FROM 'object'
        OR (a - 'card_number') IS DISTINCT FROM (b - 'card_number')
        OR (a ->> 'type') IS DISTINCT FROM type_code
        OR (a ->> 'issued_on') IS DISTINCT FROM to_char(NEW.issued_on, 'YYYY-MM-DD')
        OR (a ->> 'expires_on') IS DISTINCT FROM to_char(NEW.expires_on, 'YYYY-MM-DD')
    );
    RETURN NEW;
END
$$;

UPDATE staff_cert SET flags = flags;  -- recompute trust under the new rule

-- Judge runs: is one running, and which case it made (kept outside the demo data, like the limit).
CREATE TABLE IF NOT EXISTS ops.judge_state (
    id         boolean PRIMARY KEY DEFAULT true CHECK (id),
    running    boolean NOT NULL,
    scenario   text,
    case_code  text,
    started_at timestamptz,
    finished_at timestamptz
);

-- An approved plan B option that did not work out (the overtime person said no): "failed", so a new plan can follow.
ALTER TABLE plan_b DROP CONSTRAINT plan_b_status_check;
ALTER TABLE plan_b ADD CONSTRAINT plan_b_status_check
    CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded', 'failed'));
ALTER TABLE plan_b DROP CONSTRAINT plan_b_check;
ALTER TABLE plan_b ADD CONSTRAINT plan_b_decided
    CHECK ((status IN ('approved', 'rejected', 'failed')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL));
ALTER TABLE plan_b DROP CONSTRAINT plan_b_choice_when_approved;
ALTER TABLE plan_b ADD CONSTRAINT plan_b_choice_when_approved
    CHECK ((status IN ('approved', 'failed')) = (chosen_option IS NOT NULL));
