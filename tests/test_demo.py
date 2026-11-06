"""Judge scenario on the demo clock: reset, one morning on recordings, same result every time."""

import pytest

from coachcover.demo import clock_now, reset_school, run_judge_scenario
from coachcover.models import DbLedger, ModelClient
from coachcover.seed import NOW

pytestmark = pytest.mark.db


def test_judge_scenario_covers_six_and_plans_seven(new_database):
    conn = new_database()
    reset_school(conn)
    assert abs((clock_now(conn) - NOW).total_seconds()) < 5
    first = run_judge_scenario(conn, ModelClient(DbLedger(conn), mode="replay"))
    lane, kids = first["cases"]["L-1109-G001"], first["cases"]["L-1109-G004"]
    answers = [(t["case_id"], t["staff_id"], t["answer"], f"{t['at']:%H:%M}") for t in first["timeline"]]
    assert answers[:2] == [(lane, "S08", "no", "05:14"), (lane, "S17", "yes", "05:18")]
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = 'L-1109-G001'").fetchone() == ("S17", "scheduled")
    assert first["plan_b"]["option"] == "lead" and first["plan_b"]["staff_id"] == "S06"
    assert conn.execute("SELECT status, decided_by FROM plan_b WHERE case_id = %s", (kids,)).fetchone() == ("approved", "judge")
    assert conn.execute("SELECT count(*) FROM model_call WHERE NOT ok").fetchone() == (0,)  # every model step had a recording
    assert abs((clock_now(conn) - first["ended_at"]).total_seconds()) < 5

    conn.execute("INSERT INTO ops.judge_run (client) VALUES ('203.0.113.7')")
    reset_school(conn)
    second = run_judge_scenario(conn, ModelClient(DbLedger(conn), mode="replay"))
    strip = lambda run: [(t["staff_id"], t["answer"], t["at"]) for t in run["timeline"]]  # noqa: E731
    assert strip(second) == strip(first)
    assert conn.execute("SELECT count(*) FROM ops.judge_run").fetchone() == (1,)  # the limit survives a reset
