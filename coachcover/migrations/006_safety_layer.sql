-- Schema 6: the safety layer beyond the schedule (ideas 1-4 of 2026-11-06).

-- Idea 2: the issuer's registry of cards. In the demo it is a SIMULATED registry (training data), never a real
-- issuer: every row says so. A card the registry lists as revoked stops counting from its revocation day.
CREATE TABLE issuer_registry (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cert_type_id bigint NOT NULL REFERENCES cert_type (id),
    card_number  text NOT NULL CHECK (btrim(card_number) <> ''),
    revoked      boolean NOT NULL DEFAULT false,
    revoked_on   date,
    note         text,
    simulated    boolean NOT NULL DEFAULT true,
    CHECK (revoked = (revoked_on IS NOT NULL))
);

-- card numbers are compared without spaces, dashes and case
CREATE UNIQUE INDEX issuer_registry_card ON issuer_registry
    (cert_type_id, upper(regexp_replace(card_number, '[^A-Za-z0-9]', '', 'g')));

-- Idea 4: the languages a person speaks with families, and how many families of a group speak each language.
ALTER TABLE staff ADD COLUMN languages text[] NOT NULL DEFAULT '{}';
ALTER TABLE lesson_group ADD COLUMN family_counts jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(family_counts) = 'object');

-- Idea 1: children added to a group; a shift over the norm gets a case for one more instructor.
CREATE TABLE group_enrollment (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id    text NOT NULL REFERENCES lesson_group (id),
    at          timestamptz NOT NULL,
    added       integer NOT NULL CHECK (added > 0),
    language    text CHECK (language ~ '^[a-z]{2}$'),
    size_before integer NOT NULL CHECK (size_before >= 0),
    size_after  integer NOT NULL CHECK (size_after = size_before + added),
    by_whom     text NOT NULL
);

ALTER TABLE cover_case DROP CONSTRAINT cover_case_source_check;
ALTER TABLE cover_case ADD CONSTRAINT cover_case_source_check
    CHECK (source IN ('callout', 'night_check', 'manager', 'closure', 'ratio'));

-- the shift a second (third...) instructor fills: it teaches the same groups at the same time
ALTER TABLE shift ADD COLUMN extra_for text REFERENCES shift (id);
