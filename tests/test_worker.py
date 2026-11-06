"""The worker keeps its work in the database: a restart loses no offer timer and writes the night report once."""

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
    result = tick(conn, POLICY_ID, NOW + timedelta(minutes=9))
    assert result["expired_offers"] >= 1
    assert conn.execute("SELECT status FROM offer WHERE id = %s", (first[0],)).fetchone() == ("expired",)
    nxt = conn.execute("SELECT staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case,)).fetchone()
    assert nxt is not None and nxt[0] != first[1]
    assert tick(conn, POLICY_ID, NOW + timedelta(minutes=9))["expired_offers"] == 0  # nothing twice


def test_night_report_is_written_once_and_finds_tomorrows_problems(new_database):
    conn = new_database()
    reset_school(conn)
    evening = NOW.replace(hour=21, minute=30)  # Monday 21:30 local -> report for Tuesday
    first = tick(conn, POLICY_ID, evening)["night_report"]
    assert first["for_date"] == "2026-11-10" and first["created"] is True
    again = tick(conn, POLICY_ID, evening + timedelta(minutes=5))["night_report"]  # another tick or a restarted worker
    assert again["created"] is False
    assert conn.execute("SELECT count(*) FROM night_report").fetchone() == (1,)
    report = conn.execute("SELECT data FROM night_report").fetchone()[0]
    flagged = {p["staff_id"] for p in report["problems"]}
    tuesday_staff = {s for (s,) in conn.execute(
        "SELECT DISTINCT staff_id FROM shift WHERE starts_at >= '2026-11-10 00:00 America/New_York'"
        " AND starts_at < '2026-11-11 00:00 America/New_York' AND staff_id IN ('S07', 'S08', 'S10', 'S13', 'S14')")}
    assert tuesday_staff and flagged >= tuesday_staff  # every trap person working Tuesday is flagged
    assert all(e["days_left"] <= 30 for e in report["expiring_30"])
    assert tick(conn, POLICY_ID, NOW)["night_report"] is None  # before the night hour nothing is written
