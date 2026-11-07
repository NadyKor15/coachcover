"""Rule fixes from the adversarial review of stages A+B (REVIEW-CORE-A-B-2026-11-06, items 1, 4, 6, 7, 12, renewal)."""

from datetime import date, timedelta

from coachcover.model import Shift
from coachcover.reasons_text import describe
from coachcover.rules import Code, assess, eligible, rank
from tests.factory import AT, SWIM, booked, card, ctx, group, guard_shift, instructor, lesson, lifeguard, local


def _with(shift: Shift, **changes) -> Shift:
    fields = {
        "id": shift.id, "venue": shift.venue, "role": shift.role, "start": shift.start, "end": shift.end,
        "groups": shift.groups, "instructors": shift.instructors, "zone": shift.zone,
    }
    fields.update(changes)
    return Shift(**fields)


# 1. the absent person cannot cover (or be first in the queue for) her own shift
def test_review_1_the_absent_person_is_blocked_on_her_own_shift():
    shift = _with(lesson(), assignee="S1")
    own = eligible(instructor("S1"), shift, AT, SWIM, ctx())
    assert not own.ok and own.codes == [Code.IS_THE_ABSENT_PERSON]
    assert eligible(instructor("S2"), shift, AT, SWIM, ctx()).ok
    ranked = rank(assess(instructor(s), shift, AT, SWIM, ctx()) for s in ("S1", "S2"))
    assert [r.candidate.staff_id for r in ranked] == ["S2", "S1"]


# 4. merging groups: the partner's own shift is recognised as a shift, ids of shifts and groups are not mixed
def test_review_4_merge_with_commitments_shaped_like_the_repo():
    merged = _with(
        lesson(sid="PLANB", groups=(group("G1", "3", 4), group("G2", "3", 3))), merged_shift_ids=("L-1109-G2",)
    )
    as_repo = [booked("L-1109-G2", local(2026, 11, 9, 6, 0), 30, kind="shift")]
    assert eligible(instructor(), merged, AT, SWIM, ctx(S1=as_repo)).ok
    assert assess(instructor(), merged, AT, SWIM, ctx(S1=as_repo)).week_hours == 0.0
    not_merged = _with(merged, merged_shift_ids=())
    assert eligible(instructor(), not_merged, AT, SWIM, ctx(S1=as_repo)).codes == [Code.OVERLAPS_COMMITMENT]


def test_review_4_a_shift_named_like_a_group_is_still_seen():
    clash = [booked("G1", local(2026, 11, 9, 6, 0), 240, kind="shift")]
    decision = eligible(instructor(), lesson(groups=(group("G1", "3", 4),)), AT, SWIM, ctx(S1=clash))
    assert decision.codes == [Code.OVERLAPS_COMMITMENT]


# 6. a card renewed at midnight inside a night shift: the person is covered every second
def test_review_6_renewal_at_midnight_inside_an_overnight_shift():
    staff = lifeguard(
        certs=(
            card("lifeguarding", expires_on=date(2026, 11, 9), issued_on=date(2024, 11, 9)),
            card("lifeguarding", expires_on=date(2028, 11, 10), issued_on=date(2026, 11, 10)),
            card("cpr_aed"),
        )
    )
    decision = eligible(staff, guard_shift(start=local(2026, 11, 9, 22, 0), hours=4), AT, SWIM, ctx())
    assert decision.ok and decision.reasons == ()


def test_review_6_a_gap_between_cards_still_blocks():
    staff = lifeguard(
        certs=(
            card("lifeguarding", expires_on=date(2026, 11, 9), issued_on=date(2024, 11, 9)),
            card("lifeguarding", expires_on=date(2028, 11, 11), issued_on=date(2026, 11, 11)),
            card("cpr_aed"),
        )
    )
    decision = eligible(staff, guard_shift(start=local(2026, 11, 9, 22, 0), hours=4), AT, SWIM, ctx())
    assert decision.codes == [Code.CERT_EXPIRES_DURING_SHIFT]  # the old card runs out mid-shift, the new one starts a day later
    assert decision.reasons[0].params["expires_on"] == "2026-11-09"


# 7. bookings that overlap each other count once
def test_review_7_overlapping_bookings_are_merged_before_summing():
    same_time = [
        booked("SH-A", local(2026, 11, 10, 6, 0), 20 * 60, kind="shift"),
        booked("GRP-A", local(2026, 11, 10, 6, 0), 20 * 60, kind="group"),
    ]
    assert eligible(instructor(), lesson(minutes=60), AT, SWIM, ctx(S1=same_time)).ok
    assert assess(instructor(), lesson(minutes=60), AT, SWIM, ctx(S1=same_time)).week_hours == 20.0


# 12. ten seconds over the limit must not read as "40 h of 40"
def test_review_12_text_agrees_with_the_block():
    week = [booked(f"W{i}", local(2026, 11, 10 + i, 8, 0), 8 * 60) for i in range(4)]
    week.append(booked("W4", local(2026, 11, 14, 8, 0), 7 * 60 + 30))  # 39.5 h
    start = local(2026, 11, 9, 6, 0)
    shift = Shift("SEC", "A", "instructor", start, start + timedelta(minutes=30, seconds=10), (group(),))
    decision = eligible(instructor(), shift, AT, SWIM, ctx(S1=week))
    assert decision.codes == [Code.HOURS_LIMIT_EXCEEDED]
    assert decision.reasons[0].params["hours_after"] > 40
    text = describe(decision.reasons[0], "en")  # hours and minutes, rounded up: still over the limit
    assert "would have 40 h 1 min in the week of" in text and text.endswith("(limit 40 h)")


# risk decision: program renewal is checked over the whole shift, like a certificate
def test_program_renewal_is_checked_over_the_whole_shift():
    due_today = instructor(renewed_at=date(2025, 11, 9))  # due 2026-11-09
    day = lesson(start=local(2026, 11, 9, 18, 0))
    across_midnight = lesson(start=local(2026, 11, 9, 23, 45))  # ends 00:15 on Nov 10
    assert eligible(due_today, day, AT, SWIM, ctx()).ok
    late = eligible(due_today, across_midnight, AT, SWIM, ctx())
    assert late.codes == [Code.PROGRAM_RENEWAL_OVERDUE]
