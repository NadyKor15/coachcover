"""Fixtures of web/ follow the API contract (docs/API-CONTRACT-2026-11-06.md, v1) and tell one consistent story.

Standard library only, so it runs under the project's pytest and on a bare python3.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
FX = WEB / "fixtures"

# Field sets of contract v1. EXTENSIONS lists what the screens need on top of v1 (see web/README.md).
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
EXTENSIONS = {"callout": {"asr_label"}}
REASON_KEYS = {"code", "label", "count", "tone"}
STAFF_KEYS = {"id", "name", "eligible", "highlight", "reason_label"}
REFUSAL_KEYS = {"staff", "label", "contrast_quote"}
TIMELINE_KEYS = {"time_label", "kind", "label"}
OFFER_ROW_KEYS = {"id", "time_label", "staff", "result", "why_first"}
CARD_KEYS = {"title", "value"}
OFFER_KEYS = {"offer_id", "shift_day", "shift_start", "shift_end", "venue", "shift_detail", "why", "expires_in_s", "window_s", "manager"}
CONTRACT_NAMES = {"Marina Petrova", "Maria Lopez", "Anna Kim", "Sofia Reyes", "Leo Martins", "Dana Ruiz"}


def load(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


def today_files() -> list[str]:
    return sorted(p.name for p in FX.glob("today*.json"))


def test_there_are_fixtures():
    assert today_files() == ["today.json", "today.nobody_accepts.json"]
    assert (FX / "offers" / "O-1.json").is_file() and (FX / "mocks.json").is_file()


def test_today_fields_follow_the_contract():
    for name in today_files():
        d = load(name)
        assert set(d) == TODAY_KEYS, name
        for key, fields in NESTED.items():
            assert set(d[key]) == fields | EXTENSIONS.get(key, set()), (name, key)
        e = d["eligibility"]
        assert all(set(r) == REASON_KEYS for r in e["reasons"]), name
        assert all(set(s) == STAFF_KEYS for s in e["staff"]), name
        assert set(e["first_refusal"]) == REFUSAL_KEYS, name
        assert all(set(t) <= TIMELINE_KEYS and {"time_label", "kind"} <= set(t) for t in d["timeline"]), name
        assert all(set(o) == OFFER_ROW_KEYS for o in d["offers"]), name
        assert all(set(c) == CARD_KEYS for c in d["summary_cards"]), name


def test_offer_fields_follow_the_contract():
    o = load("offers/O-1.json")
    assert set(o) == OFFER_KEYS
    assert set(o["manager"]) == {"name", "role"}
    assert 0 < o["expires_in_s"] <= o["window_s"]


def test_eligibility_adds_up():
    for name in today_files():
        e = load(name)["eligibility"]
        staff = e["staff"]
        assert len(staff) == e["total"] == 40, name
        assert sum(s["eligible"] for s in staff) == e["eligible"] == 6, name
        assert sum(r["count"] for r in e["reasons"]) == e["total"] - e["eligible"], name
        assert len({s["id"] for s in staff}) == len(staff), name
        assert all(s["reason_label"] for s in staff if not s["eligible"]), name
        assert all(s["reason_label"] is None for s in staff if s["eligible"]), name
        for r in e["reasons"]:
            assert sum(1 for s in staff if s["reason_label"] == r["label"]) == r["count"], (name, r["label"])


def test_anna_kim_is_the_first_refusal_and_never_gets_an_offer():
    for name in today_files():
        d = load(name)
        anna = [s for s in d["eligibility"]["staff"] if s["name"] == "Anna Kim"]
        assert len(anna) == 1 and not anna[0]["eligible"], name
        assert anna[0]["reason_label"] == "Certificate expired Nov 5", name
        fr = d["eligibility"]["first_refusal"]
        assert fr["staff"] == "Anna Kim" and "BLOCKED" in fr["label"] and "Nov 5" in fr["label"], name
        assert "RosterElf" in fr["contrast_quote"], name
        assert all(o["staff"] != "Anna Kim" for o in d["offers"]), name
        assert all("Anna Kim" not in (t.get("label") or "") for t in d["timeline"]), name


def test_offers_go_to_eligible_people_only():
    for name in today_files():
        d = load(name)
        eligible = {s["name"] for s in d["eligibility"]["staff"] if s["eligible"] and s["name"]}
        assert {o["staff"] for o in d["offers"]} <= eligible, name


def test_morning_story_and_one_highlight():
    d = load("today.json")
    assert [(t["time_label"], t["kind"]) for t in d["timeline"]] == [
        ("05:12", "callout"), ("05:13", "declined"), ("05:15", "no_reply"), ("05:19", "accepted"), ("06:00", "shift_start")]
    assert [(o["staff"], o["result"]) for o in d["offers"]] == [
        ("Sofia Reyes", "declined"), ("Leo Martins", "no_reply"), ("Maria Lopez", "accepted")]
    hl = [s for s in d["eligibility"]["staff"] if s["highlight"]]
    assert len(hl) == 1 and hl[0]["name"] == "Maria Lopez" and hl[0]["eligible"]
    assert d["outcome"]["covered_by"] == "Maria Lopez"
    assert d["plan_b"]["state"] == "standby"
    assert d["title"] == "Who is allowed on deck at 06:00"


def hours(text: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?) h this week", text)
    return m.group(1) if m else None


def test_maria_hours_are_the_same_on_both_screens():
    accepted = [o for o in load("today.json")["offers"] if o["result"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["id"] == "O-1"
    offer = load("offers/O-1.json")
    assert hours(accepted[0]["why_first"]) == hours(" ".join(offer["why"])) == "12"


def test_money_is_never_made_up():
    for name in today_files():
        d = load(name)
        m = d["money"]
        # only the case cost is known in the training school: the model log of the run
        assert math.isclose(m["case_cost_cents"], d["model_log"]["usd"] * 100, rel_tol=1e-9), name
        assert m["uncovered_morning_usd"] is None and m["uncovered_morning_label"] is None and m["manager_hour_usd"] is None, name


def test_mocks_match_the_fixtures():
    mocks = load("mocks.json")
    assert set(mocks["judge_run"]) == {"dangerous_morning", "nobody_accepts", "nightly_check"}
    assert all(set(v) == {"case_id"} for v in mocks["judge_run"].values())
    for scenario, file in mocks["today_by_scenario"].items():
        assert scenario in mocks["judge_run"] and (FX / file).is_file()
    nobody = load("today.nobody_accepts.json")
    plan = mocks["plans"][nobody["plan_b"]["id"]]
    assert plan["current_hash"] == nobody["plan_b"]["hash"]
    assert plan["conflict"]["reason"] == "plan changed" and plan["conflict"]["current_hash"] != plan["current_hash"]
    assert nobody["plan_b"]["state"] == "proposed"
    o = mocks["offers"]["O-1"]
    assert o["accept"] == {"state": "accepted"} and o["decline"] == {"state": "declined"}
    assert o["accept_conflict"]["reason"]


def test_people_are_the_fictional_names_of_the_contract():
    for name in today_files():
        d = load(name)
        names = {s["name"] for s in d["eligibility"]["staff"] if s["name"]}
        names |= {o["staff"] for o in d["offers"]}
        names |= {d["callout"]["staff"].split(" · ")[0], d["user_label"].split(" · ")[0]}
        assert names <= CONTRACT_NAMES, (name, names - CONTRACT_NAMES)
    assert load("offers/O-1.json")["manager"]["name"] in CONTRACT_NAMES


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
