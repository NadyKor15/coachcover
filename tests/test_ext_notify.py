"""Team chat posts after each closed cover: short, linked to the case, delivery simulated, once per case."""

from __future__ import annotations

import logging
import re

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover import ext_api, ext_db, notify
from coachcover.api import create_app
from coachcover.demo import clock_now, reset_school
from coachcover.seed import POLICY_ID
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db


def test_channel_names():
    assert notify.channel_for("Riverside Aquatic Center") == "#riverside-staff"
    assert notify.channel_for("  St. Mary's Club ") == "#st-staff"
    assert notify.channel_for("") == "#team-staff"


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


def test_closed_covers_post_once_with_a_link(api, caplog):
    client, conn = api
    assert client.get("/api/team-posts").json()["posts"] == []
    client.post("/api/judge/run")
    with caplog.at_level(logging.INFO, logger="coachcover.notify"):
        posts = client.get("/api/team-posts").json()["posts"]
    closed = conn.execute("SELECT id, status FROM cover_case WHERE status IN ('covered', 'plan_b') ORDER BY id").fetchall()
    assert len(posts) == len(closed) >= 2
    venue = conn.execute("SELECT name FROM venue WHERE id = 'V1'").fetchone()[0]
    for post in posts:
        ref = notify.case_ref(conn, post["case_id"])
        assert post["channel"] == notify.channel_for(venue) and post["delivery"] == "simulated"
        assert re.fullmatch(r"\d\d:\d\d .+, case " + re.escape(ref), post["body"]), post["body"]
        assert post["link"].endswith(f"/case.html?id={ref}")
        assert f"would post to {post['channel']}: {post['body']}" in caplog.text
    covered = conn.execute(
        "SELECT c.id, e.data ->> 'name' FROM cover_case c JOIN case_event e ON e.case_id = c.id"
        " WHERE c.status = 'covered' AND e.kind = 'shift_covered' ORDER BY c.id LIMIT 1").fetchone()
    first_name = covered[1].split()[0]
    assert any(p["case_id"] == covered[0] and f"covered by {first_name}" in p["body"] for p in posts)
    assert client.get("/api/team-posts").json()["posts"] == posts  # once per case
    assert client.post("/api/team-posts/sweep").json()["new"] == []


def test_open_cases_do_not_post(api):
    client, conn = api
    client.post("/api/callouts", data={"staff_id": "S05", "text": "Marina, sick today"})
    ext_db.ensure_schema(conn)
    assert conn.execute("SELECT count(*) FROM cover_case WHERE status = 'open'").fetchone()[0] >= 1
    assert notify.sweep(conn, POLICY_ID, clock_now(conn)) == []
    assert conn.execute("SELECT count(*) FROM ext_team_post").fetchone()[0] == 0
