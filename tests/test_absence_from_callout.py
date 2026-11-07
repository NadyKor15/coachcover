"""A call-out counts from the moment it comes, to the end of that local day: a shift that ended before it is not
touched (Marina taught her 09:30 lesson and fell ill by midday), a shift under way when it comes is.

Live, 2026-11-07: after the private scene (call-out at 12:10) the inspector's folder marked her 09:30-10:15 lesson
"VIOLATION: called out for 2026-11-09", although it had ended at 10:15."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from coachcover import binder, seed
from coachcover.checks import night_report
from coachcover.demo import CALLOUT_DELAY, LIAM_MESSAGE, VOICES, run_scenario
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.repo import load_absences, load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, eligible

pytestmark = pytest.mark.db

NY = ZoneInfo("America/New_York")
MARINA, MORNING, PRIVATE = "S05", seed.HERO_SHIFT, seed.HERO_PRIVATE  # her 09:30-10:15 class and 17:00 private lesson


def at(hhmm: str) -> datetime:
    h, m, *s = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 11, 9, h, m, s[0] if s else 0, tzinfo=NY)


def marina_called_out_for(conn, shift_id: str) -> bool:
    """The rules' answer for Marina on that shift, as the inspector's folder asks it (at the shift's start)."""
    policy, shift = load_policy(conn, seed.POLICY_ID), load_shift(conn, shift_id)
    person = next(s for s in load_staff(conn, seed.POLICY_ID) if s.id == MARINA)
    check = eligible(person, replace(shift, assignee=None), shift.start, policy, load_context(conn, seed.POLICY_ID, shift))
    return Code.CALLED_OUT in check.codes


def folder_rows(conn) -> dict:
    data = binder.build(conn, seed.POLICY_ID, "V1", date(2026, 11, 9), date(2026, 11, 9), at("23:00"))
    return {row["shift_id"]: row for row in data["shifts"]}


def test_a_call_out_at_noon_leaves_the_lesson_she_already_taught_alone(new_database):
    conn = new_database()
    run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "private_lesson")  # her note at 12:10:10
    assert conn.execute("SELECT shift_id FROM cover_case").fetchall() == [(PRIVATE,)]  # the 17:00 lesson is the case
    windows = load_absences(conn, seed.POLICY_ID, at("00:00"), at("23:59"))[MARINA]
    assert windows == ((at("12:10:10"), at("00:00") + timedelta(days=1)),)  # from the call-out to the end of the day
    assert not marina_called_out_for(conn, MORNING)  # 09:30-10:15 ended before 12:10
    morning = folder_rows(conn)[MORNING]
    assert morning["person"] == "Marina Petrova" and morning["ok"] and "called out" not in morning["result"]
    report = night_report(conn, seed.POLICY_ID, date(2026, 11, 9), at("12:20"))
    assert "called out" not in str(report).casefold()
    assert "Javier Morales" in str(folder_rows(conn))  # the demo's other folder record stays as it is


def test_a_call_out_before_the_first_lesson_opens_both_and_blocks_both(new_database):
    conn = new_database()
    seed.load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    voice = VOICES["preschool"]
    p.callout(MARINA, now=seed.NOW + CALLOUT_DELAY["preschool"], audio=voice.read_bytes(), filename=voice.name)
    assert sorted(s for (s,) in conn.execute("SELECT shift_id FROM cover_case")) == sorted([MORNING, PRIVATE])
    assert load_absences(conn, seed.POLICY_ID, at("00:00"), at("23:59"))[MARINA][0][0] == at("08:40:10")
    assert marina_called_out_for(conn, MORNING) and marina_called_out_for(conn, PRIVATE)


def test_a_call_out_in_the_middle_of_a_shift_touches_that_shift(new_database):
    """09:45, during her 09:30-10:15 class: that class is still ahead of nobody, but it is under way, so it is hers to
    hand over (a case) and she is out of it, like the 17:00 lesson."""
    conn = new_database()
    seed.load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    p.callout(MARINA, now=at("09:45"), text=LIAM_MESSAGE)  # a text with a prepared reading: out today, no time named
    assert sorted(s for (s,) in conn.execute("SELECT shift_id FROM cover_case")) == sorted([MORNING, PRIVATE])
    assert marina_called_out_for(conn, MORNING) and marina_called_out_for(conn, PRIVATE)
    assert folder_rows(conn)[MORNING]["result"] == "no one on the post"  # its case is open: the folder shows it uncovered


def test_a_shift_that_ends_exactly_at_the_call_out_is_not_touched(new_database):
    conn = new_database()
    seed.load_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), seed.POLICY_ID)
    p.callout(MARINA, now=at("10:15"), text=LIAM_MESSAGE)  # the 09:30 class ends at 10:15
    assert [s for (s,) in conn.execute("SELECT shift_id FROM cover_case")] == [PRIVATE]
    assert not marina_called_out_for(conn, MORNING) and marina_called_out_for(conn, PRIVATE)
