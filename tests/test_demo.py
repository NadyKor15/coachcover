"""Judge scenarios on the demo clock: reset, one morning or night on recordings, the same result every time."""

import pytest

from coachcover.demo import SCENARIOS, clock_now, reset_school, run_scenario
from coachcover.models import DbLedger, ModelClient

pytestmark = pytest.mark.db


def _run(conn, scenario):
    return run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), scenario)


def test_each_scenario_runs_on_recordings_and_repeats(new_database):
    conn = new_database()
    reset_school(conn)
    conn.execute("INSERT INTO ops.judge_run (client) VALUES ('203.0.113.7')")
    results = {}
    for scenario in (s for s in SCENARIOS if s != "network_loan"):  # that one needs the agents' HTTP: test_network_loan_scenario.py
        first = _run(conn, scenario)
        failed = conn.execute("SELECT purpose, error FROM model_call WHERE NOT ok").fetchall()
        assert failed == [], (scenario, failed)  # every model step had a recorded answer
        assert abs((clock_now(conn) - first["ended_at"]).total_seconds()) < 5
        assert str(first["ended_at"].tzinfo) == "America/New_York", scenario  # venue time, as the screens show it
        timeline = conn.execute("SELECT kind, at FROM case_event ORDER BY case_id, seq").fetchall()
        second = _run(conn, scenario)
        assert second["case_id"] == first["case_id"]
        assert conn.execute("SELECT kind, at FROM case_event ORDER BY case_id, seq").fetchall() == timeline
        results[scenario] = first
    assert results["dangerous_morning"]["case_id"] == "C-1109-01"
    assert results["nobody_accepts"]["conflict"]["reason"] == "plan changed"
    assert results["nightly_check"]["case_id"] == "C-1108-01" and [o["staff_id"] for o in results["nightly_check"]["opened"]] == ["S07"]
    assert conn.execute("SELECT count(*) FROM ops.judge_run").fetchone() == (1,)  # the limit survives every reset
    # the private lesson's scenarios: Marina's midday note opens one case, her 17:00 private lesson with Lily
    assert results["private_lesson"]["case_id"] == results["private_declined"]["case_id"] == "C-1109-01"
    assert conn.execute("SELECT running, case_code FROM ops.judge_state").fetchone() == (False, "C-1109-01")


def test_unknown_scenario_is_refused(new_database):
    conn = new_database()
    reset_school(conn)
    with pytest.raises(ValueError):
        _run(conn, "sunny_day")
