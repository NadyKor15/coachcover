-- Schema 3: fixes from the adversarial review of stages A+B (items 2, 3, 5, 8, 9, 10).

-- 2. A card is trusted only when a person confirmed it, or when two different readers both
--    returned fields, agree with each other, and agree with the stored type and dates.
ALTER TABLE staff_cert DROP COLUMN needs_review;
ALTER TABLE staff_cert ADD COLUMN needs_review boolean NOT NULL DEFAULT true;

CREATE FUNCTION staff_cert_trust() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    a jsonb := NEW.reader_a -> 'fields';
    b jsonb := NEW.reader_b -> 'fields';
    type_code text := (SELECT code FROM cert_type WHERE id = NEW.cert_type_id);
BEGIN
    NEW.needs_review := NEW.verified_by_human IS NULL AND (
        cardinality(NEW.flags) > 0
        OR jsonb_typeof(a) IS DISTINCT FROM 'object'
        OR jsonb_typeof(b) IS DISTINCT FROM 'object'
        OR a IS DISTINCT FROM b
        OR (a ->> 'type') IS DISTINCT FROM type_code
        OR (a ->> 'issued_on') IS DISTINCT FROM to_char(NEW.issued_on, 'YYYY-MM-DD')
        OR (a ->> 'expires_on') IS DISTINCT FROM to_char(NEW.expires_on, 'YYYY-MM-DD')
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER staff_cert_trust BEFORE INSERT OR UPDATE ON staff_cert
    FOR EACH ROW EXECUTE FUNCTION staff_cert_trust();

UPDATE staff_cert SET flags = flags;  -- recompute for rows written before this migration

ALTER TABLE staff_cert ADD CONSTRAINT staff_cert_two_different_readers CHECK (
    reader_a IS NULL OR reader_b IS NULL
    OR (reader_a ->> 'model' IS NOT NULL AND reader_b ->> 'model' IS NOT NULL AND reader_a ->> 'model' <> reader_b ->> 'model')
);

-- 3. A successful model call names the model that answered.
ALTER TABLE model_call ADD CONSTRAINT model_call_ok_names_model CHECK (NOT ok OR model_returned IS NOT NULL);

-- 5. The case file is append-only: no update, no delete, no truncate, and a case with events cannot be deleted.
DROP TRIGGER case_event_no_update ON case_event;
CREATE TRIGGER case_event_no_change BEFORE UPDATE OR DELETE ON case_event
    FOR EACH ROW EXECUTE FUNCTION case_event_append_only();
CREATE TRIGGER case_event_no_truncate BEFORE TRUNCATE ON case_event
    FOR EACH STATEMENT EXECUTE FUNCTION case_event_append_only();
ALTER TABLE case_event DROP CONSTRAINT case_event_case_id_fkey;
ALTER TABLE case_event ADD CONSTRAINT case_event_case_id_fkey FOREIGN KEY (case_id) REFERENCES cover_case (id);

-- 8. Answers only inside the offer window; one accepted offer per shift; an offer belongs to its case's shift;
--    "released" marks an accepted offer whose person later gave the shift back.
ALTER TABLE offer ADD CONSTRAINT offer_answer_in_window
    CHECK (responded_at IS NULL OR responded_at BETWEEN sent_at AND expires_at);
ALTER TABLE cover_case ADD CONSTRAINT cover_case_id_shift UNIQUE (id, shift_id);
ALTER TABLE offer ADD COLUMN shift_id text;
UPDATE offer o SET shift_id = c.shift_id FROM cover_case c WHERE c.id = o.case_id;
ALTER TABLE offer ALTER COLUMN shift_id SET NOT NULL;
ALTER TABLE offer ADD CONSTRAINT offer_case_shift FOREIGN KEY (case_id, shift_id) REFERENCES cover_case (id, shift_id);
ALTER TABLE offer DROP CONSTRAINT offer_status_check;
ALTER TABLE offer ADD CONSTRAINT offer_status_check
    CHECK (status IN ('pending', 'accepted', 'declined', 'expired', 'withdrawn', 'released'));
CREATE UNIQUE INDEX offer_one_accepted_per_shift ON offer (shift_id) WHERE status = 'accepted';

-- A case ends either by an accepted offer or by an approved plan B, not both
-- (an overtime offer is the outcome of its own approved plan B).
CREATE FUNCTION one_outcome_per_case() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM 1 FROM cover_case WHERE id = NEW.case_id FOR UPDATE;  -- one writer per case at a time
    IF TG_TABLE_NAME = 'offer' THEN
        IF NEW.status = 'accepted' AND NOT NEW.overtime
           AND EXISTS (SELECT 1 FROM plan_b WHERE case_id = NEW.case_id AND status = 'approved') THEN
            RAISE EXCEPTION 'case % already has an approved plan B', NEW.case_id;
        END IF;
    ELSE
        IF NEW.status = 'approved'
           AND EXISTS (SELECT 1 FROM offer WHERE case_id = NEW.case_id AND status = 'accepted' AND NOT overtime) THEN
            RAISE EXCEPTION 'case % already has an accepted offer', NEW.case_id;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER offer_one_outcome BEFORE INSERT OR UPDATE OF status ON offer
    FOR EACH ROW EXECUTE FUNCTION one_outcome_per_case();
CREATE TRIGGER plan_b_one_outcome BEFORE INSERT OR UPDATE OF status ON plan_b
    FOR EACH ROW EXECUTE FUNCTION one_outcome_per_case();

-- 9. Autonomy values are strings (the existing list check lets numbers, booleans and arrays through).
ALTER TABLE policy ADD CONSTRAINT policy_autonomy_strings
    CHECK (NOT jsonb_path_exists(autonomy, 'strict $.* ? (@.type() != "string")'));  -- strict: lax mode unwraps arrays

-- 10. A venue time zone must be a named zone: Postgres also accepts POSIX strings like 'UTC+3',
--     which mean the opposite offset and which Python rejects.
CREATE OR REPLACE FUNCTION check_time_zone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_timezone_names WHERE name = NEW.tz) THEN
        RAISE EXCEPTION 'unknown time zone name: %', NEW.tz USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN NEW;
END
$$;
