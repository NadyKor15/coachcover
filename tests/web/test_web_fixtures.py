"""Fixtures of web/ are answers of the running API (tests/web/make_fixtures.py) and follow the API contract
(docs/API-CONTRACT-2026-11-06.md, v1, plus the fields listed in EXTENSIONS). The checks are relations between
answers, not copies of their values, so a new capture of the same story passes and a broken story fails.

Standard library only, so it runs under the project's pytest and on a bare python3.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
EXTENSIONS = {"today": {"flagged", "night_check", "ratio_alert", "judge"}, "callout": {"asr_label"},
              "offer": {"state", "staff", "needs_label", "why_first"}}
RATIO_KEYS = {"case_id", "status", "tone", "label", "action_label"}
OFFER_KEYS = {"offer_id", "shift_day", "shift_start", "shift_end", "venue", "shift_detail", "why", "expires_in_s", "window_s", "manager"}
CASE_KEYS = {"case_id", "status", "status_label", "covered_time_label", "shift_label", "venue", "date_label", "user_label",
             "opened_at", "closed_at", "steps", "proof", "model_calls", "model_cost_usd", "risk"}
CALL_KEYS = {"at", "purpose", "model", "model_label", "ms", "input_tokens", "output_tokens", "cost_usd", "source"}
UTC_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT(\d\d:\d\d:\d\d)Z$")


def load(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


def today_files() -> list[str]:
    return sorted(p.name for p in FX.glob("today*.json"))


def hours(text: str | None) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?) h this week", text or "")
    return m.group(1) if m else None


def test_fixture_set_is_complete():
    assert today_files() == ["today.json", "today.nightly_check.json", "today.nobody_accepts.json",
                             "today.ratio_covered.json", "today.ratio_open.json"]
    m = load("mocks.json")
    assert set(m["judge_run"]) == {"dangerous_morning", "nobody_accepts", "nightly_check"}
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
        assert all({"id", "time_label", "staff", "result", "why_first"} == set(o) for o in d["offers"]), name
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
    case = load(f"cases/{load('today.json')['callout']['case_id']}.json")
    assert "founder" in case["risk"]["assumptions"]["lesson_price"]["source"]


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
        m = UTC_STAMP.match(s["at"])
        assert m and s["time_label"] == m.group(1) + " UTC", s
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


def test_model_calls_are_recorded_answers():
    t = load("today.json")
    for c in load(f"cases/{t['callout']['case_id']}.json")["model_calls"]:
        assert c["source"].startswith("recorded answer tests/fixtures/models/"), c["source"]
    assert "replayed from recorded" in t["model_log"]["label"]


def test_mocks_match_the_fixtures():
    m = load("mocks.json")
    nobody = load("today.nobody_accepts.json")
    plan = m["plans"][nobody["plan_b"]["id"]]
    assert nobody["plan_b"]["state"] == "proposed" and plan["current_hash"] == nobody["plan_b"]["hash"]
    assert plan["conflict"]["reason"] == "plan changed" and plan["approve"]["state"] == "approved"
    for oid, o in m["offers"].items():
        assert (FX / "offers" / f"{oid}.json").is_file(), oid
        assert o["accept"]["state"] == "accepted" and o["decline"]["state"] == "declined" and o["accept_conflict"]["reason"]


VENDOR_WORDS = re.compile(r"\bvendor\b|\bAI\b|\bLLM\b|chatgpt|openai", re.IGNORECASE)


def web_text_files() -> list[Path]:
    files = [p for p in WEB.rglob("*") if p.suffix in {".html", ".js", ".css", ".json", ".md"}]
    # this file holds the guard's own samples, so it is the one file not scanned
    files += [p for p in (ROOT / "tests" / "web").rglob("*") if p.suffix in {".py", ".js"} and p.resolve() != Path(__file__).resolve()]
    return sorted(files)


def test_no_vendor_words_in_web():
    hits = []
    for p in web_text_files():
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if VENDOR_WORDS.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{n}")
    assert hits == []


def test_vendor_word_guard_is_not_blind():
    # must-light: synthetic samples the guard has to catch
    for sample in ("made with a vendor tool", "VENDOR", "an AI helper", "LLM call"):
        assert VENDOR_WORDS.search(sample), sample
    assert not VENDOR_WORDS.search("Riverside Aquatic Center · whisper on Vultr")
    assert len(web_text_files()) >= 10


def test_pages_declare_charset_first():
    for page in ("index.html", "offer.html", "case.html"):
        head = (WEB / page).read_text(encoding="utf-8").split("<head>", 1)[1].lstrip()
        assert head.startswith('<meta charset="UTF-8">'), page
