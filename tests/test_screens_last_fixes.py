"""Last screen fixes before the coupon: standby proposals, the case reasons' tone, the case cost label, wording."""

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from coachcover.money import cost_label
from coachcover.screens import case_tone
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(dsn)) as client:
        yield client, conn


def test_propose_standby_only_proposes_and_refuses_a_person_who_is_not_cleared(api):
    client, conn = api
    week = client.get("/api/week", params={"venue": "V1"}).json()
    fix = week["fixes"]["standby"][0]
    holder = conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (fix["shift_id"],)).fetchone()
    made = client.post("/api/standby", json={"shift_id": fix["shift_id"], "staff_id": fix["staff_id"]})
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["id"].startswith("SB-") and body["status"] == "proposed" and body["mode"] == "propose"
    assert body["text"].startswith(f"Standby proposed: {body['name']} for ") and "Nothing is booked" in body["text"]
    assert conn.execute("SELECT staff_id, status FROM shift WHERE id = %s", (fix["shift_id"],)).fetchone() == holder
    again = client.post("/api/standby", json={"shift_id": fix["shift_id"], "staff_id": fix["staff_id"]})
    assert again.status_code == 409 and "already proposed" in again.json()["reason"]
    # Anna is not cleared for this lesson (the rules say why); the shift's holder cannot be their own standby
    refused = client.post("/api/standby", json={"shift_id": fix["shift_id"], "staff_id": "S27"})
    assert refused.status_code == 409 and refused.json()["reason"].startswith("Anna Kim is not cleared for ")
    assert refused.json()["codes"]
    own = client.post("/api/standby", json={"shift_id": fix["shift_id"], "staff_id": holder[0]})
    assert own.status_code == 409 and "IS_THE_ABSENT_PERSON" in own.json()["codes"]
    assert client.post("/api/standby", json={"shift_id": "nope", "staff_id": "S17"}).status_code == 404


def test_case_reasons_say_risk_or_fragile_and_money_has_a_case_cost_label(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    page = client.get("/api/cases/C-1109-01").json()
    reasons = next(s for s in page["steps"] if s["kind"] == "allowed")["reasons"]
    tones = {r["code"]: r["tone"] for r in reasons}
    assert set(tones.values()) <= {"risk", "fragile"}
    assert tones["CERT_EXPIRED"] == tones["CERT_REVOKED"] == tones["HOURS_LIMIT_EXCEEDED"] == tones["PROGRAM_MISSING"] == "risk"
    assert tones["CERT_NEEDS_REVIEW"] == tones["REST_TOO_SHORT"] == "fragile"
    assert case_tone("PROGRAM_RENEWAL_OVERDUE") == "fragile"  # not first for anyone on the 08:00 morning
    money = client.get("/api/today").json()["money"]
    assert money["case_cost_label"] == cost_label(money["case_cost_cents"] / 100) and money["case_cost_label"].endswith("¢")


def test_week_hint_names_the_price_once():
    from coachcover import forecast
    import inspect

    assert "(assumed course price)" not in inspect.getsource(forecast)
