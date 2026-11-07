"""The founder's morning (preschool story of coachcover/seed.py) end to end through the judge scenarios, on replay.

07:10 Marina's voice note (synthesized locally, prepared answers marked synthetic) -> her 08:00 "Preschool swim, ages
3-5" with Emma Carter -> 5 of 40 cleared -> Sofia no 07:13, Zoe yes 07:14, Leo silent, Zoe gets it 07:15.
nobody_accepts: plan B 07:15:50 -> Liam Foster (reserve) calls out 07:16:50 -> the shown plan 409 at 07:17:10.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from coachcover.demo import run_scenario
from coachcover.models import DbLedger, ModelClient
from coachcover.screens import today

pytestmark = pytest.mark.db

NY = ZoneInfo("America/New_York")
HERO, SEAT = "L-1109-G002", "L-1109-G002-I2"


def at(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 11, 9, h, m, s, tzinfo=NY)


def run(new_database, scenario):
    conn = new_database()
    result = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), scenario)
    case = conn.execute("SELECT id FROM cover_case WHERE code = %s", (result["case_id"],)).fetchone()[0]
    return conn, result, case


def test_dangerous_morning_is_the_founders_morning(new_database):
    conn, result, case = run(new_database, "dangerous_morning")
    assert result["case_id"] == "C-1109-01" and result["ended_at"] == at("07:15:00")
    shift_id, opened = conn.execute("SELECT shift_id, opened_at FROM cover_case WHERE id = %s", (case,)).fetchone()
    assert shift_id == HERO and opened == at("07:10:10")
    offers = conn.execute("SELECT staff_id, status, sent_at, responded_at FROM offer WHERE case_id = %s ORDER BY queue_pos",
                          (case,)).fetchall()
    assert [(s, st) for s, st, _, _ in offers] == [("S25", "declined"), ("S26", "withdrawn"), ("S17", "accepted")]
    assert offers[0][3] == at("07:13:00") and offers[2][3] == at("07:14:00") and offers[1][3] is None
    covered = conn.execute("SELECT at FROM case_event WHERE case_id = %s AND kind = 'shift_covered'", (case,)).fetchone()[0]
    assert covered == at("07:15:00")
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id IN (%s, %s) ORDER BY id", (HERO, SEAT)).fetchall() == [
        ("S17", "scheduled"), ("S01", "scheduled")]  # Zoe takes Marina's place, Emma Carter stays on the lesson
    ranked = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'candidates_ranked'", (case,)).fetchone()[0]
    assert (ranked["total"], ranked["eligible"]) == (40, 5)
    assert [g["staff_id"] for g in ranked["grid"] if g["eligible"]] == ["S25", "S26", "S17", "S20", "S22"]
    calls = conn.execute("SELECT purpose, coalesce((ref->>'synthetic')::boolean, false), ok FROM model_call ORDER BY at, id").fetchall()
    assert all(ok for _, _, ok in calls) and all(synthetic for _, synthetic, _ in calls)  # every answer prepared, none live
    screen = today(conn, "demo-school", at("07:15:00"), None, live=False)
    assert screen["title"] == "Who is allowed on deck at 08:00" and screen["now_label"] == "Mon, Nov 9 · 07:15"
    assert screen["shift"]["lanes_label"] == "Teaching pool + Preschool swim, ages 3–5 (8 kids)"
    assert screen["callout"]["staff"] == "Marina Petrova · Instructor · with Emma Carter"  # the teacher who stays
    chips = {r["label"]: r["count"] for r in screen["eligibility"]["reasons"]}
    assert chips["No preschool training (ages 3–5)"] == 10
    assert screen["eligibility"]["first_refusal"]["label"].startswith("free and willing · BLOCKED: WSI certificate expired Nov 5")
    assert screen["money"]["uncovered_morning_usd"] == 240.0  # 8 children x $30


def test_nobody_accepts_then_the_reserve_calls_out_and_the_shown_plan_is_refused(new_database):
    conn, result, case = run(new_database, "nobody_accepts")
    assert result["conflict"]["reason"] == "plan changed" and result["ended_at"] == at("07:17:10")
    plans = conn.execute("SELECT id, status, created_at, snapshot FROM plan_b WHERE case_id = %s ORDER BY id", (case,)).fetchall()
    assert [(st, created) for _, st, created, _ in plans] == [("superseded", at("07:15:50")), ("proposed", at("07:17:10"))]
    assert [o["kind"] for o in plans[0][3]["options"]] == ["lead", "overtime", "reschedule", "close"]
    assert plans[0][3]["options"][0]["staff_name"] == "Liam Foster"
    assert [o["kind"] for o in plans[1][3]["options"]] == ["overtime", "reschedule", "close"]
    assert plans[1][3]["options"][0]["staff_name"] == "Camila Herrera"
    liam = conn.execute("SELECT c.code, s.venue_id, c.opened_at FROM cover_case c JOIN shift s ON s.id = c.shift_id"
                        " WHERE c.absent_staff_id = 'S06' ORDER BY c.id").fetchall()
    assert liam and {v for _, v, _ in liam} == {"V2"} and {o for _, _, o in liam} == {at("07:16:50")}
    refused = conn.execute("SELECT at, data FROM case_event WHERE case_id = %s AND kind = 'approval_refused'", (case,)).fetchone()
    assert refused[0] == at("07:17:10")
    assert (refused[1]["plan_id"], refused[1]["current_plan_id"]) == (f"P-{plans[0][0]}", f"P-{plans[1][0]}")  # shown, new
    assert refused[1]["why"] == ("Liam Foster (reserve) called out at 07:16: “A pipe burst at my place, I can't come in today."
                                 " Sorry.”")
    screen = today(conn, "demo-school", at("07:17:10"), None, live=False)
    assert [(t["time_label"], t.get("label")) for t in screen["timeline"][-4:]] == [
        ("07:15", "Plan B ready"), ("07:16", "Liam Foster called out"), ("07:17", "New plan B ready"), ("08:00", None)]
    assert screen["outcome"]["lessons_cancelled"] == 0  # nothing closes: the reserve coach called out
    # Riverside's screen counts Riverside's call-outs: Marina's 8 children, not Liam's lessons at Maplewood
    assert screen["money"]["uncovered_morning_usd"] == 240.0
