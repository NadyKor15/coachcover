"""Ranking: eligible first, then lower weekly load, closer home venue, longer tenure at the level."""

from datetime import date

import pytest

from coverage.rules import Candidate, Decision, Reason, Code, assess, rank
from tests.factory import AT, SWIM, booked, card, ctx, guard_shift, instructor, lesson, lifeguard, local

OK = Decision.of([])
BLOCKED = Decision.of([Reason(Code.ROLE_MISSING, {"role": "instructor"})])


def _c(sid, ok=True, hours=10.0, km=0.0, tenure=100):
    return Candidate(sid, OK if ok else BLOCKED, hours, km, tenure)


def test_eligible_rank_above_ineligible_with_less_load():
    ranked = rank([_c("A", ok=False, hours=0.0), _c("B", hours=30.0)])
    assert [r.candidate.staff_id for r in ranked] == ["B", "A"]
    assert ranked[0].above_next == {"factor": "eligible", "this": True, "next": False, "next_staff_id": "A"}
    assert ranked[1].above_next is None


def test_factor_order_load_then_distance_then_tenure_then_id():
    pool = [
        _c("E", hours=12.0, km=5.0, tenure=300),
        _c("D", hours=12.0, km=5.0, tenure=300),
        _c("C", hours=12.0, km=5.0, tenure=900),
        _c("B", hours=12.0, km=1.0, tenure=10),
        _c("A", hours=8.0, km=20.0, tenure=10),
    ]
    ranked = rank(pool)
    assert [r.candidate.staff_id for r in ranked] == ["A", "B", "C", "D", "E"]
    assert [r.above_next["factor"] for r in ranked[:-1]] == [
        "week_hours",
        "distance_km",
        "level_tenure_days",
        "staff_id",
    ]
    assert ranked[2].above_next["this"] == 900 and ranked[2].above_next["next"] == 300


def test_unknown_distance_and_tenure_sort_last():
    ranked = rank([_c("A", km=None), _c("B", km=30.0), _c("C", tenure=None), _c("D")])
    assert [r.candidate.staff_id for r in ranked] == ["D", "C", "B", "A"]


def test_rank_is_deterministic_for_any_input_order():
    pool = [_c(f"S{i:02d}", ok=i % 3 != 0, hours=float(i % 4), km=float(i % 2), tenure=i * 7) for i in range(12)]
    expected = [r.candidate.staff_id for r in rank(pool)]
    for shuffled in (list(reversed(pool)), pool[5:] + pool[:5], sorted(pool, key=lambda c: c.tenure if False else c.staff_id)):
        assert [r.candidate.staff_id for r in rank(shuffled)] == expected
    assert [r.position for r in rank(pool)] == list(range(1, 13))


def test_rank_rejects_the_same_person_twice():
    with pytest.raises(ValueError):
        rank([_c("A"), _c("A")])


def test_assess_collects_the_ranking_facts():
    shift = lesson()  # Monday 06:00 at venue A
    near = instructor("S1", home="A", since=date(2025, 11, 9))
    far = instructor("S2", home="B", since=date(2020, 11, 9))
    busy_week = [booked("TUE", local(2026, 11, 10, 8, 0), 6 * 60)]
    context = ctx(S2=busy_week)

    a, b = assess(near, shift, AT, SWIM, context), assess(far, shift, AT, SWIM, context)
    assert (a.week_hours, a.distance_km, a.level_tenure_days) == (0.0, 0.0, 365)
    assert b.week_hours == 6.0
    # 0.15 deg lat (16.7 km) and 0.15 deg lon at ~33.9N (13.9 km): about 21.7 km
    assert b.distance_km == pytest.approx(21.7, abs=0.1)
    assert b.level_tenure_days == 2191
    assert [r.candidate.staff_id for r in rank([b, a])] == ["S1", "S2"]


def test_assess_without_groups_has_no_tenure_and_no_home_has_no_distance():
    guard = assess(lifeguard(home=None), guard_shift(), AT, SWIM, ctx())
    assert guard.level_tenure_days is None
    assert guard.distance_km is None
    assert guard.decision.ok


def test_assess_rejects_an_unknown_home_venue():
    with pytest.raises(ValueError, match="unknown venue"):
        assess(lifeguard(home="ZZ"), guard_shift(), AT, SWIM, ctx())
