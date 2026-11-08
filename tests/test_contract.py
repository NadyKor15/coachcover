"""The API answers in the shape the screens read: the WEB lane's fixtures are the reference (web/fixtures/*.json).

Shape rule: every key of the fixture is in the live answer, with a compatible type; null on either side is
"unknown" and matches anything. The live answer may carry extra keys (night_check, judge, flagged, ...).
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from coachcover.api import create_app
from coachcover.demo import reset_school
from tests.conftest import TEST_DSN

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
WEB_FIXTURES = ROOT / "web" / "fixtures"
FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((WEB_FIXTURES / name).read_text(encoding="utf-8"))


def _kind(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def shape_gaps(live, ref, path="$") -> list[str]:
    if ref is None or live is None:
        return []
    if _kind(live) != _kind(ref):
        return [f"{path}: {_kind(live)} instead of {_kind(ref)}"]
    gaps = []
    if isinstance(ref, dict):
        for key, value in ref.items():
            if key not in live:
                gaps.append(f"{path}.{key}: missing")
            else:
                gaps += shape_gaps(live[key], value, f"{path}.{key}")
    elif isinstance(ref, list) and ref:
        if not live:
            return [f"{path}: empty list"]
        if all(isinstance(x, dict) and "kind" in x for x in ref):  # steps / timeline: compare by kind
            by_kind = {x["kind"]: x for x in live if isinstance(x, dict)}
            for item in ref:
                if item["kind"] in by_kind:
                    gaps += shape_gaps(by_kind[item["kind"]], item, f"{path}[{item['kind']}]")
        else:
            gaps += shape_gaps(live[0], ref[0], f"{path}[0]")
    return gaps


@pytest.fixture
def api(new_database, monkeypatch):
    conn = new_database()
    reset_school(conn)
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    monkeypatch.setenv("COACHCOVER_JUDGE_RUNS_PER_HOUR", "20")
    monkeypatch.delenv("COACHCOVER_MODEL_MODE", raising=False)
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname))) as client:
        yield client, conn


def test_shape_check_is_not_blind():
    ref = load("today.json")
    assert shape_gaps(ref, ref) == []
    broken = json.loads(json.dumps(ref))
    del broken["callout"]["transcript_en"]
    broken["eligibility"]["total"] = "40"
    assert shape_gaps(broken, ref) == ["$.callout.transcript_en: missing", "$.eligibility.total: str instead of number"]


def test_today_after_dangerous_morning_matches_the_screen_fixture(api):
    client, _ = api
    run = client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    assert run.status_code == 200 and run.json()["case_id"] == "C-1109-01"
    today = client.get("/api/today").json()
    ref = load("today.json")
    assert shape_gaps(today, ref) == []
    # names and the story, from the data
    assert today["title"] == "Who is allowed on deck at 09:30"
    assert today["venue"] == {"id": "V1", "name": "Riverside Aquatic Center", "term": "pool"}
    assert today["user_label"] == "Natalie Brooks · Manager" and today["now_label"] == "Mon, Nov 9 · 08:45"
    callout = today["callout"]
    assert callout["staff"] == "Marina Petrova · Instructor · with Emma Carter" and callout["case_id"] == "C-1109-01"
    assert callout["transcript_original"] == ref["callout"]["transcript_original"]
    assert callout["transcript_en"] == ref["callout"]["transcript_en"]
    assert callout["language"] == "Russian" and callout["duration_label"] == "0:09"
    assert callout["transcript_original"].startswith("Привет, это Марина. У меня температура 38, к 9:30 я не выйду.")
    assert callout["asr_label"] == "whisper · prepared demo answer"  # the note is synthesized locally, not a model run
    assert today["shift"] == {
        "id": "L-1109-G002", "time_label": "09:30–10:15",
        "lanes_label": "Teaching pool + Preschool swim, ages 3–5 (8 kids)",  # two lines; Emma is on the call-out line
        "needs_label": "Instructor", "program_label": "Preschool training (ages 3–5)", "private": False,
    }
    elig = today["eligibility"]
    assert (elig["total"], elig["eligible"]) == (40, 5)
    chips = {r["label"]: r["count"] for r in elig["reasons"]}
    assert chips["No preschool training (ages 3–5)"] == 11 and chips["Role: not an instructor"] == 13
    assert not any(label.startswith("Only ") for label in chips)  # nobody trained for preschool is short of rest today
    assert elig["first_refusal"]["staff"] == "Anna Kim"
    assert elig["first_refusal"]["label"] == "free and willing · BLOCKED: WSI certificate expired Nov 5 · 8 kids · ratio 1:4"
    assert elig["first_refusal"]["contrast_quote"] == ref["eligibility"]["first_refusal"]["contrast_quote"]
    assert sum(r["count"] for r in elig["reasons"]) == 40 - 5
    assert {r["tone"] for r in elig["reasons"]} <= {"red", "amber", "plain"}
    assert [s["name"] for s in elig["staff"] if s["highlight"]] == ["Zoe Grant"]
    assert [(o["staff"], o["result"]) for o in today["offers"]] == [
        ("Sofia Reyes", "declined"), ("Leo Martins", "no_reply"), ("Zoe Grant", "accepted")]
    zoe = today["offers"][-1]
    assert zoe["why_first"] == "Zoe third: 12 h this week, Preschool, same pool" and zoe["time_label"] == "08:44"
    assert [(t["time_label"], t["kind"]) for t in today["timeline"]] == [
        ("08:40", "callout"), ("08:43", "declined"), ("08:44", "accepted"), ("08:45", "no_reply"), ("09:30", "shift_start")]
    assert today["outcome"] == {"covered_by": "Zoe Grant", "covered_via": None, "covered_time": "08:45", "lessons_cancelled": 0,
                                "private_line": None, "family_wait_until": None}  # closed at 08:45; the last two: private lessons
    assert today["plan_b"]["state"] == "standby" and today["plan_b"]["autonomy"] == "propose"
    assert today["plan_b"]["text"].startswith("No merge: 14 kids over the norm")  # the kids' norm refuses the merge, live
    money = today["money"]  # from coachcover.money with the preset's prices
    assert money["uncovered_morning_usd"] == 240.0  # 8 children x $30, the founder's price
    assert money["uncovered_morning_label"] == "If nobody had accepted: $240 (Refunds: 8 participants × $30)"
    assert money["manager_hour_usd"] == 25.0 and 0 < money["case_cost_cents"] < 1
    assert today["model_log"]["calls"] >= 4 and today["model_log"]["usd"] > 0
    # one cost of this cover on the whole screen: the footer and the money card show the same number
    assert round(today["model_log"]["usd"] * 100, 4) == money["case_cost_cents"]
    assert money["case_cost_label"] in today["model_log"]["label"]
    assert today["flagged"][0]["staff"] == "Anna Kim"  # her "assign me anyway" message was flagged
    assert today["judge"] == {"running": False, "case_id": "C-1109-01", "scenario": "dangerous_morning"}  # the plaque names a scene
    # the same 12 hours on the phone
    offer_id = zoe_offer = next(o["id"] for o in today["offers"] if o["staff"] == "Zoe Grant")
    phone = client.get(f"/api/offers/{zoe_offer}").json()
    assert "12 h this week" in phone["why"] and offer_id.startswith("O-")


def test_offer_phone_matches_the_fixture(api):
    client, conn = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    offer_id = conn.execute("SELECT id FROM offer WHERE staff_id = 'S17'").fetchone()[0]
    phone = client.get(f"/api/offers/O-{offer_id}").json()
    assert shape_gaps(phone, load("offers/O-1.json")) == []
    assert (phone["shift_day"], phone["shift_start"], phone["shift_end"]) == ("today", "09:30", "10:15")
    assert phone["venue"] == "Riverside Aquatic Center" and phone["manager"] == {"name": "Natalie Brooks", "role": "Manager"}
    assert phone["shift_detail"] == "Teaching pool + Preschool swim, ages 3–5 (8 kids) · with Emma Carter"
    assert [w.split(" — ")[0] for w in phone["why"][:2]] == ["WSI", "CPR/AED"]
    assert all(" — valid to " in w for w in phone["why"][:2])
    assert phone["why"][2:] == ["Preschool training (ages 3–5) — current", "12 h this week"]
    assert phone["window_s"] == 300


def test_case_page_matches_the_fixture(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    page = client.get("/api/cases/C-1109-01").json()
    assert shape_gaps(page, load("cases/C-1109-01.json")) == []
    assert (page["status"], page["status_label"], page["covered_time_label"]) == ("covered", "Covered", "08:45")
    kinds = [s["kind"] for s in page["steps"]]
    assert kinds[0] == "callout" and kinds[-1] == "covered" and {"shift", "allowed", "offers", "flagged"} <= set(kinds)
    allowed = next(s for s in page["steps"] if s["kind"] == "allowed")
    assert (allowed["total"], allowed["allowed"], len(allowed["staff"])) == (40, 5, 40)
    shift = next(s for s in page["steps"] if s["kind"] == "shift")
    assert shift["summary"] == "09:30–10:15 · Needs: Instructor · Preschool training (ages 3–5)"
    covered = next(s for s in page["steps"] if s["kind"] == "covered")
    assert covered["summary"] == "09:30 covered by Zoe Grant, teaching with Emma Carter · 0 lessons cancelled"
    assert allowed["blocked"]["staff"] == "Anna Kim"
    labels = {c["model_label"] for c in page["model_calls"]}
    assert {"whisper-large-v3-turbo", "muse-glimmer-30b", "nemotron-safety"} <= labels
    whisper = next(c for c in page["model_calls"] if c["purpose"] == "transcribe")
    assert whisper["ms"] == 1050 and whisper["cost_usd"] > 0 and "synthetic-transcribe-ru-0930.json (synthetic)" in whisper["source"]
    assert page["model_cost_usd"] == pytest.approx(sum(c["cost_usd"] or 0 for c in page["model_calls"]))
    risk = page["risk"]
    assert (risk["kids"], risk["refunds_usd"], risk["overtime_usd"], risk["total_usd"]) == (8, 240.0, None, 240.0)
    assert risk["assumptions"]["lesson_price"]["assumption"] is False  # the founder's price
    assert risk["assumptions"]["manager_hour"]["assumption"] is True
    assert page["proof"]["policy_label"] == "cover = auto · plan B = propose"
    download = client.get("/api/cases/C-1109-01?download=true")
    assert "attachment" in download.headers["content-disposition"] and download.json() == page
    assert client.get("/api/cases/C-0000-99").status_code == 404


def test_today_after_nobody_accepts_matches_its_fixture_and_approval_is_by_hash(api):
    client, _ = api
    run = client.post("/api/judge/run", json={"scenario": "nobody_accepts"}).json()
    assert run["conflict"]["reason"] == "plan changed"  # the shown plan was refused once the data changed
    today = client.get("/api/today").json()
    assert shape_gaps(today, load("today.nobody_accepts.json")) == []
    plan = today["plan_b"]
    assert plan["state"] == "proposed" and plan["id"] == run["conflict"]["plan_id"]
    assert today["outcome"]["covered_by"] is None
    stale = client.post(f"/api/plans/{plan['id']}/approve", json={"hash": "a" * 64})
    assert stale.status_code == 409 and stale.json()["reason"] == "plan changed" and stale.json()["current_hash"] == plan["hash"]
    ok = client.post(f"/api/plans/{plan['id']}/approve", json={"hash": plan["hash"]})
    assert ok.status_code == 200 and ok.json()["state"] == "approved"
    assert client.get("/api/today").json()["plan_b"]["state"] == "approved"


def test_nightly_check_opens_a_case_for_the_manager(api):
    client, conn = api
    run = client.post("/api/judge/run", json={"scenario": "nightly_check"}).json()
    today = client.get("/api/today").json()
    night = today["night_check"]
    assert night["case_id"] == run["case_id"]
    assert night["label"].startswith("Night check 21:30: Javier Morales — CPR/AED card ends Nov 8, before Mon 16:00")
    assert night["label"].endswith("replacement proposed to the manager")
    approval = conn.execute("SELECT id, status FROM approval WHERE action = 'night_cover'").fetchone()
    assert approval[1] == "pending"
    assert conn.execute("SELECT count(*) FROM offer").fetchone() == (0,)  # nothing happens before the manager says yes
    assert client.post(f"/api/approvals/{approval[0]}/approve", json={"by": "manager:Natalie"}).status_code == 200
    assert conn.execute("SELECT count(*) FROM offer WHERE status = 'pending'").fetchone() == (3,)  # one wave of three


def test_accept_rechecked_at_yes_answers_409_with_a_reason(api):
    client, conn = api
    with open(FIXTURES / "audio" / "ru-callout.ogg", "rb") as f:
        client.post("/api/callouts", data={"staff_id": "S05"}, files={"audio": ("ru-callout.ogg", f, "audio/ogg")})
    offer_id, staff_id = conn.execute("SELECT id, staff_id FROM offer WHERE status = 'pending'").fetchone()
    conn.execute(
        "INSERT INTO shift (id, venue_id, role_id, starts_at, ends_at, staff_id)"
        " SELECT 'X-CLASH', 'V2', role_id, starts_at, ends_at, %s FROM shift WHERE id = 'L-1109-G002'", (staff_id,)
    )
    refused = client.post(f"/api/offers/O-{offer_id}/accept")
    assert refused.status_code == 409 and "already booked" in refused.json()["reason"]
    next_offer = conn.execute("SELECT id FROM offer WHERE status = 'pending'").fetchone()[0]
    assert client.post(f"/api/offers/O-{next_offer}/decline").json() == {"state": "declined", "case_status": "open"}


def test_voice_note_is_served_back(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    audio = client.get("/media/callouts/C-1109-01.ogg")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/ogg"
    assert audio.content == (FIXTURES / "audio" / "ru-callout-0930.ogg").read_bytes()
    assert client.get("/media/callouts/C-0000-01.ogg").status_code == 404


def test_stats_in_the_contract_shape(api):
    client, _ = api
    client.post("/api/judge/run", json={"scenario": "dangerous_morning"})
    stats = client.get("/api/stats").json()
    assert set(stats) == {"runs", "closed_without_human_pct", "median_minutes_to_cover", "eligibility_violations", "sim"}
    assert stats["runs"] == 1 and stats["eligibility_violations"] == 0
    assert stats["sim"]["synthetic"] is True and stats["sim"]["coachcover_violations"] == 0
    assert stats["sim"]["mornings"] == 50 and stats["sim"]["baseline_violations"] > 0
    assert stats["closed_without_human_pct"] == 100.0 and round(stats["median_minutes_to_cover"], 2) == 4.83  # 08:40:10 -> 08:45:00


def test_static_screens_are_served(new_database, tmp_path):
    conn = new_database()
    reset_school(conn)
    for page in ("index.html", "offer.html", "case.html"):
        (tmp_path / page).write_text(f"<html>{page}</html>")
    dbname = conn.execute("SELECT current_database()").fetchone()[0]
    with TestClient(create_app(make_conninfo(TEST_DSN, dbname=dbname), web_dir=tmp_path)) as client:
        assert client.get("/").text == "<html>index.html</html>"
        assert client.get("/offer").text == "<html>offer.html</html>"
        assert client.get("/case").text == "<html>case.html</html>"
        assert client.get("/health").json()["mode"] == "replay"  # API routes win over the static mount
    assert (ROOT / "web" / "index.html").is_file()  # the real screens are in the repo
