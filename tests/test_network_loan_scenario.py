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
    assert loan["approvals"] == 2 and (loan["borrower"]["manager"], loan["lender"]["manager"]) == ("Natalie Brooks", "Omar Haddad")
    assert loan["lender"]["venue_name"].startswith("Northside")
    checked = next(s for s in loan["steps"] if s["kind"] == "checked_offer")
    assert (checked["title"], checked["detail"]) == ("Riverside agent re-checked Kai Morgan", "cleared by Riverside rules")
    assert client.get("/api/network").json()["networks"][0]["name"] == "Bay Swim Network"
    # every eligible person at Riverside was asked first; nobody cleared was left
    asked = conn.execute("SELECT count(*), count(*) FILTER (WHERE o.status = 'declined') FROM offer o"
                         " JOIN cover_case c ON c.id = o.case_id WHERE c.code = 'C-1109-01'").fetchone()
    assert asked[0] == asked[1] == 5
    assert conn.execute("SELECT staff_id FROM shift WHERE id = 'L-1109-G002'").fetchone() == ("N01",)
    # the founder's morning, not the old 06:00 deck shift: the 09:30 preschool lesson, an instructor, 08:45:20
    assert body["ended_at"].startswith("2026-11-09T08:45:20")
    assert (today["shift"]["time_label"], today["shift"]["needs_label"]) == ("09:30–10:15", "Instructor")
    labels = [t.get("label") for t in today["timeline"]]
    assert ["Asked the network", "Northside offers Kai Morgan", "Both managers agreed"] == [
        x for x in labels if x in ("Asked the network", "Northside offers Kai Morgan", "Both managers agreed")]
    assert today["network_loan"]["code"] == body["loan"]  # the Network screen opens it without ?loan=


def test_loan_steps_follow_each_other_in_time_on_both_agents(api):
    """Request < Northside's offer < Riverside's check < approvals < booking: the lender stamps after the message it
    answers, on the same demo clock, never before the borrower's request."""
    client, conn = api
    assert client.post("/api/judge/run", json={"scenario": "network_loan"}).status_code == 200
    steps = conn.execute("SELECT seq, at, venue_id, kind FROM ext_loan_step ORDER BY seq").fetchall()
    times = [at for _, at, _, _ in steps]
    assert times == sorted(times), steps  # never back in time
    first = {kind: at for _, at, _, kind in reversed(steps)}
    lender_first = min(at for _, at, venue, _ in steps if venue == "N1")
    assert first["request_sent"] < lender_first < first["checked_offer"] < first["approved"], steps
    assert first["approved"] < max(at for _, at, _, kind in steps if kind == "approved") <= times[-1]


class _Down:
    """An HTTP client for an agent that is not up yet."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        import httpx
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))

    post = get


def test_a_lender_agent_still_starting_gives_a_clear_503_and_costs_no_run(api, monkeypatch):
    client, conn = api
    monkeypatch.setattr(a2a, "HTTP_CLIENT", _Down)
    monkeypatch.setenv("COACHCOVER_AGENT_WAIT_SECONDS", "0.3")
    before = conn.execute("SELECT count(*) FROM ops.judge_run").fetchone()[0]
    res = client.post("/api/judge/run", json={"scenario": "network_loan"})
    assert res.status_code == 503 and res.headers["Retry-After"] == "5"
    assert res.json()["error"] == "Northside agent is starting, try again in a few seconds"
    assert conn.execute("SELECT count(*) FROM ops.judge_run").fetchone()[0] == before


def test_people_read_the_venues_time_and_real_arrows_never_utc(api):
    """Every text a person reads after the network scene: the loan's steps in the venue's time ("08:43:40 ET"), no
    "UTC" and no ASCII arrow "->"; machine fields (`at`) stay in UTC."""
    import re

    client, conn = api
    run = client.post("/api/judge/run", json={"scenario": "network_loan"}).json()
    loan = client.get(f"/api/network/loans/{run['loan']}").json()
    assert [s["at_label"] for s in loan["steps"]][:2] == ["08:43:40 ET", "08:43:40 ET"]
    assert all(s["at"].endswith("Z") for s in loan["steps"])
    assert loan["borrower"]["approved"]["at_label"] == "08:44:40 ET"
    machine = {"at", "opened_at", "closed_at", "start", "end", "starts_at", "ends_at", "generated_at", "created_at", "now"}
    seen = []

    def walk(where, value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(where, v, k)
        elif isinstance(value, list):
            for v in value:
                walk(where, v, key)
        elif isinstance(value, str) and key not in machine and (re.search(r"\bUTC\b", value) or "->" in value):
            seen.append((where, key, value))

    for url in ("/api/today", f"/api/cases/{run['case_id']}", f"/api/network/loans/{run['loan']}", "/api/week?venue=V1",
                "/api/binder.json?venue=V1&from=2026-11-09&to=2026-11-09", "/api/team-posts"):
        walk(url, client.get(url).json())
    assert seen == []
    assert re.search(r"\bUTC\b", "13:43:40 UTC") and "->" in "A -> B"  # the walk can see both


def test_the_inspectors_binder_prints_the_venues_time_not_utc(api):
    from coachcover import binder
    from coachcover.demo import clock_now
    from coachcover.seed import POLICY_ID

    client, conn = api
    client.post("/api/judge/run", json={"scenario": "network_loan"})
    data = binder.build(conn, POLICY_ID, "V1", None, None, clock_now(conn))
    text = binder.render(data, compress=False).decode("latin-1")
    assert "generated Nov 9, 08:45 ET" in text and "UTC" not in text and "->" not in text
    assert "UTC" in data["generated_at"] or data["generated_at"].endswith("+00:00")  # the machine field keeps UTC
