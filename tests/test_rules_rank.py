"""Queue order (product decision): eligible, fewer hours, same/closer venue, level match, tenure; each place has a reason."""

from datetime import date

import pytest

from coachcover.presets import load_preset
from coachcover.reasons_text import describe_place
from coachcover.rules import Candidate, Code, Decision, Reason, assess, rank
from tests.factory import AT, SWIM, booked, ctx, group, guard_shift, instructor, lesson, lifeguard, local

OK = Decision.of([])
BLOCKED = Decision.of([Reason(Code.ROLE_MISSING, {"role": "instructor"})])
LEVEL_3 = (("learn_to_swim", "3"),)


def _c(sid, ok=True, hours=10.0, same=True, km=0.0, match=True, tenure=100):
    return Candidate(sid, OK if ok else BLOCKED, hours, same, km, match, tenure, LEVEL_3)


def _ids(ranked):
    return [r.candidate.staff_id for r in ranked]


def test_eligible_go_above_ineligible_with_less_load():
    ranked = rank([_c("A", ok=False, hours=0.0), _c("B", hours=30.0)])
    assert _ids(ranked) == ["B", "A"]
    assert ranked[0].above_next == {"factor": "eligible", "this": True, "next": False, "next_staff_id": "A"}
    assert ranked[1].above_next is None


def test_queue_order_follows_the_five_factors_then_staff_id():
    pool = [
        _c("F", hours=12.0, same=False, km=5.0, match=True, tenure=300),
        _c("E", hours=12.0, same=False, km=5.0, match=True, tenure=300),
        _c("D", hours=12.0, same=False, km=5.0, match=True, tenure=900),
        _c("C", hours=12.0, same=False, km=5.0, match=False, tenure=None),
        _c("B", hours=12.0, same=False, km=1.0, match=False, tenure=None),
        _c("A", hours=12.0, same=True, km=0.0, match=False, tenure=None),
        _c("Z", hours=8.0, same=False, km=40.0, match=False, tenure=None),
    ]
    ranked = rank(pool)
    # the less loaded person wins over everything after eligibility
    assert _ids(ranked) == ["Z", "A", "B", "D", "E", "F", "C"]
    assert [r.above_next["factor"] for r in ranked[:-1]] == [
        "week_hours",  # Z 8 h vs A 12 h
        "same_venue",  # A at the shift's venue
        "distance_km",  # B 1 km vs D 5 km
        "level_tenure_days",  # D 900 days vs E 300
        "staff_id",  # E and F tie on everything
        "level_match",  # F matches the level, C does not
    ]


def test_same_venue_beats_a_closer_unknown_or_any_other_venue():
    ranked = rank([_c("A", same=False, km=0.5), _c("B", same=True, km=None)])
    assert _ids(ranked) == ["B", "A"]
    assert ranked[0].above_next["factor"] == "same_venue"


def test_unknown_distance_and_tenure_sort_last_within_their_factor():
    ranked = rank([_c("A", same=False, km=None), _c("B", same=False, km=30.0), _c("C", tenure=None), _c("D")])
    assert _ids(ranked) == ["D", "C", "B", "A"]


def test_rank_is_deterministic_for_any_input_order():
    pool = [
        _c(f"S{i:02d}", ok=i % 3 != 0, hours=float(i % 4), same=i % 2 == 0, km=float(i % 5), tenure=i * 7)
        for i in range(12)
    ]
    expected = _ids(rank(pool))
    for shuffled in (list(reversed(pool)), pool[5:] + pool[:5], pool[::2] + pool[1::2]):
        assert _ids(rank(shuffled)) == expected
    assert [r.position for r in rank(pool)] == list(range(1, 13))


def test_rank_rejects_the_same_person_twice():
    with pytest.raises(ValueError):
        rank([_c("A"), _c("A")])


def test_assess_collects_the_queue_facts():
    shift = lesson()  # Monday 06:00, venue A, level 3
    near = instructor("S1", home="A", since=date(2025, 11, 9))
    far = instructor("S2", home="B", since=date(2020, 11, 9))
    other_level = instructor("S3", home="A", levels=("5",))
    context = ctx(S2=[booked("TUE", local(2026, 11, 10, 8, 0), 6 * 60)])

    a, b, c = (assess(s, shift, AT, SWIM, context) for s in (near, far, other_level))
    assert (a.week_hours, a.same_venue, a.distance_km, a.level_match, a.level_tenure_days) == (0.0, True, 0.0, True, 365)
    assert (b.week_hours, b.same_venue, b.level_tenure_days) == (6.0, False, 2191)
    # 0.15 deg lat (16.7 km) and 0.15 deg lon at ~33.9N (13.9 km): about 21.7 km
    assert b.distance_km == pytest.approx(21.7, abs=0.1)
    assert (c.decision.ok, c.level_match, c.level_tenure_days) == (False, False, None)
    assert a.levels == LEVEL_3
    assert _ids(rank([c, b, a])) == ["S1", "S2", "S3"]


def test_assess_without_groups_or_home():
    guard = assess(lifeguard(home=None), guard_shift(), AT, SWIM, ctx())
    assert (guard.level_match, guard.level_tenure_days, guard.same_venue, guard.distance_km) == (None, None, False, None)
    assert guard.decision.ok


def test_assess_rejects_an_unknown_home_venue():
    with pytest.raises(ValueError, match="unknown venue"):
        assess(lifeguard(home="ZZ"), guard_shift(), AT, SWIM, ctx())


# --- the reason for a place, machine and human -----------------------------


def test_every_place_carries_a_machine_reason():
    ranked = rank([_c("M", hours=12.0, tenure=730), _c("O", hours=20.0, same=False, km=21.7)])
    assert ranked[0].place() == {
        "position": 1,
        "staff_id": "M",
        "eligible": True,
        "week_hours": 12.0,
        "same_venue": True,
        "distance_km": 0.0,
        "level_match": True,
        "levels": [["learn_to_swim", "3"]],
        "level_tenure_days": 730,
        "above_next": {"factor": "week_hours", "this": 12.0, "next": 20.0, "next_staff_id": "O"},
    }
    assert ranked[1].place()["above_next"] is None


def test_place_line_reads_like_the_queue_card():
    preset = load_preset("swim_school")
    ranked = rank([_c("M", hours=12.0, tenure=730), _c("O", hours=20.0, same=False, km=21.7, tenure=None)])
    assert describe_place(ranked[0], "Maria", "en", preset) == "Maria first: 12 h this week, Level 3, same venue, 2.0 yr at this level"
    assert describe_place(ranked[1], "Olga", "en", preset) == "Olga second: 20 h this week, Level 3, 21.7 km away"
    assert describe_place(ranked[0], "Мария", "ru", preset) == "Мария, №1: 12 ч на этой неделе, Level 3, та же площадка, 2.0 г. на этом уровне"


def test_place_line_for_an_ineligible_candidate_and_a_shift_without_levels():
    ranked = rank([_c(f"P{i}") for i in range(3)] + [_c("X", ok=False, match=False, tenure=None, same=False, km=None)])
    assert describe_place(ranked[3], "Xavier") == "Xavier #4: not eligible, 10 h this week, not cleared for learn_to_swim 3, other venue"
    guard = rank([Candidate("L1", OK, 4.0, True, 0.0, None, None, ())])[0]
    assert describe_place(guard, "Liam") == "Liam first: 4 h this week, same venue"
    with pytest.raises(ValueError):
        describe_place(guard, "Liam", "de")
