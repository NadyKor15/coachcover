"""Hours of a week read as hours and minutes wherever a person sees them: "Chloe first: 5 h 45 min this week", never
"5.75 h" (why first on Today and the case page, the coach's phone, MCP, the Week screen, reports); whole hours stay
"6 h"."""

import re
from datetime import date

import pytest

from coachcover import seed
from coachcover.demo import SCENARIOS, run_scenario
from coachcover.mcp_server import allowed
from coachcover.models import DbLedger, ModelClient
from coachcover.reasons_text import describe, hours_words
from coachcover.rules import Code, Reason
from coachcover.presets import default_policy
from coachcover.screens import offer_phone, reason_label, today
from tests.test_network_loan_scenario import api  # noqa: F401  (the API with both agents in-process)

DECIMAL_HOURS = re.compile(r"\b\d+\.\d+ ?(h|ч)\b")


def test_hours_words():
    assert [hours_words(x) for x in (6, 6.0, 5.75, 1.75, 0.5, 0, 41.25, 40)] == \
        ["6 h", "6 h", "5 h 45 min", "1 h 45 min", "30 min", "0 h", "41 h 15 min", "40 h"]
    assert [hours_words(x, "ru") for x in (6, 5.75, 0.25)] == ["6 ч", "5 ч 45 мин", "15 мин"]
    assert hours_words(40 + 10 / 3600, rounding="up") == "40 h 1 min" and hours_words(40 + 10 / 3600) == "40 h"
    assert hours_words(40.5, rounding="up") == "40 h 30 min" and hours_words(40, rounding="up") == "40 h"
    assert hours_words(9.99, rounding="down") == "9 h 59 min" and hours_words(9.99) == "9 h 59 min"
    assert hours_words(9 + 59.9 / 60, rounding="down") == "9 h 59 min" and hours_words(9 + 59.9 / 60) == "10 h"
    assert DECIMAL_HOURS.search("Chloe first: 5.75 h this week") and DECIMAL_HOURS.search("5.75 ч")  # the sweep can see it


def test_the_hours_limit_reason_reads_in_hours_and_minutes():
    reason = Reason(Code.HOURS_LIMIT_EXCEEDED, {"hours_after": 41.25, "limit_hours": 40.0, "week_start": "2026-11-09"})
    assert describe(reason, "en") == "would have 41 h 15 min in the week of 2026-11-09 (limit 40 h)"
    assert describe(reason, "ru") == "будет 41 ч 15 мин за неделю с 09.11.2026 (лимит 40 ч)"


@pytest.mark.db
def test_chloes_why_first_phone_and_mcp_say_5_h_45_min(new_database):
    """Live, 2026-11-07: "Chloe first: 5.75 h this week, Level 2, 21.7 km away" on Today of the private scene."""
    conn = new_database()
    result = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "private_lesson")
    screen = today(conn, seed.POLICY_ID, result["ended_at"], None, live=False)
    chloe = next(o for o in screen["offers"] if o["staff"] == "Chloe Hayes")
    assert chloe["why_first"] == "Chloe first: 5 h 45 min this week, Level 2, 21.7 km away"
    phone = offer_phone(conn, seed.POLICY_ID, chloe["id"], result["ended_at"])
    assert "5 h 45 min this week" in phone["why"]
    whole = [o["why_first"] for o in screen["offers"] if o["staff"] == "Sofia Reyes"]
    assert whole == ["Sofia third: 6 h this week, Level 2, same pool"]  # whole hours stay as they are
    lines = [p["why_first"] for p in allowed(conn, seed.POLICY_ID, seed.HERO_SHIFT)["allowed"]]  # MCP who_is_allowed
    assert lines and not any(DECIMAL_HOURS.search(x) for x in lines), lines


@pytest.mark.db
def test_no_decimal_hours_in_any_text_a_person_reads(api):  # noqa: F811
    """After every judge scene: Today, the case page, every offer's phone, the family's page, the Week screen (its
    fixes), the network loan, the inspector's binder, the team posts. Machine numbers (week_hours) stay numbers."""
    client, conn = api
    seen, checked = [], 0

    def walk(where, value):
        nonlocal checked
        if isinstance(value, dict):
            for v in value.values():
                walk(where, v)
        elif isinstance(value, list):
            for v in value:
                walk(where, v)
        elif isinstance(value, str):
            checked += 1
            if DECIMAL_HOURS.search(value):
                seen.append((where, value))

    for scenario in SCENARIOS:
        run = client.post("/api/judge/run", json={"scenario": scenario}).json()
        now = client.get("/api/today").json()
        pages = {"today": "/api/today", "week": "/api/week?venue=V1", "team": "/api/team-posts",
                 "binder": f"/api/binder.json?venue=V1&from={date(2026, 11, 9)}&to={date(2026, 11, 9)}"}
        if run.get("case_id"):
            pages["case"] = f"/api/cases/{run['case_id']}"
        if run.get("loan"):
            pages["loan"] = f"/api/network/loans/{run['loan']}"
        for o in now.get("offers", []):
            pages[f"offer {o['id']}"] = f"/api/offers/{o['id']}"
        if scenario.startswith("private"):
            pages["family"] = "/api/client/Q-1"
        for label, url in pages.items():
            res = client.get(url)
            assert res.status_code == 200, (scenario, url, res.status_code)
            walk(f"{scenario} {label}", res.json())
    walk("mcp who_is_allowed", allowed(conn, seed.POLICY_ID, seed.HERO_PRIVATE))
    assert seen == [] and checked > 1000, (seen[:5], checked)


def test_rest_reads_in_hours_and_minutes_and_never_reaches_the_minimum():
    """Rest between days: "Only 9 h 30 min rest since 21:00" on the chip, "only 9 h 30 min of rest since 21:00 (needs
    10 h)" in reports; rest short of the minimum by seconds still reads short of it."""
    from zoneinfo import ZoneInfo

    from coachcover.rules import _hours_down

    preset, ny = default_policy("swim_school").preset, ZoneInfo("America/New_York")
    half = {"side": "before", "rest_hours": 9.5, "min_rest_hours": 10, "since": "2026-11-08T21:00:00-05:00"}
    assert reason_label("REST_TOO_SHORT", half, preset, None, ny) == "Only 9 h 30 min rest since 21:00"
    after = {**half, "side": "after", "until": "2026-11-10T06:00:00-05:00"}
    assert reason_label("REST_TOO_SHORT", after, preset, None, ny) == "Only 9 h 30 min rest before 6:00"
    assert describe(Reason(Code.REST_TOO_SHORT, half), "en") == "only 9 h 30 min of rest since 21:00 (needs 10 h)"
    assert describe(Reason(Code.REST_TOO_SHORT, half), "ru") == "отдых только 9 ч 30 мин после 21:00 (нужно 10 ч)"
    assert describe(Reason(Code.REST_TOO_SHORT, after), "en") == "only 9 h 30 min of rest before 06:00 (needs 10 h)"
    almost = {**half, "rest_hours": _hours_down(10 * 3600 - 10)}  # ten seconds short of 10 h: still blocked
    assert almost["rest_hours"] < 10
    assert reason_label("REST_TOO_SHORT", almost, preset, None, ny) == "Only 9 h 59 min rest since 21:00"
    assert describe(Reason(Code.REST_TOO_SHORT, almost), "en").startswith("only 9 h 59 min of rest")
    assert not DECIMAL_HOURS.search(reason_label("REST_TOO_SHORT", half, preset, None, ny))
