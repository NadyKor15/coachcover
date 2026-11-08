"""The Agent Card shows addresses a visitor can reach; the agents keep calling each other on the internal ones.

With COACHCOVER_A2A_PEERS naming hosts only the compose network knows (`api`, `agent-north`), the public card shows
this server's own `/a2a/{venue}` instead. A peer on a public name is shown as it is. Calls between the agents
(`agent_url`, `send`) are not touched.
"""

from __future__ import annotations

import json

import pytest

from coachcover import a2a
from tests.test_ext_a2a import exhaust, net, start  # noqa: F401  (the fixture and the helpers of the A2A tests)

pytestmark = pytest.mark.db

INTERNAL = {"V1": "http://api:8000/a2a/V1", "N1": "http://agent-north:8000/a2a/N1"}


def test_card_shows_public_urls_not_the_internal_ones(net, monkeypatch):
    client, _ = net
    monkeypatch.setenv("COACHCOVER_A2A_PEERS", json.dumps(INTERNAL))
    card = client.get("/.well-known/agent-card.json", headers={"Host": "coachcover.example"}).json()
    urls = {i["url"] for i in card["supportedInterfaces"]}
    assert urls == {"http://coachcover.example/a2a/V1", "http://coachcover.example/a2a/N1"}
    text = json.dumps(card)
    assert "agent-north" not in text and "api:8000" not in text  # also not in the skills' descriptions
    assert all(f"JSON-RPC endpoint: http://coachcover.example/a2a/{s['tags'][-1]}" in s["description"] for s in card["skills"])


def test_a_public_peer_name_is_shown_as_it_is(net, monkeypatch):
    client, _ = net
    monkeypatch.setenv("COACHCOVER_A2A_PEERS", json.dumps({"N1": "https://north.example.org/a2a/N1"}))
    urls = {i["url"] for i in client.get("/.well-known/agent-card.json").json()["supportedInterfaces"]}
    assert "https://north.example.org/a2a/N1" in urls and "http://testserver/a2a/V1" in urls


def test_calls_between_agents_keep_the_internal_urls(net, monkeypatch):
    client, conn = net
    monkeypatch.setenv("COACHCOVER_A2A_PEERS", json.dumps(INTERNAL))
    assert a2a.agent_url("N1", "http://testserver/") == INTERNAL["N1"]
    assert a2a.public_agent_url("N1", "http://testserver/") == "http://testserver/a2a/N1"
    sent: list[str] = []
    real_send = a2a.send

    def recording_send(url, method, params):
        sent.append(url)
        return real_send(url, method, params)

    monkeypatch.setattr(a2a, "send", recording_send)
    shift_id, _ = exhaust(conn)
    loan = start(client, shift_id)  # the borrower's agent asks the lender's agent
    assert sent and set(sent) == {INTERNAL["N1"]}, sent
    # what people read shows the public address: the loan, its steps (data and detail line), the members of the network
    request_sent = next(s for s in loan["steps"] if s["kind"] == "request_sent")
    assert request_sent["data"]["url"] == "http://testserver/a2a/N1" and request_sent["detail"] == "http://testserver/a2a/N1"
    stored = conn.execute("SELECT data->>'url' FROM ext_loan_step WHERE kind = 'request_sent' LIMIT 1").fetchone()[0]
    assert stored == INTERNAL["N1"]  # the record keeps the address the call used


def test_no_internal_host_in_the_json_people_can_open(net, monkeypatch):
    client, conn = net
    monkeypatch.setenv("COACHCOVER_A2A_PEERS", json.dumps(INTERNAL))
    shift_id, _ = exhaust(conn)
    loan = start(client, shift_id)
    members = client.get("/api/network")
    assert members.status_code == 200
    assert {m["agent_url"] for m in members.json()["members"]} == {"http://testserver/a2a/V1", "http://testserver/a2a/N1"}
    pages = {
        "/api/network": members.text,
        "/api/network/loans/" + loan["code"]: client.get("/api/network/loans/" + loan["code"]).text,
        "POST /api/network/loans": json.dumps(loan),
        "/.well-known/agent-card.json": client.get("/.well-known/agent-card.json").text,
    }
    for name, text in pages.items():
        assert "agent-north" not in text and "api:8000" not in text, name
    one = client.post(f"/api/network/loans/{loan['code']}/approve", json={"manager": "Natalie Brooks", "venue": "V1"})
    assert one.status_code == 200 and "agent-north" not in one.text and "api:8000" not in one.text
