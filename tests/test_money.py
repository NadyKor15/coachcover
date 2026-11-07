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
        ("A", 60.0), ("D", 153.0), ("B", 75.0)]  # 4 x $15; 4.5 h x $34; 5 x $15, in start order
    assert cost["total_usd"] == 288.0
    fined = uncovered_cost([six], money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour, inspection_penalty=500.0))
    assert fined["total_usd"] == 560.0 and fined["lines"][-1]["kind"] == "inspection"
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
                         "manager_hour"}
    assert all({"value", "unit", "assumption", "source", "note"} <= set(v) for v in info.values())
    assert info["lesson_price"]["assumption"] and info["manager_hour"]["assumption"] and info["hourly_wage"]["assumption"]
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
    assert usd(105) == "$105" and usd(2.5) == "$2.50" and usd(1234.5) == "$1,234.50"
    assert cents_label(0.0138) == "0.014¢" and cents_label(0.0004) == "0.0004¢" and cents_label(1.234) == "1.23¢"


def test_card_adds_lines_by_kind_and_compares_with_models_and_the_manager():
    params = money_params(SWIM.preset.money, SWIM.overtime_cost_per_hour)
    shifts = [lesson("A", groups=(group("G1", "3", 4),)), lesson("B", groups=(group("G2", "1", 3),)),
              guard_shift("D", hours=2)]
    bill = {"cents": 0.0138, "calls": 2, "own_calls": 0, "shared_calls": 2, "shared_between_cases": 2}
    card = nobody_accepted_card(uncovered_cost(shifts, params), params, case_bill=bill, manager_minutes=6.0,
                                manager_minutes_source="synthetic replay")
    assert card["title"] == "If nobody had accepted" and card["total_usd"] == 173.0 and card["total_label"] == "$173"
    assert [(x["kind"], x["usd"], x["label"]) for x in card["lines"]] == [
        ("refunds", 105.0, "Refunds: 7 participants × $15"),
        ("overtime", 68.0, "Overtime: 2 h × $22.67 × 1.5"),
    ]
    models, manager = card["compare"]
    assert models["label"] == "Models for this cover: 0.014¢ (1/2 of 2 call-out call(s))"
    assert manager["usd"] == 2.5 and "6 min" in manager["label"] and "synthetic replay" in manager["label"]
    assert card["assumptions"]["lesson_price"]["assumption"] is True


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
    assert money["uncovered_morning_usd"] == 120.0 and out["uncovered"]["participants"] == 8
    assert money["uncovered_morning_label"] == "If nobody had accepted: $120 (Refunds: 8 participants × $15)"
    assert money["manager_hour_usd"] == 25.0
    # one call-out, one case: all four call-out calls are this case's, plus its own (Anna's message filter)
    assert out["case_cost"]["shared_calls"] == 4 and out["case_cost"]["shared_between_cases"] == 1
    assert out["case_cost"]["own_calls"] == 1
    total = float(conn.execute("SELECT sum(cost_usd) FROM model_call").fetchone()[0])
    assert case_cost(conn, case)["usd"] == pytest.approx(total, abs=2e-6)  # nothing lost, nothing counted twice
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

    assert SWIM.preset.money["lesson_price"]["value"] == 15.0 and SWIM.preset.money["lesson_price"]["assumption"] is True
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
