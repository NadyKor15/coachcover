"""HTTP API behaviour beyond the screen shapes (those are in test_contract.py)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.setenv("COACHCOVER_JUDGE_RUNS_PER_HOUR", "2")
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def test_health_shows_the_model_mode(api):
    client, _ = api
    assert client.get("/health").json() == {"ok": True, "db": "ok", "mode": "replay", "model_mode": "replay"}


def test_judge_runs_are_limited_per_hour(api):
    client, _ = api
    assert client.post("/api/judge/run", json={"scenario": "dangerous_morning"}).status_code == 200
    assert client.post("/api/judge/run", json={"scenario": "nightly_check"}).status_code == 200
    third = client.post("/api/judge/run", json={"scenario": "nobody_accepts"})
    assert third.status_code == 429 and int(third.headers["Retry-After"]) > 0
    assert client.post("/api/judge/run", json={"scenario": "sunny_day"}).status_code == 422


def test_callout_validation_over_http(api):
    client, _ = api
    assert client.post("/api/callouts", data={"staff_id": "S05"}).status_code == 422  # neither text nor audio
    assert client.post("/api/callouts", data={"staff_id": "NOBODY", "text": "hi"}).status_code == 404
    with open(FIXTURES / "audio" / "ru-callout.ogg", "rb") as f:
        made = client.post("/api/callouts", data={"staff_id": "S05"}, files={"audio": ("ru-callout.ogg", f, "audio/ogg")})
    assert made.status_code == 201 and made.json()["case_id"] == "C-1109-01"


def test_volunteer_and_plan_endpoints(api):
    client, conn = api
    client.post("/api/judge/run", json={"scenario": "nobody_accepts"})
    plan = client.get("/api/today").json()["plan_b"]
    assert client.post(f"/api/plans/{plan['id']}/approve", json={}).status_code == 422
    assert client.post("/api/plans/P-999/approve", json={"hash": "a" * 64}).status_code == 404
    blocked = client.post("/api/cases/C-1109-01/volunteer",
                          json={"staff_id": "S10", "text": "I can do it"}).json()  # WSI expired Nov 2
    assert blocked["eligible"] is False and blocked["reasons"][0]["code"] == "CERT_EXPIRED"


def test_closure_and_certificate_upload(api):
    client, _ = api
    made = client.post("/api/closures", json={"venue_id": "V1", "zone_id": "V1-TEACH", "reason": "weather",
                                              "starts_at": "2026-11-09T06:00:00-05:00", "ends_at": "2026-11-09T08:00:00-05:00"})
    assert made.status_code == 201 and made.json()["families"] == 6  # G001 at 06:00 (G003 is at 10:30 this story)
    assert client.post("/api/closures", json={"venue_id": "V1", "reason": "aliens", "starts_at": "2026-11-09T06:00:00-05:00",
                                              "ends_at": "2026-11-09T08:00:00-05:00"}).status_code == 422
    assert client.post("/api/closures", json={"venue_id": "V1", "reason": "weather", "starts_at": "2026-11-09T06:00:00",
                                              "ends_at": "2026-11-09T08:00:00"}).status_code == 422
    with open(FIXTURES / "cards" / "hard" / "h04.jpg", "rb") as f:  # Sofia Reyes's card: dates agree, card number not
        card = client.post("/api/staff/S25/certificates", data={"cert_type": "lifeguarding"},
                           files={"photo": ("card.jpg", f, "image/jpeg")}).json()
    assert card["review"] is False and card["notes"] == ["check_id"]
    assert client.post("/api/staff/S02/certificates", files={"photo": ("a.txt", b"hello", "text/plain")}).status_code == 415


def test_ledger_names_models_and_their_recordings(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    ledger = client.get("/api/models/ledger").json()
    assert ledger["total_calls"] >= 5 and all(c["ok"] for c in ledger["calls"])
    whisper = next(c for c in ledger["calls"] if c["purpose"] == "transcribe")
    # the 09:30 voice note is synthesized locally: its transcript is a prepared answer, marked synthetic
    assert whisper["model_label"] == "whisper-large-v3-turbo" and whisper["recording"] == "synthetic-transcribe-ru-0930.json"


def test_during_a_reset_the_api_answers_503_resetting(api):
    """A judge run holds RESET_LOCK while it rebuilds the school; meanwhile /api/ says so instead of a half-built 422/500."""
    import psycopg

    from coachcover.demo import RESET_LOCK

    client, conn = api
    holder = psycopg.connect(conn.info.dsn, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(%s)", (RESET_LOCK,))
        res = client.get("/api/today")
        assert res.status_code == 503 and res.json() == {"resetting": True, "error": "Resetting the training school…"}
        assert res.headers["retry-after"] == "2"
        assert client.get("/api/cases/C-1109-01").status_code == 503
        assert client.get("/health").status_code == 200  # not an /api/ route
        holder.execute("SELECT pg_advisory_unlock(%s)", (RESET_LOCK,))
        assert client.get("/api/today").status_code == 200
    finally:
        holder.close()
