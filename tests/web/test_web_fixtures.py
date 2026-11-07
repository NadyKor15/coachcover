"""Fixtures of web/ are answers of the running API (tests/web/make_fixtures.py) and follow the API contract
(docs/API-CONTRACT-2026-11-06.md, v1, plus the fields listed in EXTENSIONS). The checks are relations between
answers, not copies of their values, so a new capture of the same story passes and a broken story fails.

Standard library only, so it runs under the project's pytest and on a bare python3.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
FX = WEB / "fixtures"

# Field sets of contract v1; EXTENSIONS are fields the API gives on top of v1 and the screens use or ignore.
TODAY_KEYS = {"title", "venue", "now_label", "user_label", "lang", "callout", "shift", "eligibility", "timeline", "offers",
              "outcome", "plan_b", "summary_cards", "money", "model_log"}
NESTED = {
    "venue": {"id", "name", "term"},
    "callout": {"case_id", "time_label", "staff", "audio_url", "duration_label", "language", "transcript_original", "transcript_en"},
    "shift": {"id", "time_label", "lanes_label", "needs_label", "program_label"},
    "eligibility": {"total", "eligible", "reasons", "staff", "first_refusal"},
    "outcome": {"covered_by", "covered_time", "lessons_cancelled"},
    "plan_b": {"id", "state", "text", "hash", "autonomy"},
    "money": {"uncovered_morning_usd", "uncovered_morning_label", "case_cost_cents", "manager_hour_usd"},
    "model_log": {"label", "calls", "usd"},
}
EXTENSIONS = {"today": {"flagged", "night_check", "ratio_alert", "judge", "network_loan"}, "callout": {"asr_label"},
              "outcome": {"covered_via", "private_line", "family_wait_until"}, "money": {"source", "case_cost_label"},
              "shift": {"private"}, "offer": {"state", "staff", "needs_label", "why_first", "private"}}
# the family's page of a private lesson (GET /api/client/{Q-n})
CLIENT_KEYS = {"ask_id", "kind", "state", "signed", "child", "lesson", "venue", "question", "coach", "instead_of", "day_label",
               "time_label", "answer_in_s", "answer_by_label", "thread", "current", "outcome", "manager"}
VENUE_TZ = ZoneInfo("America/New_York")  # the training school; the case page reads in the venue's time ("08:40:10 ET")
RATIO_KEYS = {"case_id", "status", "tone", "label", "action_label"}
OFFER_KEYS = {"offer_id", "shift_day", "shift_start", "shift_end", "venue", "shift_detail", "why", "expires_in_s", "window_s", "manager"}
CASE_KEYS = {"case_id", "status", "status_label", "covered_time_label", "shift_label", "venue", "date_label", "user_label",
             "opened_at", "closed_at", "steps", "proof", "model_calls", "model_cost_usd", "model_cost_label", "risk",
             "covered_by", "covered_via", "model_calls_total_label"}
# a model call on the case page: machine fields, and the Proof table's words (ET time, why, seconds, cents, share, source)
CALL_KEYS = {"at", "purpose", "model", "model_label", "ms", "input_tokens", "output_tokens", "cost_usd", "source",
             "time_label", "purpose_label", "seconds_label", "cost_label", "shared_label", "source_label"}
UTC_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)Z$")


def load(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


def today_files() -> list[str]:
    return sorted(p.name for p in FX.glob("today*.json"))


def hours(text: str | None) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?) h this week", text or "")
    return m.group(1) if m else None


def test_fixture_set_is_complete():
    assert today_files() == ["today.json", "today.network_assigned.json", "today.network_open.json",
                             "today.nightly_check.json", "today.nobody_accepts.json", "today.private_declined.json",
                             "today.private_lesson.json", "today.ratio_covered.json", "today.ratio_open.json"]
    m = load("mocks.json")
    assert set(m["judge_run"]) == {"dangerous_morning", "nobody_accepts", "nightly_check", "private_lesson", "private_declined"}
    clients = sorted(p.name for p in (FX / "clients").glob("*.json"))
    assert clients == ["Q-1.covered.json", "Q-1.credit.json", "Q-1.json", "Q-1.moved.json", "Q-1.no.json", "Q-2.credit.json",
                       "Q-2.json", "Q-2.moved.json", "mocks.json"]
    for file in [*m["today_by_scenario"].values(), *m["today_by_mock"].values()]:
        assert (FX / file).is_file(), file
    t = load("today.json")
    assert (FX / "cases" / f"{t['callout']['case_id']}.json").is_file()
    for o in t["offers"]:
        assert (FX / "offers" / f"{o['id']}.json").is_file(), o["id"]


def test_today_fields_follow_the_contract():
    for name in today_files():
        d = load(name)
        assert TODAY_KEYS <= set(d) <= TODAY_KEYS | EXTENSIONS["today"], name
        for key, fields in NESTED.items():
            if d[key] is None:
                continue
            assert fields <= set(d[key]) <= fields | EXTENSIONS.get(key, set()), (name, key)
        if d["eligibility"]:
            assert all({"code", "label", "count", "tone"} == set(r) for r in d["eligibility"]["reasons"]), name
            assert all({"id", "name", "eligible", "highlight", "reason_label"} == set(s) for s in d["eligibility"]["staff"]), name
        assert all({"time_label", "kind"} <= set(t) <= {"time_label", "kind", "label"} for t in d["timeline"]), name
        # result_label: the offer's result in words (the same words as its timeline label, or on its own when the
        # family of a private lesson decides)
        assert all({"id", "time_label", "staff", "result", "why_first", "result_label"} == set(o) for o in d["offers"]), name
        if d.get("ratio_alert"):
            assert RATIO_KEYS <= set(d["ratio_alert"]), name


def test_eligibility_adds_up():
    for name in today_files():
        e = load(name)["eligibility"]
        if not e:
            continue
        staff = e["staff"]
        assert len(staff) == e["total"], name
        assert sum(s["eligible"] for s in staff) == e["eligible"], name
        assert sum(r["count"] for r in e["reasons"]) == e["total"] - e["eligible"], name
        assert len({s["id"] for s in staff}) == len(staff), name
        for r in e["reasons"]:
            assert sum(1 for s in staff if s["reason_label"] == r["label"]) == r["count"], (name, r["label"])


def test_morning_story():
    d = load("today.json")
    e = d["eligibility"]
    fr = e["first_refusal"]
    blocked = [s for s in e["staff"] if s["name"] == fr["staff"]]
    assert len(blocked) == 1 and not blocked[0]["eligible"]
    assert "BLOCKED" in fr["label"] and "RosterElf" in fr["contrast_quote"]
    assert all(o["staff"] != fr["staff"] for o in d["offers"])
    eligible = {s["name"] for s in e["staff"] if s["eligible"]}
    assert {o["staff"] for o in d["offers"]} <= eligible
    accepted = [o for o in d["offers"] if o["result"] == "accepted"]
    assert len(accepted) == 1 and d["outcome"]["covered_by"] == accepted[0]["staff"]
    highlight = [s for s in e["staff"] if s["highlight"]]
    assert len(highlight) == 1 and highlight[0]["name"] == accepted[0]["staff"]
    assert d["callout"]["staff"].startswith("Marina Petrova")
    codes = {r["code"]: r["label"] for r in e["reasons"]}
    assert "REVOKED by issuer" in codes["CERT_REVOKED"] and "(simulated registry)" in codes["CERT_REVOKED"]
    if "REST_TOO_SHORT" in codes:  # the 09:30 morning has nobody short of rest; the label shape still holds when it does
        assert re.match(r"Only \d+(\.\d+)? h rest since \d+:\d\d$", codes["REST_TOO_SHORT"])
    assert d["ratio_alert"] is None and d["night_check"] is None


def test_hours_are_the_same_on_c_and_on_the_phone():
    d = load("today.json")
    for o in d["offers"]:
        phone = load(f"offers/{o['id']}.json")
        assert phone["offer_id"] == o["id"] and phone["staff"] == o["staff"]
        assert hours(o["why_first"]) == hours(" ".join(phone["why"])), o["id"]
    accepted = next(o for o in d["offers"] if o["result"] == "accepted")
    assert load(f"offers/{accepted['id']}.json")["state"] == "accepted"


def test_offer_fields_follow_the_contract():
    pending = 0
    for path in sorted((FX / "offers").glob("*.json")):
        o = json.loads(path.read_text(encoding="utf-8"))
        assert OFFER_KEYS <= set(o) <= OFFER_KEYS | EXTENSIONS["offer"], path.name
        assert set(o["manager"]) == {"name", "role"}
        if o["state"] == "pending":
            pending += 1
            assert 0 < o["expires_in_s"] <= o["window_s"], path.name
    assert pending >= 1  # the phone screen needs one open offer


def test_money_comes_from_the_api_and_reads_as_the_screen_shows_it():
    m = load("today.json")["money"]
    assert m["uncovered_morning_usd"] and m["uncovered_morning_usd"] > 0
    label = re.match(r"^(.*?):\s*\$([\d,]+(?:\.\d+)?)\s*\((.*)\)$", m["uncovered_morning_label"])
    assert label and label.group(1) == "If nobody had accepted"
    assert float(label.group(2).replace(",", "")) == m["uncovered_morning_usd"]
    assert m["source"] == "from the founder's coaching experience"


def test_ratio_banner_red_then_green():
    opened, covered = load("today.ratio_open.json")["ratio_alert"], load("today.ratio_covered.json")["ratio_alert"]
    assert opened["case_id"] == covered["case_id"]
    assert (opened["status"], opened["tone"]) == ("open", "red")
    assert (covered["status"], covered["tone"]) == ("covered", "green") and covered["action_label"].startswith("Covered by ")
    assert (FX / "cases" / f"{opened['case_id']}.json").is_file()


def test_case_follows_the_morning():
    t = load("today.json")
    d = load(f"cases/{t['callout']['case_id']}.json")
    assert set(d) == CASE_KEYS
    stamps = [s["at"] for s in d["steps"]]
    assert stamps == sorted(stamps)
    for s in d["steps"]:
        assert UTC_STAMP.match(s["at"]), s  # machine fields stay in UTC; the label is the venue's time
        local = datetime.fromisoformat(s["at"].replace("Z", "+00:00")).astimezone(VENUE_TZ)
        assert s["time_label"] == f"{local:%H:%M:%S} ET", s
    steps = {s["kind"]: s for s in d["steps"]}
    assert (steps["callout"]["transcript_original"], steps["callout"]["transcript_en"]) == \
        (t["callout"]["transcript_original"], t["callout"]["transcript_en"])
    assert steps["allowed"]["blocked"] == {"staff": t["eligibility"]["first_refusal"]["staff"],
                                           "label": t["eligibility"]["first_refusal"]["label"]}
    assert sum(r["count"] for r in steps["allowed"]["reasons"]) == steps["allowed"]["total"] - steps["allowed"]["allowed"]
    assert [(o["staff"], o["result"], o["why_first"]) for o in steps["offers"]["offers"]] == \
        [(o["staff"], o["result"], o["why_first"]) for o in t["offers"]]
    assert all(set(c) == CALL_KEYS for c in d["model_calls"])
    assert len(d["model_calls"]) == t["model_log"]["calls"]
    assert round(sum(c["cost_usd"] or 0 for c in d["model_calls"]), 6) == d["model_cost_usd"]
    assert d["risk"]["total_usd"] == t["money"]["uncovered_morning_usd"]
    # the Proof table: every row in the venue's time, cents that add up to the Models line, the call-out's share named
    for c in d["model_calls"]:
        local = datetime.fromisoformat(c["at"].replace("Z", "+00:00")).astimezone(VENUE_TZ)
        assert c["time_label"] == f"{local:%H:%M:%S} ET" and c["seconds_label"] == f"{c['ms'] / 1000:.1f} s", c
        assert c["source_label"] == "prepared" and c["purpose_label"][0].isupper() and "_" not in c["purpose_label"]
    cents = [round(float(c["cost_label"].rstrip("¢")) * 1000) for c in d["model_calls"]]
    assert sum(cents) == round(float(d["model_cost_label"].rstrip("¢")) * 1000)
    assert d["model_calls_total_label"] == f"{len(d['model_calls'])} calls · {d['model_cost_label']}"
    assert {c["shared_label"] for c in d["model_calls"]} == {"shared by 2 cases", None}  # Anna's message check is its own


def test_model_calls_are_recorded_answers():
    t = load("today.json")
    for c in load(f"cases/{t['callout']['case_id']}.json")["model_calls"]:
        assert c["source"].startswith("recorded answer tests/fixtures/models/"), c["source"]
    assert "replay mode: recorded and prepared demo answers" in t["model_log"]["label"]  # honest: part of the replay answers are hand-made


def test_mocks_match_the_fixtures():
    m = load("mocks.json")
    nobody = load("today.nobody_accepts.json")
    plan = m["plans"][nobody["plan_b"]["id"]]
    assert nobody["plan_b"]["state"] == "proposed" and plan["current_hash"] == nobody["plan_b"]["hash"]
    assert plan["conflict"]["reason"] == "plan changed" and plan["approve"]["state"] == "approved"
    for oid, o in m["offers"].items():
        assert (FX / "offers" / f"{oid}.json").is_file(), oid
        assert o["accept"]["state"] == "accepted" and o["decline"]["state"] == "declined" and o["accept_conflict"]["reason"]


def test_pages_declare_charset_first():
    for page in ("index.html", "offer.html", "case.html", "week.html", "network.html", "client.html"):
        head = (WEB / page).read_text(encoding="utf-8").split("<head>", 1)[1].lstrip()
        assert head.startswith('<meta charset="UTF-8">'), page


def test_fixtures_carry_no_internal_reference():
    # the repository's own guard (tests/test_public_repo_guard.py) skips web/fixtures; here it reads them
    from tests.test_public_repo_guard import find

    files = sorted(FX.rglob("*.json"))
    assert len(files) > 20
    hits = [f"{p.relative_to(ROOT)}:{n}: {find(line)}" for p in files
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if find(line)]
    assert hits == []


def test_week_follows_its_threshold_and_legend():
    w = load("week.json")
    t = w["threshold"]
    levels = {it["level"]: it for it in w["legend"]}
    assert set(levels) == {"at_risk", "fragile", "safe"}
    assert levels["fragile"]["max"] == t and levels["safe"]["min"] == t + 1 and levels["at_risk"]["max"] == 0
    cells = [c for d in w["days"] for c in d["cells"]]
    assert len(cells) == w["counts"]["shifts"]
    for c in cells:
        if c["past"]:
            assert c["level"] == "past" and c["backups"] is None
        else:
            want = "at_risk" if c["backups"] == 0 else "fragile" if c["backups"] <= t else "safe"
            assert c["level"] == want, c["shift_id"]
    fragile = [c["shift_id"] for c in cells if c["level"] in ("at_risk", "fragile")]
    assert sorted(fragile) == sorted(w["fragile"]) and len(fragile) == w["counts"]["fragile_total"]
    assert w["fixes"]["standby"] and w["fixes"]["training"]["best"]["text"]


LOAN_STATES = {"candidate": ("awaiting_approval", 0), "one_approval": ("awaiting_approval", 1),
               "one_approval_lender": ("awaiting_approval", 1), "assigned": ("assigned", 2),
               "declined_site_short": ("declined", 0), "rejected_rules": ("rejected", 0)}


def test_loans_in_every_state():
    m = load("network_mocks.json")
    assert set(m["loan_files"]) == set(LOAN_STATES)
    for name, (status, approvals) in LOAN_STATES.items():
        loan = load(m["loan_files"][name])
        assert (loan["status"], loan["approvals"]) == (status, approvals), name
        stamps = [s["at"] for s in loan["steps"]]
        assert all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", a) for a in stamps), name
        assert [s["seq"] for s in loan["steps"]] == list(range(1, len(loan["steps"]) + 1)), name
    one = load(m["loan_files"]["one_approval"])
    assert one["borrower"]["approved"] and not one["lender"]["approved"]
    lender_first = load(m["loan_files"]["one_approval_lender"])
    assert lender_first["lender"]["approved"] and not lender_first["borrower"]["approved"]
    done = load(m["loan_files"]["assigned"])
    assert done["candidate"]["name"] and done["case"]["ref"] and done["lender"]["approved"] and done["borrower"]["approved"]
    short = load(m["loan_files"]["declined_site_short"])
    assert any(s["kind"] == "declined" and s["side"] == "lender" for s in short["steps"])
    rejected = load(m["loan_files"]["rejected_rules"])
    assert any(s["kind"] == "checked_offer" and s["side"] == "borrower" for s in rejected["steps"])


def test_loan_answers_and_the_judges_scene():
    m = load("network_mocks.json")
    a = m["approve"]
    assert a["first"]["approvals"] == 1 and a["second"]["status"] == "assigned"
    assert "already approved" in a["repeat"]["error"] and "assigned" in a["closed"]["error"] and "declined" in a["declined"]["error"]
    assert "can approve" in a["forbidden"]["error"]
    assert m["ask"]["created"]["status"] == "awaiting_approval" and m["ask"]["conflict"]["error"]
    run = m["judge_run"]
    assert run["scenario"] == "network_loan" and run["loan_status"] == "assigned"
    t = load("today.network_assigned.json")
    loan = load(m["loan_files"]["assigned"])
    assert t["outcome"]["covered_by"] == loan["candidate"]["name"] and t["outcome"]["covered_via"] == "network_loan"
    assert load(f"cases/{run['case_id']}.network_loan.json")["case_id"] == run["case_id"]
    members = load("network.json")["members"]
    assert {mm["manager"] for mm in members} >= {loan["borrower"]["manager"], loan["lender"]["manager"]}


def test_standby_answers():
    w, sb = load("week.json"), load("week_mocks.json")["standby"]
    pick = w["fixes"]["standby"][0]
    assert re.fullmatch(r"SB-\d+", sb["created"]["id"]) and sb["created"]["status"] == "proposed"
    assert (sb["created"]["shift_id"], sb["created"]["staff_id"]) == (pick["shift_id"], pick["staff_id"])
    assert "already proposed" in sb["repeat"]["error"]
    assert "not cleared" in sb["holder"]["error"] and "not cleared" in sb["not_cleared"]["error"]
    assert sb["unknown"]["error"]


def test_no_short_codes_of_programs_or_levels_on_any_screen():
    """Programs and levels by name ("Learn-to-Swim Level 2"), never the preset's short code ("LTS-2")."""
    hits = [f"{p.relative_to(FX)}: {m.group(0)}" for p in sorted(FX.rglob("*.json"))
            for m in re.finditer(r".{0,30}\bLTS\b.{0,20}", p.read_text(encoding="utf-8"))]
    assert hits == []
    assert re.search(r"\bLTS\b", "Not cleared for LTS-2")  # the check can see a code


def test_the_familys_page_follows_its_answers():
    """The family's page: the open question, then each real answer of the API; the parent is never named and no id
    shows in the texts."""
    asked, covered, no, second = (load(f"clients/{n}.json") for n in ("Q-1", "Q-1.covered", "Q-1.no", "Q-2"))
    for name in ("Q-1", "Q-1.covered", "Q-1.no", "Q-1.moved", "Q-1.credit", "Q-2", "Q-2.moved", "Q-2.credit"):
        d = load(f"clients/{name}.json")
        assert set(d) == CLIENT_KEYS, name
        assert d["signed"] == f"Parent of {d['child']}", name
        texts = [d["question"], *(m["text"] for m in d["thread"]), (d["outcome"] or {}).get("line") or ""]
        assert not any(re.search(r"\b(Q|O|S)-?\d", t) for t in texts), (name, texts)
    assert (asked["state"], asked["kind"], asked["current"]) == ("asked", "cover", "Q-1") and asked["answer_in_s"] > 0
    assert [m["from"] for m in asked["thread"]] == ["school"]
    assert covered["state"] == "yes" and covered["outcome"]["kind"] == "covered" and covered["current"] is None
    assert no["state"] == "no" and no["current"] == "Q-2" and second["kind"] == "reschedule" and second["state"] == "asked"
    assert load("clients/Q-2.moved.json")["outcome"]["kind"] == "moved"
    # once the case is decided the headline says what happened; while a question is open it asks
    assert asked["question"].endswith("?") and second["question"].endswith("?")
    for name in ("Q-1.covered", "Q-1.moved", "Q-1.credit", "Q-2.moved", "Q-2.credit"):
        assert not load(f"clients/{name}.json")["question"].endswith("?"), name
    assert load("clients/Q-2.moved.json")["question"] == "Lily's lesson is moved to Wed, Nov 11"
    assert load("clients/Q-1.covered.json")["question"] == "Chloe will teach Lily today"
    assert load("clients/Q-2.credit.json")["outcome"]["line"] == "A credit for one lesson ($50)"
    assert load("clients/mocks.json")["closed"]["body"]["reason"].startswith("Thanks — this question was already closed")
