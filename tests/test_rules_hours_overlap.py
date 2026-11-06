"""Weekly hours (policy week, DST-correct) and overlaps with the person's own bookings."""

from datetime import timedelta

from coachcover.model import Shift
from coachcover.presets import default_policy
from coachcover.rules import Code, eligible
from tests.factory import AT, SWIM, booked, ctx, group, guard_shift, instructor, lesson, lifeguard, local

SUNDAY_NOV_8 = local(2026, 11, 8)
MONDAY_NOV_9 = local(2026, 11, 9)


def _week(total_minutes: int, first_day, prefix: str = "W") -> list:
    """`total_minutes` in blocks of up to 8 h, 08:00 each day, starting the day after `first_day`."""
    out, day, left = [], 0, total_minutes
    while left > 0:
        chunk = min(left, 8 * 60)
        out.append(booked(f"{prefix}{day}", first_day.replace(hour=8) + timedelta(days=day + 1), chunk))
        left -= chunk
        day += 1
    return out


def test_hours_exactly_at_the_limit_pass():
    commitments = _week(39 * 60 + 30, SUNDAY_NOV_8)  # 39.5 h, from Monday on
    decision = eligible(instructor(), lesson(minutes=30), AT, SWIM, ctx(S1=commitments))
    assert decision.ok, decision.reasons


def test_one_minute_over_the_limit_blocks():
    commitments = _week(39 * 60 + 30, SUNDAY_NOV_8)
    decision = eligible(instructor(), lesson(minutes=31), AT, SWIM, ctx(S1=commitments))
    assert decision.codes == [Code.HOURS_LIMIT_EXCEEDED]
    assert decision.reasons[0].params == {
        "week_start": "2026-11-08",
        "hours_before": 39.5,
        "shift_hours": 0.52,
        "hours_after": 40.02,
        "limit_hours": 40,
    }


def test_week_start_comes_from_the_policy():
    # 8 h on Sunday Nov 8 plus 31.5 h Tuesday-Friday; the 1 h shift is on Monday Nov 9.
    commitments = [booked("SUN", local(2026, 11, 8, 8, 0), 8 * 60), *_week(31 * 60 + 30, MONDAY_NOV_9)]
    shift = lesson(minutes=60)
    sunday_week = eligible(instructor(), shift, AT, SWIM, ctx(S1=commitments))
    monday_week = eligible(instructor(), shift, AT, default_policy("swim_school", week_start="monday"), ctx(S1=commitments))
    assert sunday_week.codes == [Code.HOURS_LIMIT_EXCEEDED]  # 8 + 31.5 + 1 = 40.5
    assert monday_week.ok  # Sunday belongs to the previous week: 31.5 + 1


def test_shift_across_midnight_and_the_week_boundary_counts_in_each_week():
    # Saturday 22:00 -> Sunday 02:00; the swim policy week starts on Sunday.
    shift = guard_shift(start=local(2026, 11, 7, 22, 0), hours=4)
    old_week = _week(39 * 60, local(2026, 11, 1), prefix="OLD")  # 39 h in the week of Nov 1
    decision = eligible(lifeguard(), shift, AT, SWIM, ctx(L1=old_week))
    assert decision.codes == [Code.HOURS_LIMIT_EXCEEDED]
    params = decision.reasons[0].params
    assert params["week_start"] == "2026-11-01"
    assert params["shift_hours"] == 2.0  # only Saturday's 2 h count in that week
    assert params["hours_after"] == 41.0


def test_dst_fall_back_night_counts_real_hours():
    # Nov 1 2026 New York: clocks go back at 02:00, so 00:00-04:00 is 5 real hours.
    shift = guard_shift(start=local(2026, 11, 1, 0, 0), hours=4)
    assert (shift.end - shift.start) == timedelta(hours=4)  # what naive wall-clock math would see
    booked_week = _week(35 * 60 + 30, local(2026, 11, 1), prefix="D")  # 35.5 h Mon-Fri
    decision = eligible(lifeguard(), shift, AT, SWIM, ctx(L1=booked_week))
    assert decision.codes == [Code.HOURS_LIMIT_EXCEEDED]
    assert decision.reasons[0].params["shift_hours"] == 5.0
    assert decision.reasons[0].params["hours_after"] == 40.5


def test_dst_spring_forward_night_counts_real_hours():
    # Mar 8 2026 New York: 01:00-04:00 local is 2 real hours.
    shift = Shift("LGS", "A", "lifeguard", local(2026, 3, 8, 1, 0), local(2026, 3, 8, 4, 0))
    booked_week = _week(37 * 60 + 30, local(2026, 3, 8), prefix="S")  # 37.5 h
    decision = eligible(lifeguard(), shift, local(2026, 3, 7, 12), SWIM, ctx(L1=booked_week))
    assert decision.ok  # 37.5 + 2 = 39.5; naive 3 h would give 40.5 and block


def test_overlap_by_one_minute_blocks_and_back_to_back_passes():
    one_minute = booked("EARLY", local(2026, 11, 9, 5, 31), 30)  # 05:31-06:01
    back_to_back = booked("EARLY", local(2026, 11, 9, 5, 30), 30)  # 05:30-06:00
    overlapping = eligible(instructor(), lesson(), AT, SWIM, ctx(S1=[one_minute]))
    assert overlapping.codes == [Code.OVERLAPS_COMMITMENT]
    assert overlapping.reasons[0].params == {
        "kind": "shift",
        "ref": "EARLY",
        "start": "2026-11-09T05:31:00-05:00",
        "end": "2026-11-09T06:01:00-05:00",
    }
    assert eligible(instructor(), lesson(), AT, SWIM, ctx(S1=[back_to_back])).ok


def test_overlap_with_own_group_is_reported_as_group():
    own_group = booked("G77", local(2026, 11, 9, 6, 15), 45, kind="group")
    decision = eligible(instructor(), lesson(), AT, SWIM, ctx(S1=[own_group]))
    assert decision.codes == [Code.OVERLAPS_COMMITMENT]
    assert decision.reasons[0].params["kind"] == "group"


def test_repeated_dst_hour_is_compared_in_real_time():
    # 01:00-01:45 EDT (first pass) and 01:00-02:00 EST (second pass) look the same on the wall clock
    first_pass = booked("EDT", local(2026, 11, 1, 1, 0), 45)
    second_pass = Shift("EST", "A", "lifeguard", local(2026, 11, 1, 1, 0, fold=1), local(2026, 11, 1, 2, 0))
    decision = eligible(lifeguard(), second_pass, AT, SWIM, ctx(L1=[first_pass]))
    assert decision.ok, decision.reasons


def test_this_shift_and_its_groups_are_not_counted_against_the_person():
    shift = lesson(groups=(group("G1", "3", 3), group("G2", "3", 3)))
    already = [booked(shift.id, shift.start, 30), booked("G2", shift.start, 30, kind="group")]
    assert eligible(instructor(), shift, AT, SWIM, ctx(S1=already)).ok
