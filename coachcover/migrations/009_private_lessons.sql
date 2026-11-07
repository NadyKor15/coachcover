-- Schema 9: private lessons (one child, one coach) and the question to the family.
-- A private lesson is a group of one: lesson_group.kind = 'private'. Another coach takes it only after the family says
-- yes; until then the coach who said yes is held (offer status 'held', with an open client_ask row) and gets no other
-- offer for that time. "No" moves the lesson to the family's own coach (asked again) or ends in a credit.
ALTER TABLE lesson_group ADD COLUMN kind text NOT NULL DEFAULT 'group' CHECK (kind IN ('group', 'private'));
ALTER TABLE lesson_group ADD CONSTRAINT lesson_group_private_is_one CHECK (kind <> 'private' OR size = 1);
-- the child's first name, for the messages to the family (a private lesson only; the parent is never named)
ALTER TABLE lesson_group ADD COLUMN participant text CHECK (participant IS NULL OR (kind = 'private' AND btrim(participant) <> ''));

-- the price of what a family gets back: a credit for a private lesson is worth that lesson
ALTER TABLE remedy ADD COLUMN amount_usd numeric(8, 2) CHECK (amount_usd IS NULL OR amount_usd >= 0);

CREATE TABLE client_ask (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id       bigint NOT NULL REFERENCES cover_case (id),
    group_id      text NOT NULL REFERENCES lesson_group (id),
    -- 'cover': another coach at the same time (offer_id is the held coach's offer);
    -- 'reschedule': the family's own coach at another time (proposed_start/end)
    kind          text NOT NULL CHECK (kind IN ('cover', 'reschedule')),
    staff_id      text NOT NULL REFERENCES staff (id),
    offer_id      bigint REFERENCES offer (id),
    proposed_start timestamptz,
    proposed_end   timestamptz,
    status        text NOT NULL DEFAULT 'asked' CHECK (status IN ('asked', 'yes', 'no', 'silent', 'withdrawn')),
    language      text NOT NULL CHECK (language ~ '^[a-z]{2}$'),
    message_id    bigint REFERENCES message (id),
    asked_at      timestamptz NOT NULL,
    answer_by     timestamptz NOT NULL,
    answered_at   timestamptz,
    CHECK (answer_by > asked_at),
    CHECK ((kind = 'cover') = (offer_id IS NOT NULL)),
    CHECK ((kind = 'reschedule') = (proposed_start IS NOT NULL AND proposed_end IS NOT NULL)),
    CHECK (proposed_end IS NULL OR proposed_end > proposed_start),
    CHECK ((status IN ('yes', 'no')) = (answered_at IS NOT NULL))
);
-- one open question per case at a time
CREATE UNIQUE INDEX client_ask_one_open ON client_ask (case_id) WHERE status = 'asked';
CREATE INDEX client_ask_due ON client_ask (answer_by) WHERE status = 'asked';

-- the family answers in the case file in its own name
ALTER TABLE case_event DROP CONSTRAINT case_event_actor_check;
ALTER TABLE case_event ADD CONSTRAINT case_event_actor_check CHECK (actor IN ('agent', 'staff', 'manager', 'family'));
