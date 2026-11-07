"""A broken children-per-instructor norm that nobody covers: plan B may never leave a lesson over the norm
(coordinator, after review: a 6:1 lesson is a safety defect). Options: a reserve person takes the seat, another
cleared instructor at the same time takes the newest children, the newest children move to the nearest lesson of
their level with room; with none of these the whole lesson is cancelled with credits."""

from datetime import timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.repo import load_policy, load_shift
from coachcover.rules import group_norm
from coachcover.seed import NOW, POLICY_ID, load_school

pytestmark = pytest.mark.db

ALLOWED = {"lead", "split", "move", "cancel_lesson"}
T = NOW + timedelta(minutes=1)


@pytest.fixture
def school(new_database):
    conn = new_database()
    load_school(conn)
    return conn


def pipeline(conn, **autonomy):
    if autonomy:
        conn.execute("UPDATE policy SET autonomy = autonomy || %s WHERE id = %s", (Jsonb(autonomy), POLICY_ID))
    return Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)


def over_the_norm(conn, p) -> list[str]:
    """Every lesson (not cancelled) whose children exceed the norm for the instructors it really has."""
    preset = load_policy(conn, POLICY_ID).preset
    bad = []
    for (lesson_id,) in conn.execute(
        "SELECT s.id FROM shift s WHERE s.extra_for IS NULL AND s.status <> 'cancelled'"
        " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id) ORDER BY s.id"
    ).fetchall():
        lesson = load_shift(conn, lesson_id)
        effective, _, _ = p._lesson_seats(lesson_id)
        per = min(group_norm(g, preset) for g in lesson.groups)
        if sum(g.size for g in lesson.groups) > per * effective:
            bad.append(lesson_id)
    return bad


def nobody_takes_it(conn, p, case_id):
    """Every eligible person says no, wave after wave, until plan B is proposed."""
    t = T
    while True:
        rows = conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending' ORDER BY queue_pos",
                            (case_id,)).fetchall()
        if not rows:
            return t
        for (offer_id,) in rows:
            t += timedelta(seconds=10)
            p.respond(offer_id, False, t)


def test_nobody_takes_the_seat_the_plan_keeps_the_norm(school):
    p = pipeline(school)
    case = p.enroll("G003", 2, NOW, language="es")["alerts"][0]["cases"][0]["case_id"]
    case_id = p.find_case(case)
    t = nobody_takes_it(school, p, case_id)
    plan = school.execute("SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'",
                          (case_id,)).fetchone()
    kinds = [o["kind"] for o in plan[1]["options"]]
    assert set(kinds) <= ALLOWED and "close" not in kinds and "reschedule" not in kinds
    assert over_the_norm(school, p) == ["L-1109-G003"]  # while the seat is open the lesson is 6:1
    p.approve_plan_b(plan[0], plan[2], t + timedelta(minutes=1), by="manager")
    assert over_the_norm(school, p) == []


def test_without_a_reserve_or_room_anywhere_the_whole_lesson_is_cancelled_with_credits(school):
    school.execute("UPDATE staff SET active = false WHERE id IN ('S06', 'S12', 'S23')")  # no reserve this morning
    p = pipeline(school, merge_groups="off")  # no moving children either
    case_id = p.find_case(p.enroll("G003", 2, NOW, language="es")["alerts"][0]["cases"][0]["case_id"])
    t = nobody_takes_it(school, p, case_id)
    plan = school.execute("SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'",
                          (case_id,)).fetchone()
    assert [o["kind"] for o in plan[1]["options"]] == ["cancel_lesson"]
    p.approve_plan_b(plan[0], plan[2], t + timedelta(minutes=1), by="manager")
    assert school.execute("SELECT id, status FROM shift WHERE id LIKE 'L-1109-G003%%' ORDER BY id").fetchall() == [
        ("L-1109-G003", "cancelled"), ("L-1109-G003-I2", "cancelled")]
    assert school.execute("SELECT kind, families FROM remedy WHERE shift_id = 'L-1109-G003'").fetchall() == [("credit", 6)]
    assert over_the_norm(school, p) == []
    # never "6 children for 1 instructor": the lesson does not run at all
    assert school.execute("SELECT status FROM cover_case WHERE id = %s", (case_id,)).fetchone() == ("plan_b",)


def test_moving_the_newest_children_tells_their_families_in_their_language(school):
    school.execute("UPDATE staff SET active = false WHERE id IN ('S06', 'S12', 'S23')")
    p = pipeline(school)
    case_id = p.find_case(p.enroll("G003", 2, NOW, language="es")["alerts"][0]["cases"][0]["case_id"])
    t = nobody_takes_it(school, p, case_id)
    plan = school.execute("SELECT id, snapshot, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'",
                          (case_id,)).fetchone()
    options = plan[1]["options"]
    assert options[0]["kind"] in ("split", "move") and options[0]["children"] == 2 and options[0]["families"] == {"es": 2}
    p.approve_plan_b(plan[0], plan[2], t + timedelta(minutes=1), by="manager")
    assert school.execute("SELECT size FROM lesson_group WHERE id = 'G003'").fetchone() == (4,)
    texts = school.execute("SELECT language, body FROM message WHERE case_id = %s AND audience = 'family'", (case_id,)).fetchall()
    assert [lang for lang, _ in texts] == ["es"] and texts[0][1].startswith("CoachCover: ")
    assert school.execute("SELECT status FROM shift WHERE id = 'L-1109-G003-I2'").fetchone() == ("cancelled",)
    assert over_the_norm(school, p) == []


def test_every_plan_b_option_for_every_group_of_the_training_school_keeps_the_norm(school):
    """Property over the whole training school: break the norm of each group's lesson, let nobody take the seat,
    then apply every option plan B offers; after each one no lesson is over its norm."""
    p = pipeline(school, assign_cover="off")  # nobody is asked: plan B straight away
    assert over_the_norm(school, p) == []  # the school starts within the norms
    preset = load_policy(school, POLICY_ID).preset
    early = NOW.replace(hour=5, minute=0)  # before the first lesson of the week, so every group is still ahead
    t = early + timedelta(minutes=1)
    groups = school.execute(
        "SELECT g.id, s.id FROM lesson_group g JOIN shift_group sg ON sg.group_id = g.id JOIN shift s ON s.id = sg.shift_id"
        " WHERE s.extra_for IS NULL AND s.status = 'scheduled' AND s.starts_at > %s ORDER BY g.id", (early,)
    ).fetchall()
    assert len(groups) > 100
    seen, applied = set(), 0
    for group_id, lesson_id in groups:
        lesson = load_shift(school, lesson_id)
        per = min(group_norm(g, preset) for g in lesson.groups)
        add = per * lesson.instructors - sum(g.size for g in lesson.groups) + 2  # two over the norm
        with school.transaction() as outer:
            alerts = p.enroll(group_id, add, early, language="es")["alerts"]
            assert alerts, group_id
            for case in (c for a in alerts for c in a["cases"]):
                case_id = p.find_case(case["case_id"])
                plan = p.propose_plan_b(case_id, t)
                options = plan["snapshot"]["options"]
                assert {o["kind"] for o in options} <= ALLOWED, (group_id, options)
                kinds = [o["kind"] for o in options]
                assert "cancel_lesson" not in kinds or kinds == ["cancel_lesson"], group_id  # the last resort only
                for index, option in enumerate(options):
                    seen.add(option["kind"])
                    with school.transaction() as inner:
                        p.approve_plan_b(plan["plan_id"], plan["hash"], t, by="manager", option=index)
                        assert over_the_norm(school, p) == [], (group_id, option["kind"])
                        applied += 1
                        raise psycopg.Rollback(inner)
            raise psycopg.Rollback(outer)
    # every group has an admissible option on the training week (cancel_lesson: the dedicated test above)
    assert len(groups) == 121 and applied >= len(groups) and {"lead", "split", "move"} <= seen <= ALLOWED
