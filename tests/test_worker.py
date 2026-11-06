"""The worker keeps its work in the database: a restart loses no offer timer; the night check runs once."""

from datetime import timedelta

import pytest

from coachcover.demo import reset_school
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import NOW, POLICY_ID
from coachcover.worker import tick

pytestmark = pytest.mark.db


def test_restart_loses_no_offer_timer(new_database):
    conn = new_database()
    reset_school(conn)
    p = Pipeline(conn, ModelClient(DbLedger(conn)), POLICY_ID)
    case = p.callout("S05", now=NOW, text="Привет, это Марина. У меня температура 38, я не выйду в 6. У меня в 6 дорожка и в 7 урок у малышей. Прости.")["cases"][0]["case_id"]
    first = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchone()
    # the worker was down while the 5-minute timer ran out; a fresh worker picks it up from the database
    assert tick(conn, POLICY_ID, NOW + timedelta(minutes=4))["expired_offers"] == 0
    assert tick(conn, POLICY_ID, NOW + timedelta(minutes=9))["expired_offers"] >= 1
    assert conn.execute("SELECT status FROM offer WHERE id = %s", (first[0],)).fetchone() == ("expired",)
    nxt = conn.execute("SELECT staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchone()
    assert nxt is not None and nxt[0] != first[1]
    assert tick(conn, POLICY_ID, NOW + timedelta(minutes=9))["expired_offers"] == 0  # nothing twice


def test_night_check_runs_once_and_opens_a_case_as_the_policy_says(new_database):
    conn = new_database()
    reset_school(conn)
    sunday_evening = NOW.replace(day=8, hour=21, minute=30)
    first = tick(conn, POLICY_ID, sunday_evening)["night_report"]
    assert first["for_date"] == "2026-11-09" and first["created"] is True and first["problems"] == 1
    assert first["cases"] == ["C-1108-01"]
    again = tick(conn, POLICY_ID, sunday_evening + timedelta(minutes=5))["night_report"]  # another tick or a restart
    assert again["created"] is False and again["cases"] == []
    assert conn.execute("SELECT count(*) FROM night_report").fetchone() == (1,)
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = (SELECT shift_id FROM cover_case)").fetchone() == ("S07", "scheduled")
    assert conn.execute("SELECT action, status FROM approval").fetchone() == ("night_cover", "pending")
    report = conn.execute("SELECT data FROM night_report").fetchone()[0]
    assert [p["staff_id"] for p in report["problems"]] == ["S07"]
    assert all(e["days_left"] <= 30 for e in report["expiring_30"])
    assert tick(conn, POLICY_ID, NOW)["night_report"] is None  # before the night hour nothing is written
