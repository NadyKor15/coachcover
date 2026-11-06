"""The 50-morning replay: deterministic, measured (not assumed) violations, the core's plan B order, the report."""

import json
from collections import Counter
from datetime import time, timedelta
from pathlib import Path

import pytest

from coachcover import sim
from coachcover.seed import NOW
from coachcover.sim import (
    BASELINE,
    COACHCOVER,
    MORNINGS,
    SEED,
    Callout,
    Morning,
    SimParams,
    State,
    _audit,
    load_world,
    make_morning,
    plan_b_options,
    report_markdown,
    run,
    stats_block,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "docs" / "sim" / "SIM-50-MORNINGS.json"
REPORT_MD = ROOT / "docs" / "sim" / "SIM-50-MORNINGS.md"


@pytest.fixture(scope="module")
def result():
    return run(MORNINGS, SEED)


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def test_same_seed_same_numbers_other_seed_other_mornings():
    assert run(4, SEED) == run(4, SEED)
    world = load_world()
    assert make_morning(world, 3, SEED) == make_morning(world, 3, SEED)
    assert run(4, SEED)["mornings_detail"] != run(4, SEED + 1)["mornings_detail"]


def test_committed_report_is_this_code_on_this_seed(result):
    """The table in docs/sim is a real run: regenerate with the command in the report if this fails."""
    assert json.loads(REPORT_JSON.read_text(encoding="utf-8")) == _plain(result)
    md = REPORT_MD.read_text(encoding="utf-8")
    assert md == report_markdown(_plain(result))
    assert "synthetic school, 50 mornings, seed 20261109" in md and result["command"] in md


def test_mornings_have_one_to_three_callouts_before_six_and_a_changed_staff_state():
    world = load_world()
    params = SimParams()
    kinds, days, counts = Counter(), Counter(), Counter()
    for i in range(MORNINGS):
        m = make_morning(world, i, SEED, params)
        days[m.day.weekday()] += 1
        counts[len(m.callouts)] += 1
        kinds.update(c["kind"] for c in m.changes)
        assert set(m.behavior) == set(world.staff)
        for c in m.callouts:
            local = c.at.astimezone(world.tz)
            assert local.date() == m.day and time(4, 30) <= local.time() <= time(5, 55)
            first = min(m.shifts[s].start for s in c.shift_ids)
            assert first - c.at >= timedelta(minutes=params.callout_lead_minutes)
            assert all(m.shifts[s].assignee == c.staff_id for s in c.shift_ids)
        assert len({c.staff_id for c in m.callouts}) == len(m.callouts)
    assert set(days) == {0, 1, 2, 3, 4} and set(counts) == {1, 2, 3}
    assert {"card", "hours", "rota", "renewal", "unconfirmed"} <= set(kinds)


def test_both_strategies_work_the_same_shifts_each_morning(result):
    for row in result["mornings_detail"]:
        wanted = sorted(s for c in row["callouts"] for s in c["shift_ids"])
        assert sorted(c["shift_id"] for c in row[BASELINE]) == wanted
        assert sorted(c["shift_id"] for c in row[COACHCOVER]) == wanted


def test_violations_are_measured_by_the_rules_for_both_strategies(result):
    b, c = result["summary"][BASELINE], result["summary"][COACHCOVER]
    assert b["cases"] == c["cases"] > 0
    assert c["placements"] > 0 and c["violations"] == 0  # every CoachCover placement passed the audit
    assert b["violations"] > 0
    for s in (b, c):
        assert sum(s["violations_by_reason"].values()) == s["violations"]  # each placement counted once
        assert s["violations"] <= s["placements"] <= s["cases"]
    detail = [case for row in result["mornings_detail"] for case in row[BASELINE] if case["violation"]]
    assert len(detail) == b["violations"] and all(case["codes"] for case in detail)


def test_the_audit_is_not_blind():
    """Must light: the same audit flags a known-bad placement on the unchanged Monday (S27's card expired Oct 20)."""
    world = load_world()
    shift = world.shifts["G-1109-V1-0530"]
    morning = Morning(0, SEED, NOW.date(), world.staff, world.shifts, {}, (), {}, ())
    state = State(world, morning)
    assert "CERT_EXPIRED" in _audit(state, "S27", shift, NOW)
    assert "ROLE_MISSING" in _audit(state, "S01", shift, NOW)  # an instructor without the role on that shift
    # merges: 4 + 4 at level 3 fits 1:8; 3 + 2 beginners breaks 1:4 - the group-norm category can light up too
    teacher_six = world.shifts["L-1109-G002"].assignee
    assert _audit(state, teacher_six, state.merge(world.shifts["L-1109-G002"], world.shifts["L-1109-G001"]), NOW) == ()
    teacher_seven = world.shifts["L-1109-G003"].assignee
    codes = _audit(state, teacher_seven, state.merge(world.shifts["L-1109-G003"], world.shifts["L-1109-G004"]), NOW)
    assert "RATIO_EXCEEDED" in codes and "IS_THE_ABSENT_PERSON" not in codes
    assert sim.primary_category(codes) in {"ratio", "program"}


def test_plan_b_follows_the_core_order_on_the_demo_morning():
    """Same answers as the core's judge scenario: 06:00 merges into the group next to it, 07:00 gets S06."""
    world = load_world()
    callout = Callout("S05", NOW, ("L-1109-G001", "L-1109-G004"))
    morning = Morning(0, SEED, NOW.date(), world.staff, world.shifts, {}, (callout,), {}, ())
    state = State(world, morning)
    at = NOW + timedelta(minutes=10)
    six = plan_b_options(state, world.shifts["L-1109-G001"], "S05", at)
    assert six[0]["kind"] == "merge" and six[0]["into_shift_id"] == "L-1109-G002"
    seven = plan_b_options(state, world.shifts["L-1109-G004"], "S05", at)
    assert [o["kind"] for o in seven][0] == "lead" and seven[0]["staff_id"] == "S06"
    assert [o["kind"] for o in seven][-2:] == ["reschedule", "close"]
    assert all(o["mode"] == "propose" for o in seven)


def test_baseline_takes_the_first_yes_and_coachcover_only_eligible_people(result):
    channels = Counter(case["channel"] for row in result["mornings_detail"] for case in row[BASELINE])
    assert set(channels) <= {"chat", "phone", "fallback"}
    coach = [case for row in result["mornings_detail"] for case in row[COACHCOVER]]
    assert {case["channel"] for case in coach} <= {"offer", "plan_b"}
    assert all(not case["human"] for case in coach if case["channel"] == "offer")  # assign_cover is auto
    assert all(case["human"] for case in coach if case["channel"] == "plan_b")  # plan B is propose
    assert result["summary"][BASELINE]["closed_without_human_pct"] == 0.0


def test_what_if_rows(result):
    checks_role = result["what_if"]["baseline_manager_checks_role"]["summary"]
    assert checks_role["violations_by_reason"]["role"] == 0 and checks_role["violations"] > 0
    batch = result["what_if"]["coachcover_three_offers_at_once"]["summary"]
    assert batch["violations"] == 0 and batch["cases"] == result["summary"][COACHCOVER]["cases"]


def test_stats_block_follows_the_contract(result):
    block = stats_block(result)
    assert block["mornings"] == 50 and block["seed"] == SEED and block["synthetic"] is True
    assert block["label"] == "synthetic school, 50 mornings, seed 20261109"
    assert block["baseline_violations"] == result["summary"][BASELINE]["violations"]
    assert block["coachcover_violations"] == 0
    assert block["baseline_violations_if_manager_checks_role"] == (
        result["what_if"]["baseline_manager_checks_role"]["summary"]["violations"])
    assert sim.sim_stats() == block  # the cached in-process run gives the same block


def test_cli_writes_both_files(tmp_path, capsys):
    assert sim.main(["--mornings", "3", "--seed", "7", "--out", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "SIM-50-MORNINGS.json").read_text(encoding="utf-8"))
    assert data["mornings"] == 3 and data["seed"] == 7 and data["label"] == "synthetic school, 3 mornings, seed 7"
    assert "| Metric |" in (tmp_path / "SIM-50-MORNINGS.md").read_text(encoding="utf-8")
    assert "synthetic school, 3 mornings, seed 7" in capsys.readouterr().out


def test_bad_arguments():
    with pytest.raises(ValueError, match="mornings"):
        run(0)
    with pytest.raises(ValueError, match="offer_batch"):
        run(1, params=SimParams(offer_batch=0))
