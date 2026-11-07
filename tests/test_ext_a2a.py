"""Staff loans between two venues of one network over A2A 1.0, on a fresh training school per test.

The borrower is the training school's venue V1; the lender is the network's Northside venue (N1) with its own
rules (a 44 h week) and its own manager. Replay models only; agents talk over HTTP through the test client.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover import a2a, ext_api
from coachcover.api import create_app
from coachcover.demo import reset_school, set_clock
from coachcover.models import DbLedger, ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import NOW, POLICY_ID
from tests.conftest import TEST_DSN
from tests.test_pipeline import RU_VOICE

pytestmark = pytest.mark.db

BORROWER_MANAGER = "Natalie Brooks"
LENDER_MANAGER = "Omar Haddad"


def ext_app(dsn: str):
    """The core app with the lane's router: added here unless the core app already includes it, and always ahead
    of a catch-all static mount (a mount at "/" would answer every later route)."""
    from starlette.routing import Mount

    app = create_app(dsn)
    routes = app.router.routes
    if not any(getattr(r, "original_router", None) is ext_api.router or getattr(r, "path", None) == "/api/network"
               for r in routes):
        app.include_router(ext_api.router)
    for mount in [r for r in routes if isinstance(r, Mount) and r.path in ("", "/")]:
        routes.remove(mount)
        routes.append(mount)
    return app


@pytest.fixture
def net(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.delenv("COACHCOVER_A2A_PEERS", raising=False)
    app = ext_app(dsn)
    with TestClient(app) as client:
        monkeypatch.setattr(a2a, "HTTP_CLIENT", lambda: contextlib.nullcontext(client))
        yield client, conn


def exhaust(conn) -> tuple[str, int]:
    """Marina's call-out; everyone cleared for her first shift says no. Returns (shift, case) with nobody left."""
    shift_id = conn.execute(
        "SELECT id FROM shift WHERE staff_id = 'S05' AND venue_id = 'V1' ORDER BY starts_at, id LIMIT 1"
    ).fetchone()[0]
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
    p.callout("S05", now=NOW, audio=RU_VOICE, filename="ru-callout.ogg")
    case_id = conn.execute("SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)).fetchone()[0]
    t = NOW
    while True:
        row = conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
        if row is None:
            break
        t += timedelta(seconds=30)
        p.respond(row[0], False, t)
    set_clock(conn, t + timedelta(minutes=1))
    assert conn.execute("SELECT status FROM shift WHERE id = %s", (shift_id,)).fetchone()[0] == "open"
    return shift_id, case_id


def kinds(loan: dict) -> list[str]:
    return [s["kind"] for s in loan["steps"]]


def snapshot(conn, shift_id: str, case_id: int) -> tuple:
    return (
        conn.execute("SELECT status, staff_id FROM shift WHERE id = %s", (shift_id,)).fetchone(),
        conn.execute("SELECT status FROM cover_case WHERE id = %s", (case_id,)).fetchone(),
        conn.execute("SELECT count(*) FROM case_event WHERE case_id = %s AND kind = 'shift_covered'", (case_id,)).fetchone(),
    )


def start(client, shift_id: str) -> dict:
    resp = client.post("/api/network/loans", json={"shift_id": shift_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_loan_needs_both_managers_then_books_and_records_on_both_sides(net):
    client, conn = net
    shift_id, case_id = exhaust(conn)
    loan = start(client, shift_id)
    assert loan["status"] == "awaiting_approval", loan
    assert loan["candidate"]["name"] == "Kai Morgan" and loan["lender"]["venue_name"] == "Northside Aquatic Center"
    assert {"asked_network", "request_sent", "checked_own_people", "offered", "checked_offer", "borrower_accepted",
            "awaiting_approval"} <= set(kinds(loan))
    lender_steps = [s for s in loan["steps"] if s["side"] == "lender"]
    assert lender_steps and all(s["venue_id"] == "N1" for s in lender_steps)
    checked = next(s for s in loan["steps"] if s["kind"] == "checked_own_people")
    assert checked["data"]["rules"]["max_weekly_hours"] == 44 and checked["data"]["guards"]["ok"] is True
    assert next(s for s in loan["steps"] if s["kind"] == "checked_offer")["data"]["ok"] is True
    assert all(s["at"].endswith("Z") and s["at_label"].endswith("UTC") for s in loan["steps"])
    assert loan["a2a"]["task_state"] == a2a.STATE_INPUT_REQUIRED
    before = snapshot(conn, shift_id, case_id)

    one = client.post(f"/api/network/loans/{loan['code']}/approve", json={"manager": BORROWER_MANAGER, "venue": "V1"})
    assert one.status_code == 200, one.text
    assert one.json()["status"] == "awaiting_approval" and one.json()["approvals"] == 1
    assert snapshot(conn, shift_id, case_id) == before  # one approval moves nothing

    two = client.post(f"/api/network/loans/{loan['code']}/approve", json={"manager": LENDER_MANAGER, "venue": "N1"})
    assert two.status_code == 200, two.text
    done = two.json()
    assert done["status"] == "assigned" and done["approvals"] == 2, done
    assert done["status_label"] == "Loan approved by both managers"
    assert conn.execute("SELECT status, staff_id FROM shift WHERE id = %s", (shift_id,)).fetchone() == ("scheduled", "N01")
    assert conn.execute("SELECT status FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0] == "covered"
    covered = conn.execute(
        "SELECT data FROM case_event WHERE case_id = %s AND kind = 'shift_covered'", (case_id,)
    ).fetchone()[0]
    assert covered["via"] == "network_loan" and covered["staff_id"] == "N01" and covered["lender_venue"] == "N1"
    assert covered["approved_by"] == {"borrower": BORROWER_MANAGER, "lender": LENDER_MANAGER}
    # records on both sides: the borrower's case file, the lender's own journal and A2A task
    assert "lent" in [s["kind"] for s in done["steps"] if s["venue_id"] == "N1"]
    assert "assigned" in [s["kind"] for s in done["steps"] if s["venue_id"] == "V1"]
    assert done["a2a"]["task_state"] == a2a.STATE_COMPLETED
    assert conn.execute("SELECT count(*) FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case_id,)).fetchone()[0] == 0
    posts = client.get("/api/team-posts").json()["posts"]
    lender_post = next(p for p in posts if p["venue_id"] == "N1")
    assert lender_post["channel"] == "#northside-staff" and lender_post["body"].startswith("Kai Morgan lent to ")
    assert lender_post["body"].endswith(f"approved by {BORROWER_MANAGER} and {LENDER_MANAGER}")
    borrower_post = next(p for p in posts if p["case_id"] == case_id)
    assert "covered by Kai Morgan from Northside Aquatic Center" in borrower_post["body"]
    assert all(p["delivery"] == "simulated" for p in posts)


def test_lender_refuses_when_it_would_leave_its_own_site_short(net):
    client, conn = net
    shift_id, case_id = exhaust(conn)
    a2a.ensure_network(conn)
    conn.execute("UPDATE ext_network_member SET min_guards = 4 WHERE venue_id = 'N1'")
    before = snapshot(conn, shift_id, case_id)
    loan = start(client, shift_id)
    assert loan["status"] == "declined", loan
    declined = next(s for s in loan["steps"] if s["kind"] == "declined")
    assert declined["side"] == "lender" and declined["data"]["reason"] == "WOULD_LEAVE_SITE_SHORT"
    assert declined["data"]["guards"]["before"] == 4 and declined["data"]["guards"]["after"] == 3
    assert "minimum is 4" in declined["data"]["why"]
    assert loan["a2a"]["task_state"] == a2a.STATE_REJECTED
    assert snapshot(conn, shift_id, case_id) == before


def test_borrower_rejects_the_offered_person_by_its_own_rules(net):
    client, conn = net
    shift_id, case_id = exhaust(conn)
    a2a.ensure_network(conn)
    role = conn.execute("SELECT role_id FROM shift WHERE id = 'N-1110-1000'").fetchone()[0]
    # Kai: 8 h booked + 32 h = 40 h this week. Northside allows 44 h, the borrower's rules 40 h.
    # The extra blocks keep well clear of Monday morning (rest rules, if any, stay out of it).
    # Riverside's week is Mon 9 - Sun 15; Northside's (the preset's) Sun 8 - Sat 14: the Sunday block counts only here
    for i, (day, begin, finish) in enumerate((("2026-11-15", "06:00", "14:00"), ("2026-11-11", "13:00", "21:00"),
                                           ("2026-11-13", "06:00", "14:00"), ("2026-11-14", "06:00", "14:00"))):
        conn.execute(
            "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id)"
            " VALUES (%s, 'N1', 'N1-MAIN', %s, %s::timestamp AT TIME ZONE 'America/New_York',"
            " %s::timestamp AT TIME ZONE 'America/New_York', 'N01')",
            (f"N-EXTRA-{i}", role, f"{day} {begin}", f"{day} {finish}"),
        )
    before = snapshot(conn, shift_id, case_id)
    loan = start(client, shift_id)
    assert loan["status"] == "rejected", loan
    checked = next(s for s in loan["steps"] if s["kind"] == "checked_offer")
    assert checked["side"] == "borrower" and checked["data"]["ok"] is False
    assert checked["data"]["codes"] == ["HOURS_LIMIT_EXCEEDED"] and checked["data"]["max_weekly_hours"] == 40
    offered = next(s for s in loan["steps"] if s["kind"] == "checked_own_people")
    assert offered["data"]["rules"]["max_weekly_hours"] == 44
    assert "offer_withdrawn" in kinds(loan)
    # the second request excludes Kai, and Northside has nobody else cleared
    assert next(s for s in loan["steps"] if s["kind"] == "lender_declined")["data"]["reason"] == "NO_CLEARED_PERSON"
    assert snapshot(conn, shift_id, case_id) == before


def test_nobody_approves_for_a_manager(net):
    client, conn = net
    shift_id, case_id = exhaust(conn)
    loan = start(client, shift_id)
    code = loan["code"]
    wrong = client.post(f"/api/network/loans/{code}/approve", json={"manager": LENDER_MANAGER, "venue": "V1"})
    assert wrong.status_code == 403 and "Natalie Brooks" in wrong.json()["error"]
    wrong = client.post(f"/api/network/loans/{code}/approve", json={"manager": BORROWER_MANAGER, "venue": "N1"})
    assert wrong.status_code == 403
    outsider = client.post(f"/api/network/loans/{code}/approve", json={"manager": BORROWER_MANAGER, "venue": "V2"})
    assert outsider.status_code == 403
    # the borrower's agent tries to approve for Northside's manager, agent to agent
    rpc = client.post("/a2a/N1", headers={"A2A-Version": "1.0"}, json={
        "jsonrpc": "2.0", "id": 7, "method": "SendMessage",
        "params": {"message": a2a.message("ROLE_USER", [a2a.data_part({"type": "loan.approve", "loan": code,
                                                                      "manager": LENDER_MANAGER})],
                                          loan["a2a"]["context_id"], loan["a2a"]["task_id"])}})
    err = rpc.json()["error"]
    assert err["code"] == a2a.UNSUPPORTED_OPERATION and err["data"][0]["reason"] == "MANAGER_APPROVAL_REQUIRED"
    view = client.get(f"/api/network/loans/{code}").json()
    assert view["approvals"] == 0 and view["status"] == "awaiting_approval"
    ok = client.post(f"/api/network/loans/{code}/approve", json={"manager": " natalie  brooks ", "venue": "V1"})
    assert ok.status_code == 200 and ok.json()["approvals"] == 1
    again = client.post(f"/api/network/loans/{code}/approve", json={"manager": BORROWER_MANAGER, "venue": "V1"})
    assert again.status_code == 409
    assert conn.execute("SELECT status FROM shift WHERE id = %s", (shift_id,)).fetchone()[0] == "open"


def test_no_loan_while_own_people_are_left(net):
    client, conn = net
    shift_id = conn.execute(
        "SELECT id FROM shift WHERE staff_id = 'S05' AND venue_id = 'V1' ORDER BY starts_at, id LIMIT 1"
    ).fetchone()[0]
    p = Pipeline(conn, ModelClient(DbLedger(conn), mode="replay"), POLICY_ID)
    p.callout("S05", now=NOW, audio=RU_VOICE, filename="ru-callout.ogg")
    resp = client.post("/api/network/loans", json={"shift_id": shift_id})
    assert resp.status_code == 409 and "have not been asked yet" in resp.json()["error"]
    scheduled = conn.execute("SELECT id FROM shift WHERE status = 'scheduled' AND venue_id = 'V1' LIMIT 1").fetchone()[0]
    assert client.post("/api/network/loans", json={"shift_id": scheduled}).status_code == 409


def test_agent_card_and_json_rpc_errors(net):
    client, _ = net
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200 and card.headers["etag"]
    body = card.json()
    for key in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes",
                "defaultOutputModes", "skills"):
        assert body[key], key
    assert {i["protocolVersion"] for i in body["supportedInterfaces"]} == {"1.0"}
    assert {i["protocolBinding"] for i in body["supportedInterfaces"]} == {"JSONRPC"}
    assert {i["url"].rsplit("/", 1)[1] for i in body["supportedInterfaces"]} == {"V1", "N1"}
    assert {s["id"] for s in body["skills"]} == {"staff-loan.V1", "staff-loan.N1"}
    assert all({"id", "name", "description", "tags"} <= set(s) for s in body["skills"])

    def rpc(payload, headers={"A2A-Version": "1.0"}):
        return client.post("/a2a/N1", headers=headers, content=payload if isinstance(payload, str) else None,
                           json=None if isinstance(payload, str) else payload).json()

    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}, headers={})["error"]["code"] == -32009
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})["error"]["code"] == -32601
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "nope"}})["error"]["code"] == -32001
    assert rpc("{not json")["error"]["code"] == -32700
    assert rpc({"id": 1, "method": "SendMessage"})["error"]["code"] == -32600
    no_data = {"jsonrpc": "2.0", "id": 2, "method": "SendMessage",
               "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}}}
    assert rpc(no_data)["error"]["code"] == -32602
    net_ = client.get("/api/network").json()
    assert {m["venue_id"] for m in net_["members"]} == {"V1", "N1"} and net_["protocol_version"] == "1.0"
