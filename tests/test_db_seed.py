"""Schema, preset install, the training school and its traps, read back through the rules."""

import dataclasses
from datetime import date, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from coachcover.db import install_preset, migrate
from coachcover.presets import default_policy
from coachcover.reasons_text import describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, assess, eligible, rank
from coachcover.seed import DECK_NOW, HERO_SEAT, NOW, POLICY_ID, build_school, load_school

pytestmark = pytest.mark.db

DEMO_LESSON = "L-1109-G002"  # preschool: Marina's Monday 09:30-10:15 Preschool swim, ages 3-5 (8 kids, 2 instructors);
#                              deck: her 06:00-08:00 lanes + Level 3 (8 young kids) at V1
DEMO_GUARD_BLOCK = "G-1109-V1-0530"  # Monday 05:30-10:00 lifeguard block at V1


# --- schema and preset ------------------------------------------------------


def test_migrations_apply_once(new_database):
    conn = new_database()
    versions = ["001_init", "002_pipeline", "003_review_fixes", "004_callouts_and_ops", "005_screens_and_filter",
                "006_safety_layer", "007_offer_waves", "008_standby"]
    assert migrate(conn) == versions
    assert migrate(conn) == []
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")] == versions


def test_preset_install_is_idempotent_and_refuses_a_different_copy(new_database):
    conn = new_database()
    migrate(conn)
    install_preset(conn, "swim_school")
    install_preset(conn, "swim_school")
    assert conn.execute("SELECT count(*) FROM program_level").fetchone()[0] == 7  # Learn-to-Swim 1-6, preschool 3-5
    conn.execute("UPDATE preset SET source = source || '{\"label\": \"changed\"}' WHERE code = 'swim_school'")
    with pytest.raises(ValueError, match="different content"):
        install_preset(conn, "swim_school")


def test_policy_read_back_equals_the_preset_policy(school):
    assert load_policy(school, POLICY_ID) == default_policy("swim_school", week_start="monday")  # the school's week


# --- the training school ----------------------------------------------------


def _count(conn, query, *params):
    return conn.execute(query, params).fetchone()[0]


def test_school_size_and_languages(school):
    assert _count(school, "SELECT count(*) FROM venue") == 3
    assert _count(school, "SELECT count(*) FROM zone") == 6
    assert _count(school, "SELECT count(*) FROM staff") == 40
    assert _count(school, "SELECT count(DISTINCT full_name) FROM staff") == 40
    assert _count(school, "SELECT count(*) FROM lesson_group") == 121  # + the lesson next door to the 09:30 one
    assert len(build_school("deck").groups) == 120
    assert _count(school, "SELECT count(*) FROM lesson_group WHERE instructor_id IS NULL") == 0
    langs = {r[0] for r in school.execute("SELECT DISTINCT unnest(family_languages) FROM lesson_group")}
    assert langs == {"en", "es", "ru"}
    assert {r[0] for r in school.execute("SELECT DISTINCT language FROM staff")} == {"en", "es", "ru"}
    assert _count(school, "SELECT count(*) FROM venue WHERE tz = 'America/New_York'") == 3


@pytest.mark.parametrize("story", ["school", "deck_school"])
def test_every_group_size_fits_its_level_norm(story, request):
    conn = request.getfixturevalue(story)
    over = conn.execute(
        "SELECT g.id FROM lesson_group g JOIN program_level l ON l.id = g.level_id"
        " WHERE g.size > l.max_per_instructor * (SELECT max(s.instructors) FROM shift s JOIN shift_group sg"
        "  ON sg.shift_id = s.id WHERE sg.group_id = g.id)"
    ).fetchall()
    assert over == []
    two = conn.execute("SELECT count(*) FROM shift WHERE id = 'L-1109-G002-I2'").fetchone()[0]
    assert two == (1 if story == "school" else 0)  # preschool: the 09:30 lesson has its second instructor's seat


def test_trap_counts_in_the_data(school):
    today = date(2026, 11, 9)
    assert _count(school, "SELECT count(*) FROM staff_cert WHERE expires_on < %s", today) == 3
    assert _count(school, "SELECT count(*) FROM staff_cert WHERE expires_on = %s", today) == 2
    assert _count(school, "SELECT count(*) FROM staff_cert WHERE needs_review") == 1
    assert _count(school, "SELECT count(*) FROM staff_cert WHERE issued_on > %s", today) == 0
    assert _count(school, "SELECT count(*) FROM staff_program_level WHERE since > %s", today) == 0
    overdue = school.execute(
        "SELECT sp.staff_id FROM staff_program sp JOIN program p ON p.id = sp.program_id"
        " WHERE sp.renewed_at + make_interval(months => p.renewal_months) < %s",
        (today,),
    ).fetchall()
    assert overdue == [("S14",)]
    week_hours = school.execute(
        "SELECT staff_id, sum(extract(epoch FROM ends_at - starts_at)) / 3600 AS h FROM shift"
        " WHERE starts_at >= '2026-11-09 00:00 America/New_York' AND starts_at < '2026-11-16 00:00 America/New_York'"
        " GROUP BY staff_id HAVING sum(extract(epoch FROM ends_at - starts_at)) / 3600 > 32 ORDER BY staff_id"
    ).fetchall()
    assert [(s, float(h)) for s, h in week_hours] == [("S11", 39.5), ("S29", 38.0)]


def test_disputed_card_clears_once_a_person_confirms_it(school):
    with school.transaction(force_rollback=True):
        assert _count(school, "SELECT count(*) FROM staff_cert WHERE needs_review AND staff_id = 'S13'") == 1
        school.execute(
            "UPDATE staff_cert SET verified_by_human = 'manager:demo', verified_at = now()"
            " WHERE staff_id = 'S13' AND needs_review"
        )
        assert _count(school, "SELECT count(*) FROM staff_cert WHERE needs_review") == 0


def test_the_traps_meet_the_rules_at_the_morning_call_out(school):
    policy = load_policy(school, POLICY_ID)
    staff = {s.id: s for s in load_staff(school, POLICY_ID)}
    lesson = load_shift(school, DEMO_LESSON)
    guard = load_shift(school, DEMO_GUARD_BLOCK)
    group = lesson.groups[0]
    assert (group.program, group.level, group.size, group.age_min) == ("preschool", "3-5", 8, 3)
    assert (lesson.venue, lesson.zone, lesson.role, lesson.instructors) == ("V1", "V1-TEACH", "instructor", 2)
    assert f"{lesson.start.astimezone(NOW.tzinfo):%H:%M}" == "09:30" and NOW.strftime("%H:%M") == "08:40"
    lesson_ctx, guard_ctx = load_context(school, POLICY_ID, lesson), load_context(school, POLICY_ID, guard)

    def decide(sid, shift, ctx):
        return eligible(staff[sid], shift, NOW, policy, ctx)

    expired = decide("S07", lesson, lesson_ctx)
    assert expired.codes[0] == Code.CERT_EXPIRED and expired.reasons[0].params["expires_on"] == "2026-11-08"
    assert decide("S10", lesson, lesson_ctx).codes[0] == Code.CERT_EXPIRED
    today = decide("S08", lesson, lesson_ctx)  # card ends today (a warning); no preschool training
    assert today.codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY, Code.PROGRAM_MISSING] and today.reasons[0].params["today"] is True
    assert decide("S11", lesson, lesson_ctx).codes == [Code.HOURS_LIMIT_EXCEEDED]  # 39.5 h + 0.75 h
    anna = decide("S27", lesson, lesson_ctx)  # free, cleared for preschool, but her WSI ended Nov 5
    assert anna.codes == [Code.CERT_EXPIRED] and anna.reasons[0].params == {"cert_type": "wsi", "expires_on": "2026-11-05"}
    assert decide("S13", lesson, lesson_ctx).codes[0] == Code.CERT_NEEDS_REVIEW
    revoked = decide("S14", lesson, lesson_ctx)  # printed valid, revoked by the issuer (simulated registry)
    assert revoked.codes == [Code.CERT_REVOKED, Code.PROGRAM_MISSING] and revoked.reasons[0].params["cert_type"] == "wsi"
    assert decide("S24", lesson, lesson_ctx).codes == [Code.PROGRAM_MISSING]  # Gabriela: no preschool training
    second = decide("S01", lesson, lesson_ctx)  # Emma Carter teaches this lesson with Marina: her own seat
    assert second.codes == [Code.OVERLAPS_COMMITMENT] and second.reasons[0].params["ref"] == HERO_SEAT
    cleared = {sid for sid in staff if decide(sid, lesson, lesson_ctx).reasons == ()}
    assert cleared == {"S06", "S17", "S20", "S22", "S25", "S26"}  # the five, plus Liam Foster (reserve, plan B only)
    no_clearance = [sid for sid in staff if decide(sid, lesson, lesson_ctx).codes == [Code.PROGRAM_MISSING]]
    # only the preschool training is missing: 9 instructors and the reserve head coaches Noah Bennett and Harper Hughes
    assert len(no_clearance) == 11 and {"S12", "S23", "S24"} <= set(no_clearance)

    assert decide("S27", guard, guard_ctx).codes == []  # her lifeguard card is fine: only the WSI ran out
    assert decide("S28", lesson, lesson_ctx).codes[:3] == [Code.ROLE_MISSING, Code.CERT_MISSING, Code.PROGRAM_MISSING]
    holder_view = dataclasses.replace(guard, assignee=None)  # judged as the person on the block, not a replacement
    assert decide("S30", holder_view, guard_ctx).codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY]  # her card ends today
    over = decide("S29", guard, guard_ctx)
    assert over.codes == [Code.HOURS_LIMIT_EXCEEDED] and over.reasons[0].params["hours_after"] == 42.5
    # the instructor of the lesson next door (lap lanes, the same 09:30-10:15) is busy
    neighbour = school.execute("SELECT staff_id FROM shift WHERE id = 'L-1109-G121'").fetchone()[0]
    assert Code.OVERLAPS_COMMITMENT in decide(neighbour, lesson, lesson_ctx).codes


def test_the_deck_story_traps_meet_the_rules_at_its_morning_call_out(deck_school):
    """The deck story (network_loan): Marina's 06:00-08:00 deck shift, as the school was before the preschool morning."""
    school = deck_school
    policy = load_policy(school, POLICY_ID)
    staff = {s.id: s for s in load_staff(school, POLICY_ID)}
    lesson = load_shift(school, DEMO_LESSON)
    guard = load_shift(school, DEMO_GUARD_BLOCK)
    assert lesson.groups[0].level == "3" and lesson.venue == "V1"
    lesson_ctx, guard_ctx = load_context(school, POLICY_ID, lesson), load_context(school, POLICY_ID, guard)

    def decide(sid, shift, ctx):
        return eligible(staff[sid], shift, DECK_NOW, policy, ctx)

    expired = decide("S07", lesson, lesson_ctx)
    assert expired.codes == [Code.CERT_EXPIRED] and expired.reasons[0].params["expires_on"] == "2026-11-08"
    assert decide("S10", lesson, lesson_ctx).codes == [Code.CERT_EXPIRED]
    today = decide("S08", lesson, lesson_ctx)  # card ends today (a warning) and he teaches at 07:00
    assert today.codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY, Code.OVERLAPS_COMMITMENT] and today.reasons[0].params["today"] is True
    assert decide("S11", lesson, lesson_ctx).codes == [Code.HOURS_LIMIT_EXCEEDED]  # 39.5 h + 2 h
    anna = decide("S27", lesson, lesson_ctx)  # free, level 3, but her lifeguard card ended Nov 5
    assert anna.codes == [Code.CERT_EXPIRED] and anna.reasons[0].params == {"cert_type": "lifeguarding", "expires_on": "2026-11-05"}
    assert decide("S13", lesson, lesson_ctx).codes[0] == Code.CERT_NEEDS_REVIEW
    assert decide("S14", lesson, lesson_ctx).codes == [Code.PROGRAM_RENEWAL_OVERDUE]
    assert decide("S17", lesson, lesson_ctx).reasons == ()

    assert decide("S27", guard, guard_ctx).codes == [Code.CERT_EXPIRED]
    assert decide("S28", lesson, lesson_ctx).codes[:2] == [Code.CERT_MISSING, Code.PROGRAM_MISSING]  # lifeguard without WSI
    holder_view = dataclasses.replace(guard, assignee=None)  # judged as the person on the block, not a replacement
    assert decide("S30", holder_view, guard_ctx).codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY]  # her card ends today
    over = decide("S29", guard, guard_ctx)
    assert over.codes == [Code.HOURS_LIMIT_EXCEEDED] and over.reasons[0].params["hours_after"] == 42.5
    # the regular instructor of the group next door is busy at 06:00
    neighbour = school.execute("SELECT staff_id FROM shift WHERE id = 'L-1109-G001'").fetchone()[0]
    assert Code.OVERLAPS_COMMITMENT in decide(neighbour, lesson, lesson_ctx).codes


def test_the_morning_queue_is_ordered_and_explained(school):
    policy = load_policy(school, POLICY_ID)
    staff = load_staff(school, POLICY_ID)
    lesson = load_shift(school, DEMO_LESSON)
    context = load_context(school, POLICY_ID, lesson)
    ranked = rank(assess(s, lesson, NOW, policy, context) for s in staff)
    ok = [r for r in ranked if r.candidate.decision.ok]
    assert len(ranked) == 40 and 3 <= len(ok) < 40
    assert "S05" not in {r.candidate.staff_id for r in ok}  # the absent person is not her own cover
    assert all(r.candidate.decision.ok for r in ranked[: len(ok)])  # eligible first
    hours = [r.candidate.week_hours for r in ok]
    assert hours == sorted(hours)  # within the eligible, lighter week first
    names = dict(school.execute("SELECT id, full_name FROM staff").fetchall())
    top = ranked[0]
    line = describe_place(top, names[top.candidate.staff_id].split()[0], "en", policy.preset)
    assert line.startswith(f"{names[top.candidate.staff_id].split()[0]} first: ") and "Preschool, same pool" in line
    again = rank(assess(s, lesson, NOW, policy, context) for s in reversed(staff))
    assert [r.candidate.staff_id for r in again] == [r.candidate.staff_id for r in ranked]


# --- determinism ------------------------------------------------------------

TABLES = [
    "preset", "role", "cert_type", "role_cert", "program", "program_level", "policy", "venue", "zone", "staff",
    "staff_role", "staff_cert", "staff_program", "staff_program_level", "lesson_group", "shift", "shift_group",
]


def _dump(conn):
    return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in TABLES}


def test_seed_is_deterministic(new_database):
    assert build_school() == build_school()
    first, second = new_database(), new_database()
    load_school(first)
    load_school(second)
    a, b = _dump(first), _dump(second)
    assert a == b
    # 204 + Gabriela's Sunday evening before the week + the lesson next door (G121) + Emma Carter's seat on G002
    assert len(a["shift"]) == 207 and len(a["staff_cert"]) == 107
    assert build_school("deck") == build_school("deck") and len(build_school("deck").shifts) == 205


def test_loading_the_school_twice_fails(new_database):
    conn = new_database()
    load_school(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        load_school(conn)


# --- integrity the database itself enforces ---------------------------------


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        (
            "INSERT INTO model_call (at, purpose, model_requested, model_returned, source, ok)"
            " VALUES (now(), 'extract', 'muse-glimmer-30b', 'some-other-model', 'live', true)",
            psycopg.errors.CheckViolation,
        ),
        (
            "INSERT INTO message (created_at, channel, audience, group_id, language, body)"
            " VALUES (now(), 'sms', 'family', 'G001', 'en', 'hi')",
            psycopg.errors.CheckViolation,
        ),
        (
            "INSERT INTO shift (id, venue_id, role_id, starts_at, ends_at, staff_id)"
            " SELECT 'X-DOUBLE', 'V1', role_id, starts_at + interval '10 minutes', ends_at, staff_id"
            " FROM shift WHERE id = 'L-1109-G001'",
            psycopg.errors.ExclusionViolation,
        ),
        (
            "UPDATE policy SET autonomy = autonomy || '{\"overtime\": \"sometimes\"}'",
            psycopg.errors.CheckViolation,
        ),
        (
            "UPDATE policy SET autonomy = autonomy || '{\"fly\": \"auto\"}'",
            psycopg.errors.CheckViolation,
        ),
        ("INSERT INTO venue (id, policy_id, name, tz) VALUES ('VX', 'demo-school', 'X', 'Mars/Base')", psycopg.errors.InvalidParameterValue),
        ("UPDATE lesson_group SET zone_id = 'V2-MAIN' WHERE id = 'G001'", psycopg.errors.ForeignKeyViolation),
        ("UPDATE staff_cert SET expires_on = issued_on - 1 WHERE id = (SELECT min(id) FROM staff_cert)", psycopg.errors.CheckViolation),
    ],
)
def test_database_rejects_bad_rows(school, statement, error):
    with school.transaction(force_rollback=True):
        with pytest.raises(error):
            with school.transaction():
                school.execute(statement)


def test_case_file_events_and_first_yes_wins(school):
    with school.transaction(force_rollback=True):
        case_id = school.execute(
            "INSERT INTO cover_case (shift_id, absent_staff_id, source, opened_at)"
            " VALUES (%s, 'S05', 'callout', %s) RETURNING id",
            (DEMO_LESSON, NOW),
        ).fetchone()[0]
        school.execute(
            "INSERT INTO case_event (case_id, seq, at, actor, kind, data) VALUES (%s, 1, %s, 'agent', 'callout_received', %s)",
            (case_id, NOW, Jsonb({"text": "cannot come at 6"})),
        )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with school.transaction():
                school.execute("UPDATE case_event SET kind = 'edited' WHERE case_id = %s", (case_id,))
        school.execute(
            "INSERT INTO offer (case_id, shift_id, staff_id, queue_pos, place, sent_at, expires_at, status, responded_at)"
            " VALUES (%s, %s, 'S17', 1, '{}', %s, %s, 'accepted', %s)",
            (case_id, DEMO_LESSON, NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=1)),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with school.transaction():
                school.execute(
                    "INSERT INTO offer (case_id, shift_id, staff_id, queue_pos, place, sent_at, expires_at, status, responded_at)"
                    " VALUES (%s, %s, 'S08', 2, '{}', %s, %s, 'accepted', %s)",
                    (case_id, DEMO_LESSON, NOW, NOW + timedelta(minutes=5), NOW + timedelta(minutes=2)),
                )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with school.transaction():
                school.execute(
                    "INSERT INTO cover_case (shift_id, source, opened_at) VALUES (%s, 'manager', %s)",
                    (DEMO_LESSON, NOW),
                )


def test_the_week_has_no_violation_outside_the_designed_traps(school):
    """Coordinator, 2026-11-07: nobody with an expired, revoked or disputed card or an overdue renewal stands on a
    shift of the week; the only holder trap is S07's Monday 16:00 lesson, which the night check must find."""
    from coachcover import binder

    at = NOW
    found = []
    for venue in ("V1", "V2", "V3"):
        for row in binder.build(school, POLICY_ID, venue, None, None, at)["shifts"]:
            if not row["ok"] and row["staff_id"]:
                found.append((row["date"], row["time"], row["staff_id"]))
    assert found == [("2026-11-09", "16:00-16:45", "S07")]
