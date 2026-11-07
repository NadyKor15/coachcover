"""Offer waves (product decision): three at once in queue order, the highest "yes" in a 60 s window wins, the others
stand down; a "yes" after the shift is covered is turned down politely; plan B goes out 20 min before the start."""

from datetime import timedelta

import pytest

from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Conflict, Pipeline
from coachcover.presets import default_policy
from coachcover.seed import NOW, POLICY_ID, load_school
from tests.test_pipeline import HERO, RU_VOICE

pytestmark = pytest.mark.db


def at(minutes: float):
    return NOW + timedelta(minutes=minutes)


@pytest.fixture
def morning(new_database):
    conn = new_database()
    load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
    result = p.callout("S05", now=NOW, audio=RU_VOICE, filename="ru-callout.ogg")
    case = next(c["case_id"] for c in result["cases"] if c["shift_id"] == HERO)
    return conn, p, case


def offers(conn, case):
    return dict((sid, oid) for oid, sid in conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s", (case,)))


def statuses(conn, case):
    return conn.execute("SELECT staff_id, status FROM offer WHERE case_id = %s ORDER BY queue_pos", (case,)).fetchall()


def test_the_preset_sets_the_waves():
    preset = default_policy("swim_school").preset
    assert (preset.offer_wave_size, preset.offer_collect_seconds, preset.plan_b_minutes_before_start) == (3, 60, 20)


def test_two_yes_in_the_window_the_one_higher_in_the_queue_gets_the_shift(morning):
    conn, p, case = morning
    o = offers(conn, case)
    assert list(o) == ["S25", "S26", "S17"]  # Sofia, Leo, Zoe: one wave
    p.respond(o["S17"], True, at(2))  # 05:14 Zoe: yes (Sofia and Leo, higher, have not answered)
    p.respond(o["S26"], True, at(2.25))  # 05:14:15 Leo: yes, still below Sofia, who may answer
    assert statuses(conn, case) == [("S25", "pending"), ("S26", "held"), ("S17", "held")]
    p.expire_due(at(2.9))  # 55 s after the first yes: still collecting
    assert conn.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == (None,)
    p.expire_due(at(3))  # 05:15, 60 s after Zoe's yes: the higher yes wins
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S26", "scheduled")
    assert statuses(conn, case) == [("S25", "withdrawn"), ("S26", "accepted"), ("S17", "withdrawn")]
    texts = dict(conn.execute("SELECT staff_id, body FROM message WHERE case_id = %s AND audience = 'staff'"
                              " AND body LIKE '%%Leo%%'", (case,)).fetchall())
    assert set(texts) == {"S25", "S17"}  # Sofia (silent) and Zoe (the lower yes) both stand down, in Spanish
    assert texts["S17"] == "CoachCover: gracias — Leo ya cubrió Mon 09.11 06:00-08:00."


def test_the_top_of_the_wave_saying_yes_needs_no_window(morning):
    conn, p, case = morning
    o = offers(conn, case)
    state = p.respond(o["S25"], True, at(1))  # Sofia is first: nobody can beat her
    assert (state["status"], state["case_status"]) == ("accepted", "covered")
    assert statuses(conn, case) == [("S25", "accepted"), ("S26", "withdrawn"), ("S17", "withdrawn")]


def test_a_yes_after_the_shift_is_covered_is_turned_down_politely(morning):
    conn, p, case = morning
    o = offers(conn, case)
    p.respond(o["S25"], True, at(1))
    with pytest.raises(Conflict, match="Thanks — Sofia already covered it") as late:
        p.respond(o["S17"], True, at(1.5))
    assert late.value.detail["covered_by"] == "Sofia Reyes"
    assert conn.execute("SELECT staff_id FROM shift WHERE id = %s", (HERO,)).fetchone() == ("S25",)


def test_nobody_in_the_wave_answers_then_the_next_wave(morning):
    conn, p, case = morning
    p.expire_due(at(5))  # 05:17: all three timers ran out
    assert [s for s, st in statuses(conn, case) if st == "pending"] == ["S20", "S22"]  # Mateo, Ethan: wave 2
    assert conn.execute("SELECT DISTINCT wave FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchall() == [(2,)]


def test_before_the_deadline_plan_b_waits_for_the_queue(morning):
    conn, p, case = morning
    p.expire_due(at(5))  # 05:17: wave 1 ran out, wave 2 goes out; 06:00 - 20 min is still ahead
    p.expire_due(at(9.9))
    assert conn.execute("SELECT count(*) FROM plan_b WHERE case_id = %s", (case,)).fetchone() == (0,)
    p.expire_due(at(10))  # 05:22: wave 2 ran out, nobody is left: plan B now, because the queue is empty
    kinds = [k for (k,) in conn.execute("SELECT kind FROM case_event WHERE case_id = %s ORDER BY seq", (case,))]
    assert "queue_exhausted" in kinds and "plan_b_deadline" not in kinds
    assert conn.execute("SELECT status FROM plan_b WHERE case_id = %s", (case,)).fetchall() == [("proposed",)]


def test_plan_b_by_the_deadline_does_not_wait_for_the_queue(new_database):
    conn = new_database()
    load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
    late_callout = at(26)  # 05:38: two minutes before the deadline, the first wave goes out
    cases = p.callout("S05", now=late_callout, text="Marina here, I am sick, I can't come to the 6 am lesson.")["cases"]
    case = next(c["case_id"] for c in cases if c["shift_id"] == HERO)
    assert conn.execute("SELECT count(*) FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchone() == (3,)
    p.expire_due(at(28))  # 05:40: the offers still have 3 minutes, plan B goes out anyway
    kinds = [k for (k,) in conn.execute("SELECT kind FROM case_event WHERE case_id = %s ORDER BY seq", (case,))]
    assert "plan_b_deadline" in kinds and "queue_exhausted" not in kinds
    assert conn.execute("SELECT status FROM plan_b WHERE case_id = %s", (case,)).fetchall() == [("proposed",)]
    assert conn.execute("SELECT count(*) FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchone() == (3,)
    # a "yes" still covers the shift; the proposed plan is no longer needed
    first = conn.execute("SELECT id FROM offer WHERE case_id = %s ORDER BY queue_pos LIMIT 1", (case,)).fetchone()[0]
    assert p.respond(first, True, at(29))["case_status"] == "covered"
    assert conn.execute("SELECT status FROM plan_b WHERE case_id = %s", (case,)).fetchall() == [("superseded",)]
