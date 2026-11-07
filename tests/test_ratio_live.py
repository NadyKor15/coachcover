"""Idea 1 of 2026-11-06: children added to a group -> the norm is checked again -> a case for one more instructor."""

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

GROUP = "G003"  # Riverside, Mon 07:00, Level 1 beginners aged 4-5: 4 children at the 1:4 norm, mostly Spanish-speaking
SHIFT = "L-1109-G003"


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def test_four_to_six_children_at_one_to_four_opens_a_case_and_the_queue_runs(api):
    client, conn = api
    assert client.get("/api/today").json()["ratio_alert"] is None
    res = client.post(f"/api/groups/{GROUP}/enroll", json={"add": 2, "language": "es"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["size_before"], body["size_after"]) == (4, 6)
    [alert] = body["alerts"]
    assert (alert["shift_id"], alert["participants"], alert["max_per_instructor"]) == (SHIFT, 6, 4)
    assert (alert["instructors_before"], alert["instructors_needed"]) == (1, 2)
    [case] = alert["cases"]
    assert case["extra_shift_id"] == f"{SHIFT}-I2"
    # the seat is a shift that teaches the same group at the same time; both now count two instructors
    rows = conn.execute("SELECT id, staff_id, status, instructors, extra_for FROM shift WHERE id IN (%s, %s) ORDER BY id",
                        (SHIFT, f"{SHIFT}-I2")).fetchall()
    assert rows == [(SHIFT, "S01", "scheduled", 2, None), (f"{SHIFT}-I2", None, "open", 2, SHIFT)]
    assert conn.execute("SELECT source FROM cover_case WHERE code = %s", (case["case_id"],)).fetchone() == ("ratio",)
    assert conn.execute("SELECT family_counts FROM lesson_group WHERE id = %s", (GROUP,)).fetchone()[0] == {"es": 5, "en": 1}

    page = client.get(f"/api/cases/{case['case_id']}")
    assert page.status_code == 200, page.text  # the case file of a ratio case opens like any other
    assert page.json()["steps"][0]["kind"] == "ratio"
    banner = client.get("/api/today").json()["ratio_alert"]
    assert banner["label"] == "Level 1, Mon 07:00 — now 6 kids, ratio 1:4 broken"
    assert (banner["status"], banner["tone"], banner["case_id"]) == ("open", "red", case["case_id"])

    # the same queue rules: an offer went out to the first eligible person, the line says why
    offer = conn.execute("SELECT o.id, o.staff_id, o.place->>'line', s.full_name FROM offer o JOIN staff s ON s.id = o.staff_id"
                         " WHERE o.status = 'pending' ORDER BY o.queue_pos").fetchone()
    assert offer is not None
    offer_id, staff_id, line, name = offer
    assert (staff_id, line) == ("S15", "Diego first: 5.25 h this week, Level 1 (beginner), 21.7 km away,"
                                " speaks Spanish like 5 of 6 families")
    assert client.post(f"/api/offers/O-{offer_id}/accept").status_code == 200
    closed = client.get("/api/today").json()["ratio_alert"]
    assert (closed["status"], closed["tone"]) == ("covered", "green")
    assert closed["action_label"].startswith(f"Covered by {name}")


def test_children_within_the_norm_open_nothing_and_bad_input_is_refused(api):
    client, conn = api
    res = client.post("/api/groups/G002/enroll", json={"add": 1})  # Level 3, 8 -> 9 aged 4-5, two instructors: 1:4 broken
    assert res.json()["alerts"][0]["instructors_needed"] == 3
    ok = client.post("/api/groups/G001/enroll", json={"add": 1})  # Level 4 aged 8-10: 6 -> 7 at 1:8
    assert ok.status_code == 200 and ok.json()["alerts"] == []
    assert client.post("/api/groups/G999/enroll", json={"add": 1}).status_code == 404
    assert client.post("/api/groups/G001/enroll", json={"add": 0}).status_code == 422
    assert client.post("/api/groups/G001/enroll", json={"add": 1, "language": "Spanish"}).status_code == 422
