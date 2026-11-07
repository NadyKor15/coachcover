"""The core app serves the extension router (before the static mount, so POSTs are not swallowed by it)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture
def client(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    monkeypatch.delenv("COACHCOVER_MCP_KEY", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname), web_dir=WEB)) as c:
        yield c


def test_every_extension_route_answers_through_the_core_app(client):
    assert WEB.is_dir()  # the static mount is there, the extension routes still answer
    card = client.get("/.well-known/agent-card.json")
    assert card.status_code == 200 and card.json()["supportedInterfaces"]
    assert client.get("/api/network").status_code == 200
    loan = client.post("/api/network/loans", json={"shift_id": "L-1109-G002"})
    assert loan.status_code in (201, 409) and loan.status_code != 405, loan.text  # 409: own cleared people not asked yet
    assert client.get("/api/network/loans/LN-0000-00").status_code == 404
    week = client.get("/api/week", params={"venue": "V1", "start": "2026-11-08"})
    assert week.status_code == 200, week.text
    whatif = client.post("/api/whatif", json={"text": "What if Maria is sick tomorrow too?"})
    assert whatif.status_code == 200 and whatif.json()["dry_run"] is True, whatif.text
    pdf = client.get("/api/binder.pdf", params={"venue": "V1"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF"), pdf.text[:200]
    mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                      headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                               "MCP-Protocol-Version": "2025-06-18"})
    assert mcp.status_code == 200 and mcp.json()["result"]["tools"], mcp.text
    assert client.get("/api/today").status_code == 200 and client.get("/").status_code == 200  # the core still answers
