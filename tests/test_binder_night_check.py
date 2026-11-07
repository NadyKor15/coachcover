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
    assert data["summary"]["headline"] == "68 shifts · 1 violation"
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
    p.decide_approval(approval, True, at, by="manager:Dana")
    first = school.execute("SELECT id FROM offer WHERE status = 'pending' ORDER BY queue_pos LIMIT 1").fetchone()[0]
    p.respond(first, True, at + timedelta(minutes=1))  # the top of the wave says yes: covered at once
    holder = school.execute("SELECT s.full_name FROM shift sh JOIN staff s ON s.id = sh.staff_id WHERE sh.id = %s",
                            (TRAP,)).fetchone()[0]
    data = binder.build(school, POLICY_ID, "V1", None, None, NOW)
    assert data["summary"]["headline"] == "68 shifts · 0 violations · 1 caught before the shift"
    assert _row(data, TRAP)["result"] == f"caught by the nightly check · case {run['case_id']} · covered by {holder}"
    assert binder.render(data)[:5] == b"%PDF-"
