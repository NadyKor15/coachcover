"""Fragile shifts of the week: one definition, the legend from the same thresholds, fixes that hold up."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from coachcover import forecast
from coachcover.demo import clock_now, reset_school
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import eligible
from coachcover.seed import NOW, POLICY_ID
from tests.conftest import TEST_DSN
from tests.test_ext_a2a import ext_app

WEEK = date(2026, 11, 8)


# --- pure: legend and levels -------------------------------------------------------------

@pytest.mark.parametrize("threshold", [0, 1, 2, 5])
def test_every_count_has_exactly_one_level_from_the_legend(threshold):
    items = forecast.legend(threshold)
    assert items[0] == {"level": "at_risk", "label": "0 backups", "min": 0, "max": 0, "tone": "red"}
    assert items[-1]["level"] == "safe" and items[-1]["min"] == threshold + 1 and items[-1]["max"] is None
    assert ("fragile" in [i["level"] for i in items]) == (threshold >= 1)
    for n in range(0, threshold + 4):
        matching = [i for i in items if n >= i["min"] and (i["max"] is None or n <= i["max"])]
        assert len(matching) == 1 and forecast.level_of(n, threshold) == matching[0]["level"]
    assert forecast.level_of(0, threshold) == "at_risk" and forecast.level_of(threshold + 1, threshold) == "safe"


def test_default_threshold_is_one():
    assert forecast.level_of(1, forecast.DEFAULT_THRESHOLD) == "fragile"
    assert forecast.level_of(2, forecast.DEFAULT_THRESHOLD) == "safe"


# --- on the training school --------------------------------------------------------------

@pytest.fixture
def school_db(new_database):
    conn = new_database()
    reset_school(conn)
    return conn


def cells(report: dict) -> list[dict]:
    return [c for d in report["days"] for c in d["cells"]]


def recount(conn, shift_id: str, now, staff=None) -> int:
    """Backups counted independently: same-venue, non-reserve people the rules clear, holder excluded."""
    policy = load_policy(conn, POLICY_ID)
    shift = load_shift(conn, shift_id)
    context = load_context(conn, POLICY_ID, shift)
    reserve = {r.code for r in policy.preset.roles.values() if r.reserve}
    people = staff if staff is not None else load_staff(conn, POLICY_ID)
    return sum(1 for s in people if not (s.roles & reserve) and s.home_venue == shift.venue
               and eligible(s, shift, now, policy, context).ok)


@pytest.mark.db
def test_list_count_equals_cells_at_risk_or_fragile(school_db):
    conn = school_db
    now = clock_now(conn)
    for venue in (None, "V1", "V2"):
        report = forecast.week(conn, POLICY_ID, now, venue, WEEK)
        thin = [c["shift_id"] for c in cells(report) if c["level"] in ("at_risk", "fragile")]
        assert report["fragile"] == thin
        assert report["counts"]["fragile_total"] == len(thin) == report["counts"]["at_risk"] + report["counts"]["fragile"]
        assert report["legend"] == forecast.legend(report["threshold"])
        assert sum(report["counts"][k] for k in ("at_risk", "fragile", "safe", "past")) == report["counts"]["shifts"]
        assert all(forecast.level_of(c["backups"], report["threshold"]) == c["level"] for c in cells(report) if not c["past"])
    whole = forecast.week(conn, POLICY_ID, now, None, WEEK)
    assert whole["counts"]["fragile_total"] > 0, "the training week should have thin shifts to show"
    assert whole["headline"] == f"{whole['counts']['fragile_total']} fragile shifts this week"


@pytest.mark.db
def test_backups_match_an_independent_count_and_react_to_data(school_db):
    conn = school_db
    now = clock_now(conn)
    report = forecast.week(conn, POLICY_ID, now, None, WEEK)
    upcoming = [c for c in cells(report) if not c["past"]]
    sample = upcoming[:: max(1, len(upcoming) // 25)]
    for c in sample:
        assert c["backups"] == recount(conn, c["shift_id"], now), c["shift_id"]
    # must-differ: take one backup away and the count drops by one
    cell = next(c for c in upcoming if c["backups"] >= 1 and c["backup_names"])
    first = conn.execute("SELECT id FROM staff WHERE full_name = %s", (cell["backup_names"][0],)).fetchone()[0]
    conn.execute("UPDATE staff SET active = false WHERE id = %s", (first,))
    again = next(c for c in cells(forecast.week(conn, POLICY_ID, now, None, WEEK)) if c["shift_id"] == cell["shift_id"])
    assert again["backups"] == cell["backups"] - 1


@pytest.mark.db
def test_past_shifts_are_not_counted(school_db):
    conn = school_db
    report = forecast.week(conn, POLICY_ID, NOW + timedelta(hours=3), None, WEEK)
    past = [c for c in cells(report) if c["past"]]
    assert past and all(c["level"] == "past" and c["backups"] is None for c in past)
    assert not set(report["fragile"]) & {c["shift_id"] for c in past}


@pytest.mark.db
def test_threshold_and_scope_come_from_the_preset(school_db):
    conn = school_db
    now = clock_now(conn)
    base = forecast.week(conn, POLICY_ID, now, None, WEEK)
    code = conn.execute("SELECT preset_code FROM policy WHERE id = %s", (POLICY_ID,)).fetchone()[0]
    conn.execute("UPDATE preset SET source = source || %s WHERE code = %s",
                 (Jsonb({"forecast": {"fragile_max_backups": 2}}), code))
    wider = forecast.week(conn, POLICY_ID, now, None, WEEK)
    assert wider["threshold"] == 2 and wider["legend"][1] == {"level": "fragile", "label": "1–2 backups", "min": 1,
                                                              "max": 2, "tone": "gold"}
    assert wider["counts"]["fragile_total"] > base["counts"]["fragile_total"]
    conn.execute("UPDATE preset SET source = source || %s WHERE code = %s",
                 (Jsonb({"forecast": {"fragile_max_backups": 1, "backups_from": "policy"}}), code))
    school_wide = forecast.week(conn, POLICY_ID, now, None, WEEK)
    assert school_wide["backups_from"] == "policy"
    assert school_wide["counts"]["fragile_total"] <= base["counts"]["fragile_total"]
    conn.execute("UPDATE preset SET source = source || %s WHERE code = %s",
                 (Jsonb({"forecast": {"fragile_max_backups": -1}}), code))
    with pytest.raises(ValueError):
        forecast.week(conn, POLICY_ID, now, None, WEEK)


@pytest.mark.db
def test_training_claim_holds_when_the_course_is_applied(school_db):
    conn = school_db
    now = clock_now(conn)
    report = forecast.week(conn, POLICY_ID, now, None, WEEK)
    best = report["fixes"]["training"]["best"]
    assert best and best["makes_safe"] >= 1 and best["of"] == report["counts"]["fragile_total"]
    assert best["text"].startswith(f"Train {best['count']} more in {best['label']} → {best['makes_safe']} of {best['of']}")
    assert best["cost_source"].startswith("assumption")  # no price list in the preset: said so
    option = tuple(best["key"].split(":"))
    staff = load_staff(conn, POLICY_ID)
    trained = [forecast.with_course(s, option, now.date()) if s.id in best["people"] else s for s in staff]
    for shift_id in best["shift_ids"]:
        before, after = recount(conn, shift_id, now), recount(conn, shift_id, now, trained)
        assert before <= report["threshold"] < after, shift_id
    standby = report["fixes"]["standby"]
    assert standby and all(s["text"].startswith(f"Put {s['name']} on standby for ") for s in standby)


@pytest.mark.db
def test_week_endpoint(school_db, monkeypatch):
    conn = school_db
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.setenv("DATABASE_URL", dsn)
    app = ext_app(dsn)
    with TestClient(app) as client:
        body = client.get("/api/week", params={"venue": "V1", "start": "2026-11-10"}).json()
        assert body["week"]["start"] == "2026-11-08" and body["venue"]["id"] == "V1"
        assert len(body["days"]) == 7 and body["days"][1]["label"] == "Mon 9"
        assert {"legend", "counts", "fragile", "fixes", "assumptions", "headline"} <= set(body)
        assert client.get("/api/week", params={"venue": "nope"}).status_code == 422
