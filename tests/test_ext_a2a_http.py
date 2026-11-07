"""Two agents, two addresses: the borrower's server and the lender's server run as real HTTP servers on two ports
of this machine and talk A2A 1.0 over the network stack. Same training database (one machine, demo)."""

from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from psycopg.conninfo import make_conninfo

from coachcover import ext_api
from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN
from tests.test_ext_a2a import exhaust

pytestmark = pytest.mark.db


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, dsn: str, port: int):
        app = create_app(dsn)
        app.include_router(ext_api.router)
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError(f"server on {self.port} did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture
def two_servers(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    borrower_port, lender_port = free_port(), free_port()
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.setenv("COACHCOVER_A2A_PEERS", json.dumps({
        "V1": f"http://127.0.0.1:{borrower_port}/a2a/V1", "N1": f"http://127.0.0.1:{lender_port}/a2a/N1"}))
    with Server(dsn, borrower_port) as borrower, Server(dsn, lender_port) as lender:
        yield borrower, lender, conn


def test_loan_over_http_between_two_addresses(two_servers):
    borrower, lender, conn = two_servers
    shift_id, case_id = exhaust(conn)
    with httpx.Client(timeout=30) as http:
        card = http.get(f"{borrower.url}/.well-known/agent-card.json").json()
        urls = {i["url"] for i in card["supportedInterfaces"]}
        assert urls == {f"{borrower.url}/a2a/V1", f"{lender.url}/a2a/N1"}
        started = http.post(f"{borrower.url}/api/network/loans", json={"shift_id": shift_id})
        assert started.status_code == 201, started.text
        loan = started.json()
        assert loan["status"] == "awaiting_approval" and loan["candidate"]["name"] == "Kai Morgan"
        sent = [s for s in loan["steps"] if s["kind"] == "request_sent"]
        assert sent and sent[0]["data"]["url"] == f"{lender.url}/a2a/N1"
        # the lender's task lives at the lender's address
        task = http.post(f"{lender.url}/a2a/N1", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": loan["a2a"]["task_id"]}}).json()
        assert task["result"]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert task["result"]["status"]["timestamp"].endswith("Z")
        # each manager approves at their own venue's address
        one = http.post(f"{borrower.url}/api/network/loans/{loan['code']}/approve", json={"manager": "Dana Ruiz", "venue": "V1"})
        assert one.json()["status"] == "awaiting_approval"
        two = http.post(f"{lender.url}/api/network/loans/{loan['code']}/approve", json={"manager": "Hannah Cole", "venue": "N1"})
        assert two.status_code == 200, two.text
        assert two.json()["status"] == "assigned"
        task = http.post(f"{lender.url}/a2a/N1", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": loan["a2a"]["task_id"]}}).json()["result"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["artifacts"][0]["parts"][0]["data"]["type"] == "loan.recorded"
        assert [m["role"] for m in task["history"]] == ["ROLE_USER", "ROLE_AGENT"] * 3
    assert conn.execute("SELECT staff_id FROM shift WHERE id = %s", (shift_id,)).fetchone()[0] == "N01"
    assert conn.execute("SELECT status FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0] == "covered"
