"""WEB lane review of the live API (2026-11-07): a called-out person is offered nothing that day; the role reason
reads right; no internal chat numbers in answers; model costs never print as $0.000."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from coachcover.money import cost_label
from coachcover.presets import load_preset
from coachcover.screens import reason_label
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db
PRESETS = Path(__file__).resolve().parents[1] / "coachcover" / "presets"


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(dsn)) as client:
        yield client, conn


def test_a_called_out_person_gets_no_offer_or_clearance_that_day(api):
    client, conn = api
    assert client.post("/api/judge/run", json={"scenario": "dangerous_morning"}).status_code == 200
    # the 07:00 beginners grow over their norm: a second-instructor seat opens at 07:00, after Marina's call-out
    alert = client.post("/api/groups/G003/enroll", json={"add": 2, "language": "es"}).json()["alerts"][0]
    seat = alert["cases"][0]["extra_shift_id"]
    case = client.get(f"/api/cases/{alert['cases'][0]['case_id']}").json()
    marina = next(s for s in next(x for x in case["steps"] if x["kind"] == "allowed")["staff"] if s["name"] == "Marina Petrova")
    assert marina["allowed"] is False and marina["reason_label"] == "Called out sick today"
    asked = conn.execute("SELECT staff_id FROM offer o JOIN cover_case c ON c.id = o.case_id WHERE c.shift_id = %s", (seat,))
    assert "S05" not in {sid for (sid,) in asked}
    banner = client.get("/api/today").json()["ratio_alert"]
    assert "Marina" not in json.dumps(banner)
    # every shift of hers that day: never offered, never allowed
    rows = conn.execute("SELECT DISTINCT o.shift_id FROM offer o WHERE o.staff_id = 'S05'").fetchall()
    assert rows == []


def test_the_role_reason_reads_like_english():
    preset = load_preset("swim_school")
    assert reason_label("ROLE_MISSING", {"role": "instructor"}, preset, None, None) == "Role: not an instructor"
    assert reason_label("ROLE_MISSING", {"role": "lifeguard"}, preset, None, None) == "Role: not a lifeguard"


def test_no_internal_chat_numbers_in_answers_or_data(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    today = client.get("/api/today")
    page = client.get("/api/cases/C-1109-01")
    pattern = re.compile(r"\bTG ?\d{3,}")
    for body in (today.text, page.text, client.get("/api/stats").text):
        assert not pattern.search(body)
    for path in PRESETS.glob("**/*.json"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name
    assert today.json()["money"]["source"] == "from the founder's coaching experience"


def test_model_costs_never_print_as_zero(api):
    assert cost_label(0.0004) == "0.040¢" and cost_label(0.0123) == "$0.01" and cost_label(1.5) == "$1.50"
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    log = client.get("/api/today").json()["model_log"]
    assert log["calls"] > 0 and "$0.000" not in log["label"] and "¢" in log["label"]
    page = client.get("/api/cases/C-1109-01").json()
    assert page["model_cost_label"] == cost_label(page["model_cost_usd"]) and page["model_cost_label"] != "$0.00"
