"""HTTP API on a fresh training school (demo clock at Monday 05:12), replay models only."""

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


def test_health(api):
    client, _ = api
    assert client.get("/health").json() == {"ok": True, "db": "ok", "model_mode": "replay"}


def test_judge_run_then_case_page_today_stats_and_ledger(api):
    client, _ = api
    run = client.post("/api/judge/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["plan_b"]["option"] == "lead" and body["runs_left_this_hour"] == 1
    page = client.get(body["case_urls"]["L-1109-G001"]).json()
    assert page["status"] == "covered" and page["covered_by"] == "S17" and page["covered_by_name"] == "Maria Sokolova"
    assert page["absent_name"] == "Marina Petrova" and page["message_channel"] == "voice"
    assert page["filter_total"] == 40 and page["filter_eligible"] + page["filter_out"] + 3 == 40  # 3 head coaches in reserve
    assert {c["code"] for c in page["filter_chips"]} >= {"IS_THE_ABSENT_PERSON", "CERT_EXPIRED", "OVERLAPS_COMMITMENT"}
    assert len(page["filter_grid"]) == 37 and page["filter_grid"][0]["eligible"] is True
    assert [o["status"] for o in page["offers"]] == ["declined", "accepted"]
    assert page["timeline"][0]["line"].startswith("05:12 Marina Petrova (voice)")
    assert any(t["line"] == "05:18 Maria Sokolova: yes, in the schedule (6 min after the call-out)" for t in page["timeline"])
    assert [c["purpose"] for c in page["model_calls"]] == ["transcribe", "extract_callout"]
    assert page["model_cost_usd"] > 0
    kids = client.get(body["case_urls"]["L-1109-G004"]).json()
    assert kids["status"] == "plan_b" and kids["plans"][0]["status"] == "approved"
    assert kids["plans"][0]["options"][0]["line"].startswith("Liam Foster")
    today = client.get("/api/today").json()
    assert today["focus_case_id"] == page["case_id"] and today["filter_total"] == 40
    assert today["feed"] and today["feed"][0]["time"] == "05:12"
    assert today["tomorrow_date"] == "2026-11-10"
    assert [(e["staff_id"], e["days_left"]) for e in today["expiring_30"]] == [("S20", 17), ("S33", 24)]
    assert ("S02", 48) in [(e["staff_id"], e["days_left"]) for e in today["expiring_60"]]
    assert all(30 < e["days_left"] <= 60 for e in today["expiring_60"])
    assert sum(c["count"] for c in today["filter_chips"]) == today["filter_out"]
    stats = client.get("/api/stats").json()
    assert stats["covered"] == 1 and stats["plan_b_by_option"] == {"lead": 1} and stats["lessons_saved"] == 2
    ledger = client.get("/api/models/ledger").json()
    assert ledger["total_calls"] >= 3 and all(c["ok"] for c in ledger["calls"])


def test_judge_runs_are_limited_per_hour(api):
    client, _ = api
    assert client.post("/api/judge/run").status_code == 200
    assert client.post("/api/judge/run").status_code == 200
    third = client.post("/api/judge/run")
    assert third.status_code == 429 and int(third.headers["Retry-After"]) > 0


def test_callout_by_voice_then_answers_over_http(api):
    client, conn = api
    with open(FIXTURES / "audio" / "ru-callout.ogg", "rb") as f:
        made = client.post("/api/callouts", data={"staff_id": "S05"}, files={"audio": ("ru-callout.ogg", f, "audio/ogg")})
    assert made.status_code == 201, made.text
    case_id = made.json()["cases"][0]["case_id"]
    offer_id = conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()[0]
    assert client.post(f"/api/offers/{offer_id}/decline").json()["status"] == "declined"
    again = client.post(f"/api/offers/{offer_id}/decline")
    assert again.status_code == 409 and "declined" in again.json()["error"]
    assert client.post("/api/offers/999999/accept").status_code == 404
    assert client.post(f"/api/offers/{offer_id}/maybe").status_code == 404


def test_callout_validation_over_http(api):
    client, _ = api
    assert client.post("/api/callouts", data={"staff_id": "S05"}).status_code == 422  # neither text nor audio
    assert client.post("/api/callouts", data={"staff_id": "NOBODY", "text": "hi"}).status_code == 404


def test_plan_approval_needs_the_shown_hash(api):
    client, conn = api
    client.post("/api/callouts", data={"staff_id": "S05", "text": "Привет, это Марина. У меня температура 38, я не выйду в 6. У меня в 6 дорожка и в 7 урок у малышей. Прости."})
    case_id = conn.execute("SELECT id FROM cover_case WHERE shift_id = 'L-1109-G001'").fetchone()[0]
    while row := conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone():
        client.post(f"/api/offers/{row[0]}/decline")
    plan_id, digest = conn.execute("SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case_id,)).fetchone()
    assert client.post(f"/api/plans/{plan_id}/approve", json={}).status_code == 422
    stale = client.post(f"/api/plans/{plan_id}/approve", json={"hash": "f" * 64})
    assert stale.status_code == 409 and stale.json()["detail"]["hash"] == digest
    ok = client.post(f"/api/plans/{plan_id}/approve", json={"hash": digest, "by": "manager:the founder"})
    assert ok.status_code == 200 and ok.json()["option"] == "merge" and ok.json()["families"] == 8


def test_closure_and_certificate_upload(api):
    client, _ = api
    made = client.post("/api/closures", json={"venue_id": "V1", "zone_id": "V1-TEACH", "reason": "weather",
                                              "starts_at": "2026-11-09T06:00:00-05:00", "ends_at": "2026-11-09T08:00:00-05:00"})
    assert made.status_code == 201 and made.json()["families"] == 7
    assert client.post("/api/closures", json={"venue_id": "V1", "reason": "aliens", "starts_at": "2026-11-09T06:00:00-05:00",
                                              "ends_at": "2026-11-09T08:00:00-05:00"}).status_code == 422
    assert client.post("/api/closures", json={"venue_id": "V1", "reason": "weather", "starts_at": "2026-11-09T06:00:00",
                                              "ends_at": "2026-11-09T08:00:00"}).status_code == 422
    with open(FIXTURES / "cards" / "c5-photo.jpg", "rb") as f:
        card = client.post("/api/staff/S02/certificates", files={"photo": ("card.jpg", f, "image/jpeg")})
    assert card.status_code == 201 and card.json()["review"] is True and "readers_disagree" in card.json()["flags"]
    assert client.post("/api/staff/S02/certificates", files={"photo": ("a.txt", b"hello", "text/plain")}).status_code == 415
