"""MCP smoke: the official MCP client talks to /mcp on a real HTTP server and calls every tool.

Read tools answer with reasons; request_cover leaves a proposal and nothing else; approve_plan_b is refused by
the autonomy policy (N1). Public mode (no key) has an hourly limit per address; a wrong key is refused.
"""

from __future__ import annotations

import json

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg.conninfo import make_conninfo

from coachcover.demo import reset_school
from tests.conftest import TEST_DSN
from tests.test_ext_a2a import exhaust
from tests.test_ext_a2a_http import Server, free_port

pytestmark = pytest.mark.db

TOOLS = {"who_is_allowed", "shifts_on_day", "fragile_shifts", "get_case", "request_cover", "approve_plan_b"}


@pytest.fixture
def server(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn, "deck")  # exhaust() empties the queue of Marina's 06:00 deck shift (the deck story)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.delenv("COACHCOVER_MCP_KEY", raising=False)
    monkeypatch.setenv("COACHCOVER_MCP_CALLS_PER_HOUR", "1000")
    with Server(dsn, free_port()) as srv:
        yield srv, conn, monkeypatch


def call_all(url: str, calls: list[tuple[str, dict]], headers: dict | None = None) -> tuple[set[str], list]:
    async def main():
        async with httpx.AsyncClient(headers=headers or {}, timeout=30) as http:
            async with streamable_http_client(url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {t.name for t in (await session.list_tools()).tools}
                    results = [await session.call_tool(name, args) for name, args in calls]
                    return names, results

    return anyio.run(main)


def payload(result) -> dict:
    assert not result.isError, result.content
    return result.structuredContent or json.loads(result.content[0].text)


def test_smoke_every_tool_and_the_refused_approval(server):
    srv, conn, _ = server
    shift_id, case_id = exhaust(conn)  # an open case with plan B waiting for the manager
    plan_id, plan_hash = conn.execute(
        "SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case_id,)
    ).fetchone()
    offers_before = conn.execute("SELECT count(*) FROM offer").fetchone()[0]
    other = conn.execute(
        "SELECT id FROM shift WHERE status = 'scheduled' AND venue_id = 'V1' AND starts_at > now() - interval '0 s'"
        " ORDER BY starts_at, id LIMIT 1"
    ).fetchone()
    other_shift = other[0] if other else shift_id
    names, results = call_all(f"{srv.url}/mcp", [
        ("shifts_on_day", {"day": "2026-11-09", "start": "06:00"}),
        ("who_is_allowed", {"shift_id": other_shift}),
        ("fragile_shifts", {"week_start": "2026-11-08"}),
        ("get_case", {"case_id": str(case_id)}),
        ("request_cover", {"shift_id": other_shift}),
        ("approve_plan_b", {"plan_id": plan_id, "hash": plan_hash}),
    ])
    assert TOOLS <= names
    day, allowed, fragile, case, proposal, refused = results
    assert any(s["shift_id"] == shift_id for s in payload(day)["shifts"])
    allowed = payload(allowed)
    assert allowed["counts"]["allowed"] >= 1 and allowed["allowed"][0]["why_first"]
    assert all(person["reasons"] for person in allowed["not_allowed"])
    fragile = payload(fragile)
    assert fragile["legend"][0]["level"] == "at_risk" and fragile["headline"]
    case = payload(case)
    assert case["status"] == "open" and case["plan_b"][0]["status"] == "proposed"
    proposal = payload(proposal)
    assert proposal["status"] == "proposed" and proposal["sent"] is False and proposal["booked"] is False
    assert refused.isError is True
    text = refused.content[0].text
    assert "Refused by the autonomy policy (N1)" in text and "plan_b = 'propose'" in text
    # nothing moved: no offer sent, the plan still waits for the manager, the proposal is only a proposal
    assert conn.execute("SELECT count(*) FROM offer").fetchone()[0] == offers_before
    assert conn.execute("SELECT status FROM plan_b WHERE id = %s", (plan_id,)).fetchone()[0] == "proposed"
    row = conn.execute("SELECT shift_id, status, requested_by FROM ext_cover_request").fetchone()
    assert row[0] == other_shift and row[1] == "proposed" and row[2].startswith("mcp:public:")


def test_public_limit_and_keys(server):
    srv, conn, monkeypatch = server
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    monkeypatch.setenv("COACHCOVER_MCP_CALLS_PER_HOUR", "2")
    with httpx.Client(timeout=30) as http:
        codes = [http.post(f"{srv.url}/mcp", json=body, headers=headers).status_code for _ in range(3)]
        assert codes[2] == 429 and 429 not in codes[:2]
        monkeypatch.setenv("COACHCOVER_MCP_KEY", "test-key-123")
        assert http.post(f"{srv.url}/mcp", json=body, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        keyed = http.post(f"{srv.url}/mcp", json=body, headers={**headers, "Authorization": "Bearer test-key-123"})
        assert keyed.status_code == 200, keyed.text  # a key is not limited
        assert {t["name"] for t in keyed.json()["result"]["tools"]} >= TOOLS
        monkeypatch.setenv("COACHCOVER_POLICY_ID", "some-real-school")
        assert http.post(f"{srv.url}/mcp", json=body, headers=headers).status_code == 401
