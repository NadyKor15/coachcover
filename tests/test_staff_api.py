"""GET /api/staff: the Staff & Certificates screen reads what the database holds, with each card's state worked out
as the rules do it (revoked, expired, readers disagree, expiring in 30 days, valid)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from coachcover.model import CertType, StaffCert
from coachcover.views import cert_status
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def test_staff_lists_everyone_with_cards_and_states(api):
    client, conn = api
    body = client.get("/api/staff")
    assert body.status_code == 200
    data = body.json()
    assert len(data["staff"]) == conn.execute("SELECT count(*) FROM staff WHERE active").fetchone()[0] == 40
    assert data["as_of"] == "2026-11-09"
    people = {p["id"]: p for p in data["staff"]}

    def state(staff_id: str, cert: str) -> str:
        return next(c["state"] for c in people[staff_id]["certs"] if c["type"] == cert)

    assert state("S27", "wsi") == "expired"  # Anna Kim: expired on Nov 5
    assert state("S35", "lifeguarding") == "revoked"  # the simulated issuer registry revoked it, printed date is 2028
    assert state("S13", "wsi") == "review"  # the two readings disagree
    assert people["S13"]["needs_review"] is True and sum(p["needs_review"] for p in data["staff"]) == 1
    assert state("S08", "cpr_aed") == "expiring" and state("S20", "cpr_aed") == "expiring"
    assert state("S01", "wsi") == "valid"
    assert next(c for c in people["S13"]["certs"] if c["state"] == "review")["state_label"] == "Readers disagree — review"
    t = data["totals"]
    assert t["revoked"] == 2 and t["expired"] == 3 and t["review"] == 1 and t["expiring"] == 4
    assert sum(t.values()) == sum(len(p["certs"]) for p in data["staff"])  # every card is counted once
    assert any(g["renewal_overdue"] for g in people["S14"]["programs"])
    assert "invented" in data["note"]


def test_staff_states_match_the_rules_for_one_card():
    ctype = CertType("wsi", "Water Safety Instructor (WSI)", 24)
    today = date(2026, 11, 9)
    card = lambda **kw: StaffCert("wsi", date(2025, 1, 1), **kw)  # noqa: E731
    assert cert_status(card(expires_on=date(2026, 11, 8)), ctype, today)[0] == "expired"  # valid to the end of its day
    assert cert_status(card(expires_on=date(2026, 11, 9)), ctype, today) == ("expiring", 0)
    assert cert_status(card(expires_on=date(2026, 12, 9)), ctype, today) == ("expiring", 30)
    assert cert_status(card(expires_on=date(2026, 12, 10)), ctype, today) == ("valid", 31)
    assert cert_status(card(expires_on=date(2028, 1, 1), needs_review=True), ctype, today)[0] == "review"
    assert cert_status(card(expires_on=date(2028, 1, 1), revoked_on=date(2026, 7, 24)), ctype, today)[0] == "revoked"
    assert cert_status(card(expires_on=date(2028, 1, 1), revoked_on=date(2026, 12, 1)), ctype, today)[0] == "valid"


def test_staff_endpoint_reads_only(api):
    client, conn = api
    before = conn.execute("SELECT count(*), max(id) FROM staff_cert").fetchone()
    assert client.get("/api/staff").status_code == 200
    assert conn.execute("SELECT count(*), max(id) FROM staff_cert").fetchone() == before
    assert client.post("/api/staff").status_code == 405
