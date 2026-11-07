"""Money: the cost of an uncovered morning from preset parameters, and the cost of one cover from the model ledger."""

from datetime import timedelta

import pytest

from coachcover.money import (
    calls_cost,
    case_cost,
    cents_label,
    money_params,
    nobody_accepted_card,
    today_money,
    uncovered_cost,
    usd,
)
from coachcover.models import call_cost, load_prices
from coachcover.model import Shift
from coachcover.presets import default_policy
from tests.factory import group, guard_shift, lesson, local

SWIM = default_policy("swim_school")
CONTRACT_MONEY_KEYS = {"uncovered_morning_usd", "uncovered_morning_label", "case_cost_cents", "manager_hour_usd"}


def test_uncovered_morning_refunds_overtime_and_optional_inspection():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    six = lesson("A", groups=(group("G1", "3", 4),))
    seven = lesson("B", start=local(2026, 11, 9, 7, 0), groups=(group("G4", "1", 3), group("G5", "1", 2)))
    deck = guard_shift("D", hours=4.5)
    cost = uncovered_cost([six, seven, deck], params)
    assert cost["participants"] == 9 and cost["overtime_hours"] == 4.5
    assert [(line["shift_id"], line["usd"]) for line in cost["lines"]] == [
        ("A", 120.0), ("D", 153.0), ("B", 150.0)]  # 4 x $30; 4.5 h x $34; 5 x $30, in start order
    assert cost["total_usd"] == 423.0
    fined = uncovered_cost([six], money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour, inspection_penalty=500.0))
    assert fined["total_usd"] == 620.0 and fined["lines"][-1]["kind"] == "inspection"
    assert uncovered_cost([], params)["total_usd"] == 0  # no shift, no fine
    with pytest.raises(ValueError, match="twice"):
        uncovered_cost([six, six], params)


def test_overtime_rate_is_the_policy_price_so_both_cards_agree():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    assert params.overtime_hour_usd == SWIM.overtime_cost_per_hour == 34.0
    assert round(params.hourly_wage.value, 2) == 22.67 and params.overtime_multiplier.value == 1.5


def test_hours_across_the_dst_change_are_real_hours():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    night = Shift("N", "A", "lifeguard", local(2026, 11, 1, 0, 0), local(2026, 11, 1, 3, 0))  # 00:00 EDT -> 03:00 EST
    assert uncovered_cost([night], params)["overtime_hours"] == 4.0


def test_every_price_says_whether_it_is_an_assumption():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    info = params.as_dict()
    assert set(info) == {"lesson_price", "refund_share", "hourly_wage", "overtime_multiplier", "inspection_penalty",
                         "manager_hour", "private_lesson_price"}
    assert not info["private_lesson_price"]["assumption"] and info["private_lesson_price"]["value"] == 50.0
    assert all({"value", "unit", "assumption", "source", "note"} <= set(v) for v in info.values())
    assert not info["lesson_price"]["assumption"] and info["lesson_price"]["source"] == "from the founder's coaching experience"
    assert info["manager_hour"]["assumption"] and info["hourly_wage"]["assumption"]
    assert info["inspection_penalty"]["value"] == 0  # off unless a school sets it
    assert info["overtime_multiplier"]["assumption"] is False and "Fair Labor Standards" in info["overtime_multiplier"]["source"]
    own = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour, lesson_price=28.0).as_dict()["lesson_price"]
    assert own == {**own, "value": 28.0, "assumption": False, "source": "set by the school"}


def test_bad_parameters_fail_loudly():
    with pytest.raises(ValueError, match="unknown money parameters"):
        money_params(SWIM.preset.money, lesson_cost=1.0)
    with pytest.raises(ValueError, match="refund_share"):
        money_params(SWIM.preset.money, refund_share=1.5)
    with pytest.raises(ValueError, match=">= 0"):
        money_params(SWIM.preset.money, lesson_price=-1.0)


def test_calls_cost_recomputes_tokens_and_uses_the_ledger_for_audio():
    prices = load_prices()
    calls = [
        {"model_requested": "muse-glimmer-30b", "model_returned": "muse-glimmer-30b", "input_tokens": 445,
         "output_tokens": 70, "cost_usd": 999.0, "source": "recorded"},  # a wrong stored cost must not leak in
        {"model_requested": "whisper-large-v3-turbo", "model_returned": "whisper-large-v3-turbo", "cost_usd": 0.000095,
         "source": "recorded"},
        {"model_requested": "unknown-model", "model_returned": None, "source": "live", "ok": False},
    ]
    bill = calls_cost(calls, prices)
    expected = call_cost(prices, "muse-glimmer-30b", 445, 70, None) + 0.000095
    assert bill["usd"] == pytest.approx(expected) and bill["cents"] == pytest.approx(expected * 100)
    assert bill["calls"] == 3 and bill["unpriced"] == 1 and bill["recorded"] == 2
    assert bill["per_model"]["muse-glimmer-30b"]["usd"] == pytest.approx(0.000181)
    half = calls_cost(calls, prices, weight=0.5)
    assert half["usd"] == pytest.approx(expected / 2)


def test_labels():
    assert usd(210) == "$210" and usd(2.5) == "$2.50" and usd(1234.5) == "$1,234.50"
    assert cents_label(0.0138) == "0.014¢" and cents_label(0.0004) == "0.0004¢" and cents_label(1.234) == "1.23¢"


def test_card_adds_lines_by_kind_and_compares_with_models_and_the_manager():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    shifts = [lesson("A", groups=(group("G1", "3", 4),)), lesson("B", groups=(group("G2", "1", 3),)),
              guard_shift("D", hours=2)]
    bill = {"cents": 0.0138, "calls": 2, "own_calls": 0, "shared_calls": 2, "shared_between_cases": 2}
    card = nobody_accepted_card(uncovered_cost(shifts, params), params, case_bill=bill, manager_minutes=6.0,
                                manager_minutes_source="synthetic replay")
    assert card["title"] == "If nobody had accepted" and card["total_usd"] == 278.0 and card["total_label"] == "$278"
    assert [(x["kind"], x["usd"], x["label"]) for x in card["lines"]] == [
        ("refunds", 210.0, "Refunds: 7 participants × $30"),
        ("overtime", 68.0, "Overtime: 2 h × $22.67 × 1.5"),
    ]
    models, manager = card["compare"]
    assert models["label"] == "Models for this cover: 0.014¢ (1/2 of 2 call-out call(s))"
    assert manager["usd"] == 2.5 and "6 min" in manager["label"] and "synthetic replay" in manager["label"]
    assert card["assumptions"]["lesson_price"]["assumption"] is False


# --- from the database: one judge morning on recordings -------------------------------------


@pytest.mark.db
def test_today_money_after_the_judge_morning(new_database):
    from coachcover.demo import reset_school, run_scenario
    from coachcover.models import DbLedger, ModelClient
    from coachcover.seed import POLICY_ID

    conn = new_database()
    reset_school(conn)
    run = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "dangerous_morning")
    case = conn.execute("SELECT id FROM cover_case WHERE code = %s", (run["case_id"],)).fetchone()[0]
    out = today_money(conn, POLICY_ID, run["ended_at"], manager_minutes=6.0)
    money = out["money"]
    assert set(money) == CONTRACT_MONEY_KEYS
    # Marina's shift: 8 children (seed FIXED_GROUPS), refunds at the preset's lesson price
    assert money["uncovered_morning_usd"] == 240.0 and out["uncovered"]["participants"] == 8
    assert money["uncovered_morning_label"] == "If nobody had accepted: $240 (Refunds: 8 participants × $30)"
    assert money["manager_hour_usd"] == 25.0
    # one call-out, two cases (Marina is out all day: the 09:30 lesson and her 17:00 private lesson): the four
    # call-out calls are shared between them; plus the case's own call (Anna's message filter)
    assert out["case_cost"]["shared_calls"] == 4 and out["case_cost"]["shared_between_cases"] == 2
    assert out["case_cost"]["own_calls"] == 1
    total = float(conn.execute("SELECT sum(cost_usd) FROM model_call").fetchone()[0])
    cases = [c for (c,) in conn.execute("SELECT id FROM cover_case")]
    assert len(cases) == 2  # nothing lost, nothing counted twice
    assert sum(case_cost(conn, c)["usd"] for c in cases) == pytest.approx(total, abs=2e-6)
    assert 0 < money["case_cost_cents"] < 1  # a fraction of a cent
    # before the call-out nothing is due
    quiet = today_money(conn, POLICY_ID, run["ended_at"] - timedelta(minutes=30))
    assert quiet["money"] == {"uncovered_morning_usd": 0, "uncovered_morning_label": "No call-outs today",
                              "case_cost_cents": None, "manager_hour_usd": 25.0}
    with pytest.raises(ValueError, match="unknown case"):
        case_cost(conn, 999999)


def test_prices_come_from_the_preset_and_the_loader_is_strict():
    import copy

    from coachcover.presets import preset_from_dict, read_preset_file

    assert SWIM.preset.money["lesson_price"]["value"] == 30.0 and SWIM.preset.money["lesson_price"]["assumption"] is False
    assert default_policy("fitness_club").preset.money["lesson_price"]["unit"] == "USD per participant per class"
    broken = {
        "missing price": lambda d: d["money"].pop("manager_hour"),
        "assumption": lambda d: d["money"]["lesson_price"].update(assumption="yes"),
        "needs a source": lambda d: d["money"]["overtime_multiplier"].pop("source"),
        "between 0 and 1": lambda d: d["money"]["refund_share"].update(value=2),
        "value must be a number": lambda d: d["money"]["lesson_price"].update(value=-1),
    }
    for message, change in broken.items():
        data = copy.deepcopy(read_preset_file("swim_school"))
        change(data)
        with pytest.raises(ValueError, match="missing|" + message):
            preset_from_dict(data)
    with pytest.raises(ValueError, match="no money block"):
        money_params({})


def test_one_cost_of_a_cover_on_today_the_case_and_the_downloaded_case(new_database):
    """The voice note's calls are made once for Marina's message, which opens two cases (09:30 and her 17:00 private
    lesson): each case pays its own calls and half of the call-out's. Today's card and footer, the case page, its
    table of calls and the downloaded case show that one number; the two cases add up to the whole ledger."""
    from coachcover.demo import reset_school, run_scenario
    from coachcover.models import DbLedger, ModelClient
    from coachcover.screens import case_page, today
    from coachcover.seed import POLICY_ID

    conn = new_database()
    reset_school(conn)
    run = run_scenario(conn, ModelClient(DbLedger(conn), mode="replay"), "dangerous_morning")
    case = conn.execute("SELECT id FROM cover_case WHERE code = %s", (run["case_id"],)).fetchone()[0]
    screen = today(conn, POLICY_ID, run["ended_at"], None, live=False)
    page = case_page(conn, POLICY_ID, case, live=False)
    bill = case_cost(conn, case)
    assert bill["shared_between_cases"] == 2
    assert screen["money"]["case_cost_cents"] == pytest.approx(bill["cents"], abs=1e-4)
    assert screen["model_log"]["usd"] == pytest.approx(bill["usd"], abs=1e-6)
    assert page["model_cost_usd"] == pytest.approx(bill["usd"], abs=1e-6)
    assert sum(c["cost_usd"] or 0 for c in page["model_calls"]) == pytest.approx(bill["usd"], abs=1e-6)
    assert page["model_cost_label"] == screen["money"]["case_cost_label"]
    assert len(page["model_calls"]) == screen["model_log"]["calls"] == bill["calls"]
    assert sum(1 for c in page["model_calls"] if "shared by 2 cases" in c["source"]) == bill["shared_calls"] == 4
    total = float(conn.execute("SELECT sum(cost_usd) FROM model_call").fetchone()[0])
    cases = [c for (c,) in conn.execute("SELECT id FROM cover_case")]
    assert sum(case_cost(conn, c)["usd"] for c in cases) == pytest.approx(total, abs=2e-6)


def test_the_proof_table_cents_add_up_to_the_case_cost_label():
    """The case page's Proof rows show each call in cents with the total's own decimals; plain rounding of three equal
    thirds would show 0.003 + 0.003 + 0.003 = 0.009 under a total of 0.010¢. The rows add up to the label exactly."""
    from coachcover.money import cost_label, split_cents

    thirds = [0.0001 / 3] * 3  # 0.00333…¢ each
    assert cost_label(sum(thirds)) == "0.010¢"
    labels = split_cents(thirds, sum(thirds))
    assert sorted(labels) == ["0.003¢", "0.003¢", "0.004¢"]
    assert sum(round(float(x.rstrip("¢")) * 1000) for x in labels) == 10
    morning = [2.575e-05, 6.25e-06, 4.275e-05, 3.5e-05, 1.75e-05]  # the 09:30 case: the call-out's four halves + Anna's
    labels = split_cents(morning, sum(morning))
    assert sum(round(float(x.rstrip("¢")) * 1000) for x in labels) == round(float(cost_label(sum(morning)).rstrip("¢")) * 1000)
    assert split_cents([None, 0.0001], 0.0001) == ["no price", "0.010¢"]
    assert split_cents([0.004, 0.0071], 0.0111) == ["0.40¢", "0.71¢"]  # from a cent up: two decimals
