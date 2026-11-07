""""What if?": a plan without actions. After any question the database is exactly as before."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from coachcover import ext_db, whatif
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN
from tests.test_ext_a2a import ext_app

FIXTURES = Path(__file__).parent / "fixtures"
RU_QUESTION = "А если завтра и Зои заболеет?"
EN_QUESTION = "What if Zoe is off tomorrow too?"


def test_fallback_parser_reads_names_and_days():
    names = {"zoe", "marina", "leo"}
    assert whatif.fallback_parse("what if Zoe is sick tomorrow?", names)["people"] == [{"name": "zoe", "day": "tomorrow"}]
    assert whatif.fallback_parse(RU_QUESTION, names)["people"] == [{"name": "zoe", "day": "tomorrow"}]
    assert whatif.fallback_parse("а если послезавтра Лео и Марина?", names)["people"] == [
        {"name": "leo", "day": "day_after_tomorrow"}, {"name": "marina", "day": "day_after_tomorrow"}]
    assert whatif.fallback_parse("y si Zoé falta hoy", names)["people"] == [{"name": "zoe", "day": "today"}]
    assert whatif.fallback_parse("what if the boiler breaks", names)["people"] == []


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    ext_db.ensure_schema(conn)
    dsn = make_conninfo(TEST_DSN, dbname=conn.execute("SELECT current_database()").fetchone()[0])
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    app = ext_app(dsn)
    with TestClient(app) as client:
        yield client, conn


def whole_database(conn) -> str:
    """Every row of every table in the public schema, hashed."""
    digest = hashlib.sha256()
    tables = [t for (t,) in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        " ORDER BY 1")]
    assert {"shift", "cover_case", "offer", "case_event", "message", "plan_b", "model_call"} <= set(tables)
    for table in tables:
        rows = conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall()
        digest.update(f"{table}:{len(rows)}:{rows!r}".encode())
    return digest.hexdigest()


pytestmark_db = pytest.mark.db


def give_zoe_a_shift_tomorrow(conn) -> str:
    """The training data may leave Zoe free on Tuesday; then she takes one Tuesday shift of her role."""
    zoe = conn.execute("SELECT id FROM staff WHERE full_name LIKE 'Zoe %' AND policy_id = 'demo-school'").fetchone()[0]
    has = conn.execute(
        "SELECT 1 FROM shift WHERE staff_id = %s AND (starts_at AT TIME ZONE 'America/New_York')::date = '2026-11-10'",
        (zoe,),
    ).fetchone()
    if not has:
        conn.execute(
            "UPDATE shift SET staff_id = %s WHERE id = (SELECT s.id FROM shift s JOIN staff_role sr ON sr.role_id = s.role_id"
            " AND sr.staff_id = %s WHERE s.venue_id = 'V1' AND s.status = 'scheduled'"
            " AND (s.starts_at AT TIME ZONE 'America/New_York')::date = '2026-11-10' ORDER BY s.starts_at, s.id LIMIT 1)",
            (zoe, zoe),
        )
    return zoe


@pytestmark_db
def test_text_question_gives_a_plan_and_changes_nothing(api):
    client, conn = api
    give_zoe_a_shift_tomorrow(conn)
    before = whole_database(conn)
    resp = client.post("/api/whatif", json={"text": RU_QUESTION})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert whole_database(conn) == before
    assert body["dry_run"] is True and body["database"]["unchanged"] is True
    assert body["understood"]["by"] == "muse-glimmer-30b"
    person = body["understood"]["people"][0]
    assert person["name"] == "Zoe" and person["day"] == "tomorrow" and person["staff_name"].startswith("Zoe ")
    plan = body["plan"][0]
    assert plan["date"] == "2026-11-10" and plan["shifts"], plan
    first = plan["shifts"][0]
    assert first["cleared"] >= 1 and first["queue"] and first["who_steps_in"]["name"]
    assert first["if_nobody_says_yes"]["options"] and first["if_nobody_says_yes"]["recommended"]
    assert body["summary"]["shifts"] == len(plan["shifts"]) and body["summary"]["headline"].startswith(person["staff_name"])
    assert [c["purpose"] for c in body["model_calls"]] == ["whatif_parse"] and body["model_calls"][0]["synthetic"] is True


@pytestmark_db
def test_voice_question_through_replay(api):
    client, conn = api
    give_zoe_a_shift_tomorrow(conn)
    before = whole_database(conn)
    audio = (FIXTURES / "audio" / "whatif-ru.ogg").read_bytes()
    resp = client.post("/api/whatif", files={"audio": ("whatif-ru.ogg", audio, "audio/ogg")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"] == {"text": RU_QUESTION, "heard_from": "voice", "language": "ru"}
    assert [c["purpose"] for c in body["model_calls"]] == ["transcribe", "whatif_parse"]
    assert all(c["synthetic"] for c in body["model_calls"])
    assert body["plan"][0]["staff_name"].startswith("Zoe ")
    assert whole_database(conn) == before


@pytestmark_db
def test_where_the_norm_would_break_and_unknown_names(api):
    client, conn = api
    before = whole_database(conn)
    body = client.post("/api/whatif", json={"text": "What if Marina is off today?"}).json()
    assert body["understood"]["by"] == "fallback"  # no recorded answer for this question: the small parser read it
    assert body["understood"]["people"][0]["staff_name"] == "Marina Petrova"
    breaks = [b for s in body["plan"][0]["shifts"] for b in (s["if_nobody_says_yes"] or {}).get("norm_breaks", [])]
    assert breaks and all(b["participants"] > b["capacity"] for b in breaks)
    assert "the norm is 1:" in breaks[0]["text"]
    assert body["summary"]["norm_breaks_if_nobody"] == len(breaks)
    nobody = client.post("/api/whatif", json={"text": "What if Zed is off?"}).json()
    assert nobody["plan"] == [] and nobody["understood"]["people"] == []
    assert client.post("/api/whatif", json={"text": "  "}).status_code == 422
    assert whole_database(conn) == before


@pytestmark_db
def test_the_dry_run_sees_the_current_state(api):
    """A shift already opened by a real call-out is reported, not opened twice; the real case stays as it was."""
    client, conn = api
    client.post("/api/callouts", data={"staff_id": "S05", "text": "Marina here, sick, cannot come today"})
    before = whole_database(conn)
    body = client.post("/api/whatif", json={"text": "What if Marina is off today?"}).json()
    plan = body["plan"][0]
    assert plan["shifts"] == [] and ("no shifts left" in plan.get("note", "") or "not simulated" in plan.get("note", ""))
    assert whole_database(conn) == before
