"""The inspection binder: every shift re-checked by the rules, cases and incidents with hashes, a stable digest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover import binder, ext_api
from coachcover.api import create_app
from coachcover.demo import clock_now, reset_school
from coachcover.model import Shift
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import eligible
from coachcover.seed import POLICY_ID
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db
MONDAY = date(2026, 11, 9)


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    app = create_app(dsn)
    app.include_router(ext_api.router)
    with TestClient(app) as client:
        yield client, conn


def independent_violations(conn, shift_ids: list[str]) -> int:
    policy = load_policy(conn, POLICY_ID)
    staff = {s.id: s for s in load_staff(conn, POLICY_ID)}
    count = 0
    for sid in shift_ids:
        holder = conn.execute("SELECT staff_id FROM shift WHERE id = %s", (sid,)).fetchone()[0]
        shift = load_shift(conn, sid)
        decision = eligible(staff[holder], replace(shift, assignee=None), shift.start, policy, load_context(conn, POLICY_ID, shift))
        count += 0 if decision.ok else 1
    return count


def test_every_shift_is_checked_and_a_planted_violation_shows(api):
    client, conn = api
    client.post("/api/judge/run")
    data = client.get("/api/binder.json", params={"venue": "V1", "from": "2026-11-09", "to": "2026-11-09"}).json()
    ids = [r for (r,) in conn.execute(
        "SELECT id FROM shift WHERE venue_id = 'V1' AND status = 'scheduled'"
        " AND (starts_at AT TIME ZONE 'America/New_York')::date = '2026-11-09' ORDER BY starts_at, id")]
    assert [r["shift_id"] for r in data["shifts"]] == ids
    assert data["summary"]["violations"] == independent_violations(conn, ids)
    assert data["summary"]["cases"] == 2 and all(len(c["sha256"]) == 64 for c in data["cases"])
    # must-differ: S27's main card is expired in the training school; a late post for S27 must show as a violation
    role = conn.execute("SELECT role_id FROM shift WHERE id = 'G-1109-V1-0530'").fetchone()[0]
    conn.execute(
        "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id) VALUES ('X-PLANTED', 'V1',"
        " (SELECT zone_id FROM shift WHERE id = 'G-1109-V1-0530'), %s,"
        " '2026-11-09 22:00'::timestamp AT TIME ZONE 'America/New_York',"
        " '2026-11-09 23:00'::timestamp AT TIME ZONE 'America/New_York', 'S27')", (role,))
    again = client.get("/api/binder.json", params={"venue": "V1", "from": "2026-11-09", "to": "2026-11-09"}).json()
    assert again["summary"]["violations"] == data["summary"]["violations"] + 1
    planted = next(r for r in again["shifts"] if r["shift_id"] == "X-PLANTED")
    assert planted["ok"] is False and planted["result"].startswith("VIOLATION:") and "expired" in planted["result"]
    assert any("none valid" in card for card in planted["cards"])
    assert again["sha256"] != data["sha256"]


def test_case_hash_is_the_hash_of_its_event_file(api):
    client, conn = api
    client.post("/api/judge/run")
    data = binder.build(conn, POLICY_ID, "V1", MONDAY, MONDAY, clock_now(conn))
    assert data["cases"]
    for case in data["cases"]:
        events = conn.execute(
            "SELECT e.seq, e.at, e.actor, e.kind, e.data FROM case_event e JOIN cover_case c ON c.id = e.case_id"
            " WHERE c.shift_id = %s ORDER BY e.seq", (case["shift_id"],)).fetchall()
        chain = [[seq, at.astimezone(UTC).isoformat(), actor, kind, d] for seq, at, actor, kind, d in events]
        text = json.dumps(chain, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
        assert case["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert case["events"] == len(events)


def test_pdf_and_digest(api):
    client, conn = api
    params = {"venue": "V1", "from": "2026-11-09", "to": "2026-11-10"}
    pdf = client.get("/api/binder.pdf", params=params)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-") and pdf.content.rstrip().endswith(b"%%EOF")
    data = client.get("/api/binder.json", params=params).json()
    assert pdf.headers["x-binder-sha256"] == data["sha256"]  # same records, same digest, whenever printed
    assert data["summary"]["headline"] == (f"{data['summary']['shifts']} shifts · {data['summary']['violations']} "
                                           f"violation{'' if data['summary']['violations'] == 1 else 's'}")
    assert binder.render(data)[:5] == b"%PDF-"
    assert client.get("/api/binder.pdf", params={"from": "2026-11-10", "to": "2026-11-09"}).status_code == 422
    assert client.get("/api/binder.pdf", params={"from": "2026-01-01", "to": "2026-12-31"}).status_code == 422
    assert client.get("/api/binder.pdf", params={"venue": "nope"}).status_code == 422


def test_latin1_text_survives():
    assert binder._t("Sofía García – 06:00→07:00 “ok”") == "Sofía García - 06:00->07:00 \"ok\""
    assert binder._t("Марина") == "??????"
