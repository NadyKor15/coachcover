-- Coverage engine, schema 1.
-- Reference data (roles, certificate types, programs and levels) is copied
-- from a preset file. People, schedule and the record of every cover case
-- live here. Runtime rows use identity keys; registry rows use readable text ids.

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- presets ---------------------------------------------------------------------

CREATE TABLE preset (
    code   text PRIMARY KEY CHECK (code ~ '^[a-z][a-z0-9_]*$'),
    label  text NOT NULL,
    source jsonb NOT NULL  -- the preset file as installed
);

CREATE TABLE role (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    preset_code text NOT NULL REFERENCES preset (code),
    code        text NOT NULL,
    label       text NOT NULL,
    UNIQUE (preset_code, code),
    UNIQUE (id, preset_code)
);

CREATE TABLE cert_type (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    preset_code     text NOT NULL REFERENCES preset (code),
    code            text NOT NULL,
    label           text NOT NULL,
    validity_months integer NOT NULL CHECK (validity_months > 0),
    UNIQUE (preset_code, code),
    UNIQUE (id, preset_code)
);

-- certificates a role needs, in a fixed order; role and type from the same preset
CREATE TABLE role_cert (
    role_id      bigint NOT NULL,
    cert_type_id bigint NOT NULL,
    preset_code  text NOT NULL,
    ord          smallint NOT NULL,
    PRIMARY KEY (role_id, cert_type_id),
    UNIQUE (role_id, ord),
    FOREIGN KEY (role_id, preset_code) REFERENCES role (id, preset_code),
    FOREIGN KEY (cert_type_id, preset_code) REFERENCES cert_type (id, preset_code)
);

CREATE TABLE program (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    preset_code    text NOT NULL REFERENCES preset (code),
    code           text NOT NULL,
    label          text NOT NULL,
    renewal_months integer CHECK (renewal_months > 0),  -- NULL: never needs renewal
    UNIQUE (preset_code, code)
);

CREATE TABLE program_level (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    program_id         bigint NOT NULL REFERENCES program (id),
    code               text NOT NULL,
    label              text NOT NULL,
    ord                smallint NOT NULL,
    max_per_instructor integer NOT NULL CHECK (max_per_instructor > 0),
    UNIQUE (program_id, code),
    UNIQUE (program_id, ord),
    UNIQUE (id, program_id)
);

-- the business: settings on top of a preset ---------------------------------

CREATE TABLE policy (
    id                 text PRIMARY KEY,
    name               text NOT NULL,
    preset_code        text NOT NULL REFERENCES preset (code),
    max_weekly_hours   numeric(5, 2) NOT NULL CHECK (max_weekly_hours > 0 AND max_weekly_hours <= 168),
    week_start         smallint NOT NULL CHECK (week_start BETWEEN 0 AND 6),  -- 0 = Monday
    makeup_window_days integer NOT NULL CHECK (makeup_window_days >= 0),
    -- what the engine does by itself, only proposes, or never does
    autonomy           jsonb NOT NULL CHECK (
        jsonb_typeof(autonomy) = 'object'
        AND autonomy ?& ARRAY['assign_cover', 'plan_b', 'merge_groups', 'overtime']
        AND autonomy - ARRAY['assign_cover', 'plan_b', 'merge_groups', 'overtime'] = '{}'::jsonb
        AND NOT jsonb_path_exists(autonomy, '$.* ? (@ != "auto" && @ != "propose" && @ != "off")')
    )
);

-- places ------------------------------------------------------------------------

CREATE FUNCTION check_time_zone() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM now() AT TIME ZONE NEW.tz;  -- raises on an unknown zone name
    RETURN NEW;
END
$$;

CREATE TABLE venue (
    id        text PRIMARY KEY,
    policy_id text NOT NULL REFERENCES policy (id),
    name      text NOT NULL,
    tz        text NOT NULL,
    lat       double precision CHECK (lat BETWEEN -90 AND 90),
    lon       double precision CHECK (lon BETWEEN -180 AND 180),
    CHECK ((lat IS NULL) = (lon IS NULL))
);

CREATE TRIGGER venue_tz_valid BEFORE INSERT OR UPDATE OF tz ON venue
    FOR EACH ROW EXECUTE FUNCTION check_time_zone();

CREATE TABLE zone (
    id       text PRIMARY KEY,
    venue_id text NOT NULL REFERENCES venue (id),
    name     text NOT NULL,
    UNIQUE (venue_id, name),
    UNIQUE (id, venue_id)
);

-- people --------------------------------------------------------------------------

CREATE TABLE staff (
    id            text PRIMARY KEY,
    policy_id     text NOT NULL REFERENCES policy (id),
    full_name     text NOT NULL CHECK (btrim(full_name) <> ''),
    home_venue_id text REFERENCES venue (id),
    language      text NOT NULL DEFAULT 'en' CHECK (language ~ '^[a-z]{2}$'),
    active        boolean NOT NULL DEFAULT true
);

CREATE TABLE staff_role (
    staff_id text NOT NULL REFERENCES staff (id) ON DELETE CASCADE,
    role_id  bigint NOT NULL REFERENCES role (id),
    PRIMARY KEY (staff_id, role_id)
);

-- A card is trusted when a person confirmed it or both readings agree.
CREATE TABLE staff_cert (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    staff_id          text NOT NULL REFERENCES staff (id) ON DELETE CASCADE,
    cert_type_id      bigint NOT NULL REFERENCES cert_type (id),
    card_number       text,
    issued_on         date NOT NULL,
    expires_on        date,  -- NULL: issued_on + validity_months of the type
    reader_a          jsonb,  -- {"model": ..., "fields": {...}, "read_at": ...}
    reader_b          jsonb,
    verified_by_human text,  -- who confirmed the card; NULL until a person checks it
    verified_at       timestamptz,
    needs_review      boolean GENERATED ALWAYS AS (
        verified_by_human IS NULL
        AND (reader_a IS NULL OR reader_b IS NULL OR (reader_a -> 'fields') IS DISTINCT FROM (reader_b -> 'fields'))
    ) STORED,
    CHECK (expires_on IS NULL OR expires_on >= issued_on),
    CHECK ((verified_by_human IS NULL) = (verified_at IS NULL))
);

CREATE TABLE staff_program (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    staff_id   text NOT NULL REFERENCES staff (id) ON DELETE CASCADE,
    program_id bigint NOT NULL REFERENCES program (id),
    renewed_at date NOT NULL,
    UNIQUE (staff_id, program_id),
    UNIQUE (id, program_id)
);

-- levels a person is cleared for; the level must belong to the same program
CREATE TABLE staff_program_level (
    staff_program_id bigint NOT NULL,
    program_id       bigint NOT NULL,
    level_id         bigint NOT NULL,
    since            date NOT NULL,
    PRIMARY KEY (staff_program_id, level_id),
    FOREIGN KEY (staff_program_id, program_id) REFERENCES staff_program (id, program_id) ON DELETE CASCADE,
    FOREIGN KEY (level_id, program_id) REFERENCES program_level (id, program_id)
);

-- schedule ------------------------------------------------------------------------

CREATE TABLE lesson_group (
    id               text PRIMARY KEY,
    venue_id         text NOT NULL REFERENCES venue (id),
    zone_id          text,
    level_id         bigint NOT NULL REFERENCES program_level (id),
    name             text NOT NULL,
    size             integer NOT NULL CHECK (size >= 0),
    family_languages text[] NOT NULL CHECK (cardinality(family_languages) >= 1),
    weekday          smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0 = Monday
    starts_local     time NOT NULL,
    minutes          integer NOT NULL CHECK (minutes > 0 AND minutes <= 240),
    instructor_id    text REFERENCES staff (id),  -- the regular instructor
    FOREIGN KEY (zone_id, venue_id) REFERENCES zone (id, venue_id)
);

-- One block of work for one role. Groups on a shift are taught together for the whole shift.
CREATE TABLE shift (
    id          text PRIMARY KEY,
    venue_id    text NOT NULL REFERENCES venue (id),
    zone_id     text,
    role_id     bigint NOT NULL REFERENCES role (id),
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    staff_id    text REFERENCES staff (id),
    instructors smallint NOT NULL DEFAULT 1 CHECK (instructors >= 1),
    status      text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'open', 'cancelled')),
    CHECK (ends_at > starts_at AND ends_at - starts_at <= interval '24 hours'),
    CHECK (status <> 'scheduled' OR staff_id IS NOT NULL),
    FOREIGN KEY (zone_id, venue_id) REFERENCES zone (id, venue_id),
    -- nobody is booked twice at the same time
    EXCLUDE USING gist (staff_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (status = 'scheduled')
);

CREATE INDEX shift_venue_time ON shift (venue_id, starts_at);

CREATE TABLE shift_group (
    shift_id text NOT NULL REFERENCES shift (id) ON DELETE CASCADE,
    group_id text NOT NULL REFERENCES lesson_group (id),
    PRIMARY KEY (shift_id, group_id)
);

-- the case file of one cover: every step and decision, in order -------------

CREATE TABLE cover_case (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shift_id        text NOT NULL REFERENCES shift (id),
    absent_staff_id text REFERENCES staff (id),
    source          text NOT NULL CHECK (source IN ('callout', 'night_check', 'manager', 'closure')),
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'covered', 'plan_b', 'uncovered', 'cancelled')),
    opened_at       timestamptz NOT NULL,
    closed_at       timestamptz,
    CHECK ((status = 'open') = (closed_at IS NULL)),
    CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

CREATE UNIQUE INDEX cover_case_one_open_per_shift ON cover_case (shift_id) WHERE status = 'open';

CREATE TABLE case_event (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id bigint NOT NULL REFERENCES cover_case (id) ON DELETE CASCADE,
    seq     integer NOT NULL CHECK (seq >= 1),
    at      timestamptz NOT NULL,
    actor   text NOT NULL CHECK (actor IN ('agent', 'staff', 'manager')),
    kind    text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*$'),
    data    jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object'),
    UNIQUE (case_id, seq)
);

CREATE FUNCTION case_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'case_event rows are append-only';
END
$$;

CREATE TRIGGER case_event_no_update BEFORE UPDATE ON case_event
    FOR EACH ROW EXECUTE FUNCTION case_event_append_only();

CREATE TABLE offer (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id      bigint NOT NULL REFERENCES cover_case (id),
    staff_id     text NOT NULL REFERENCES staff (id),
    queue_pos    smallint NOT NULL CHECK (queue_pos >= 1),
    place        jsonb NOT NULL,  -- machine reason for the place in the queue
    sent_at      timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'accepted', 'declined', 'expired', 'withdrawn')),
    responded_at timestamptz,
    CHECK (expires_at > sent_at),
    CHECK (status NOT IN ('accepted', 'declined') OR responded_at IS NOT NULL),
    UNIQUE (case_id, staff_id)
);

-- the first "yes" wins: at most one accepted offer per case
CREATE UNIQUE INDEX offer_one_accepted ON offer (case_id) WHERE status = 'accepted';

-- A plan B is approved against the hash of the snapshot that was shown.
CREATE TABLE plan_b (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id       bigint NOT NULL REFERENCES cover_case (id),
    snapshot      jsonb NOT NULL,
    snapshot_hash text NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    status        text NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'approved', 'rejected', 'superseded')),
    created_at    timestamptz NOT NULL,
    decided_by    text,
    decided_at    timestamptz,
    CHECK ((status IN ('approved', 'rejected')) = (decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

CREATE UNIQUE INDEX plan_b_one_live ON plan_b (case_id) WHERE status IN ('proposed', 'approved');

CREATE TABLE closure (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue_id   text NOT NULL REFERENCES venue (id),
    zone_id    text,
    reason     text NOT NULL
               CHECK (reason IN ('weather', 'contamination', 'chemicals', 'equipment', 'no_cover', 'other')),
    note       text,
    starts_at  timestamptz NOT NULL,
    ends_at    timestamptz NOT NULL,
    remedy     text NOT NULL CHECK (remedy IN ('credit', 'makeup')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (ends_at > starts_at),
    FOREIGN KEY (zone_id, venue_id) REFERENCES zone (id, venue_id)
);

-- Messages are recorded, never sent: the only channel is "simulated".
CREATE TABLE message (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL,
    channel    text NOT NULL DEFAULT 'simulated' CHECK (channel = 'simulated'),
    audience   text NOT NULL CHECK (audience IN ('family', 'staff', 'manager')),
    group_id   text REFERENCES lesson_group (id),
    staff_id   text REFERENCES staff (id),
    case_id    bigint REFERENCES cover_case (id),
    closure_id bigint REFERENCES closure (id),
    offer_id   bigint REFERENCES offer (id),
    language   text NOT NULL CHECK (language ~ '^[a-z]{2}$'),
    body       text NOT NULL CHECK (btrim(body) <> ''),
    CHECK (audience <> 'family' OR group_id IS NOT NULL),
    CHECK (audience <> 'staff' OR staff_id IS NOT NULL)
);

-- Every model call. An answer from a model other than the one requested is an error, never a result.
CREATE TABLE model_call (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              timestamptz NOT NULL,
    purpose         text NOT NULL CHECK (purpose ~ '^[a-z][a-z0-9_]*$'),
    model_requested text NOT NULL,
    model_returned  text,
    source          text NOT NULL CHECK (source IN ('live', 'recorded')),
    ok              boolean NOT NULL,
    error           text,
    input_tokens    integer CHECK (input_tokens >= 0),
    output_tokens   integer CHECK (output_tokens >= 0),
    duration_ms     integer CHECK (duration_ms >= 0),
    cost_usd        numeric(12, 6) CHECK (cost_usd >= 0),
    case_id         bigint REFERENCES cover_case (id),
    ref             jsonb NOT NULL DEFAULT '{}',
    CHECK (ok = (error IS NULL)),
    CHECK (NOT ok OR model_returned = model_requested)
);
