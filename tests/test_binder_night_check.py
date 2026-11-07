"""Inspection binder and the night check's trap (S07, Monday 16:00, CPR ended Nov 8): caught and covered before the
shift, it is a line of its own, not a violation; without the night check it is an honest violation."""

from datetime import date, timedelta

import pytest

from coachcover import binder
from coachcover.demo import reset_school, run_scenario
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import NOW, POLICY_ID

pytestmark = pytest.mark.db
TRAP = "L-1109-G021"


@pytest.fixture
def school(new_database):
    conn = new_database()
    reset_school(conn)
    return conn


def _row(data, shift_id):
    return next(r for r in data["shifts"] if r["shift_id"] == shift_id)


def test_without_the_night_check_the_trap_is_a_violation(school):
    data = binder.build(school, POLICY_ID, "V1", None, None, NOW)
    # V1's week, with the 09:30 lesson's second seat, G121 and 7 private lessons
    assert data["summary"]["headline"] == "77 shifts · 1 violation"
    row = _row(data, TRAP)
    assert row["ok"] is False and row["result"].startswith("VIOLATION: CPR/AED expired")


def test_caught_by_the_night_check_and_covered_is_not_a_violation(school):
    models = ModelClient(DbLedger(school), mode="replay")
    run = run_scenario(school, models, "nightly_check")  # Sunday 21:30: case for S07's lesson, approval pending
    p = Pipeline(school, models, POLICY_ID)
    at = run["ended_at"] + timedelta(minutes=2)
    pending_case = binder.build(school, POLICY_ID, "V1", date(2026, 11, 9), date(2026, 11, 15), at)
    assert pending_case["summary"]["violations"] == 1  # caught but not yet covered: still honest
    approval = school.execute("SELECT id FROM approval WHERE action = 'night_cover' AND status = 'pending'").fetchone()[0]
    p.decide_approval(approval, True, at, by="manager:Natalie")
    first = school.execute("SELECT id FROM offer WHERE status = 'pending' ORDER BY queue_pos LIMIT 1").fetchone()[0]
    p.respond(first, True, at + timedelta(minutes=1))  # the top of the wave says yes: covered at once
    holder = school.execute("SELECT s.full_name FROM shift sh JOIN staff s ON s.id = sh.staff_id WHERE sh.id = %s",
                            (TRAP,)).fetchone()[0]
    data = binder.build(school, POLICY_ID, "V1", None, None, NOW)
    assert data["summary"]["headline"] == "77 shifts · 0 violations · 1 caught before the shift"
    assert _row(data, TRAP)["result"] == f"caught by the nightly check · case {run['case_id']} · covered by {holder}"
    assert binder.render(data)[:5] == b"%PDF-"


def test_an_open_night_check_case_stays_a_violation_and_says_it_was_caught(school):
    """Sunday 21:30, right after the night check: its case is open (approval pending, no replacement yet). The folder
    still counts the violation and says that the nightly check caught it and which case is finding a replacement; once
    the case is closed with a replacement, the line reads "caught … covered by …" and the violation is gone."""
    models = ModelClient(DbLedger(school), mode="replay")
    run = run_scenario(school, models, "nightly_check")
    at = run["ended_at"] + timedelta(minutes=2)
    data = binder.build(school, POLICY_ID, "V1", None, None, at)  # no dates: the week of the shifts it checked
    assert data["period"]["from"] == "2026-11-09" and data["period"]["to"] == "2026-11-15"
    s = data["summary"]
    assert (s["shifts"], s["violations"], s["caught"], s["caught_open"]) == (77, 1, 0, 1)
    assert s["headline"] == "77 shifts · 1 violation · 1 caught by the nightly check"
    row = _row(data, TRAP)
    assert row["ok"] is False and row["caught_open"] is True
    assert row["result"] == (f"VIOLATION: CPR/AED expired on 2026-11-08 · caught by the nightly check · case "
                             f"{run['case_id']} · replacement being found")
    text = binder.render(data, compress=False).decode("latin-1")
    assert "77 shifts" in text and "1 caught by the nightly check" in text and "replacement being found" in text
    p = Pipeline(school, models, POLICY_ID)
    approval = school.execute("SELECT id FROM approval WHERE action = 'night_cover' AND status = 'pending'").fetchone()[0]
    p.decide_approval(approval, True, at, by="manager:Natalie")
    first = school.execute("SELECT id FROM offer WHERE status = 'pending' ORDER BY queue_pos LIMIT 1").fetchone()[0]
    p.respond(first, True, at + timedelta(minutes=1))
    closed = binder.build(school, POLICY_ID, "V1", None, None, at + timedelta(minutes=2))
    assert (closed["summary"]["violations"], closed["summary"]["caught"], closed["summary"]["caught_open"]) == (0, 1, 0)
    assert _row(closed, TRAP)["result"].startswith("caught by the nightly check · case ")
    assert "caught_open" not in _row(closed, TRAP)


def test_the_folder_without_dates_opens_the_week_with_shifts_still_ahead(school):
    """The policy week that holds "now" (Monday 08:40: Nov 9-15); on Sunday night, when nothing of the week Nov 2-8 is
    ahead, the next week with shifts: Nov 9-15, not an empty "0 shifts"."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    monday = binder.build(school, POLICY_ID, "V1", None, None, NOW)
    sunday_night = binder.build(school, POLICY_ID, "V1", None, None, datetime(2026, 11, 8, 21, 30, tzinfo=ny))
    for data in (monday, sunday_night):
        assert (data["period"]["from"], data["period"]["to"], data["summary"]["shifts"]) == ("2026-11-09", "2026-11-15", 77)
    asked = binder.build(school, POLICY_ID, "V1", date(2026, 11, 2), None, NOW)  # dates given: taken as they are
    assert (asked["period"]["from"], asked["period"]["to"], asked["summary"]["shifts"]) == ("2026-11-02", "2026-11-08", 0)
