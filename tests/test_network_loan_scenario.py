"""Judge scenario "network_loan": nobody cleared left at Riverside, Northside lends a person over A2A, two managers
approve; the morning screen and the case file show who covered it (a person not on Riverside's staff list)."""

import contextlib

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover import a2a
from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.delenv("COACHCOVER_A2A_PEERS", raising=False)
    monkeypatch.setenv("COACHCOVER_SELF_URL", "http://testserver")
    with TestClient(create_app(dsn)) as client:
        monkeypatch.setattr(a2a, "HTTP_CLIENT", lambda: contextlib.nullcontext(client))  # agents talk HTTP in-process
        yield client, conn


def test_network_loan_covers_the_hero_shift_with_both_approvals(api):
    client, conn = api
    run = client.post("/api/judge/run", json={"scenario": "network_loan"})
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["case_id"] == "C-1109-01" and body["loan_status"] == "assigned"
    today = client.get("/api/today").json()
    assert today["outcome"]["covered_by"] == "Kai Morgan" and today["outcome"]["covered_via"] == "network_loan"
    page = client.get("/api/cases/C-1109-01").json()
    assert (page["status"], page["covered_by"], page["covered_via"]) == ("covered", "Kai Morgan", "network_loan")
    loan = client.get(f"/api/network/loans/{body['loan']}").json()
    assert loan["approvals"] == 2 and (loan["borrower"]["manager"], loan["lender"]["manager"]) == ("Dana Ruiz", "Omar Haddad")
    assert loan["lender"]["venue_name"].startswith("Northside")
    # every eligible person at Riverside was asked first; nobody cleared was left
    asked = conn.execute("SELECT count(*), count(*) FILTER (WHERE o.status = 'declined') FROM offer o"
                         " JOIN cover_case c ON c.id = o.case_id WHERE c.code = 'C-1109-01'").fetchone()
    assert asked[0] == asked[1] == 5
    assert conn.execute("SELECT staff_id FROM shift WHERE id = 'L-1109-G002'").fetchone() == ("N01",)
