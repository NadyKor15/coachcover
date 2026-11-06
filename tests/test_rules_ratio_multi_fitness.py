"""Participants per instructor (merged groups), several reasons at once, and the fitness preset."""

from datetime import date

import pytest

from coverage.model import Shift, Staff, StaffProgram
from coverage.rules import Code, eligible, ratio_reason
from tests.factory import AT, FIT, SWIM, booked, card, ctx, group, instructor, lesson, lifeguard, local


def test_merged_groups_at_the_norm_pass():
    shift = lesson(groups=(group("G1", "3", 4), group("G2", "3", 4)))
    assert eligible(instructor(), shift, AT, SWIM, ctx()).ok


def test_merged_groups_above_the_norm_block():
    shift = lesson(groups=(group("G1", "3", 5), group("G2", "3", 4)))
    decision = eligible(instructor(), shift, AT, SWIM, ctx())
    assert decision.codes == [Code.RATIO_EXCEEDED]
    assert decision.reasons[0].params == {
        "participants": 9,
        "instructors": 1,
        "max_per_instructor": 8,
        "capacity": 8,
        "program": "learn_to_swim",
        "level": "3",
        "groups": ["G1", "G2"],
    }


def test_mixed_levels_take_the_strictest_norm_and_a_second_instructor_lifts_it():
    groups = (group("G1", "1", 3), group("G2", "2", 2))  # 1:4 beginners with 1:8
    staff = instructor(levels=("1", "2"))
    alone = eligible(staff, lesson(groups=groups), AT, SWIM, ctx())
    assert alone.codes == [Code.RATIO_EXCEEDED]
    assert alone.reasons[0].params["capacity"] == 4
    assert alone.reasons[0].params["level"] == "1"
    assert eligible(staff, lesson(groups=groups, instructors=2), AT, SWIM, ctx()).ok


def test_ratio_reason_edges():
    assert ratio_reason((), 1, SWIM.preset) is None
    assert ratio_reason((group(size=8),), 1, SWIM.preset) is None
    with pytest.raises(ValueError):
        ratio_reason((group(),), 0, SWIM.preset)


def test_merge_with_the_candidates_own_group_is_not_an_overlap():
    # plan B: the candidate already teaches G2 at 06:00; G1 (absent instructor) is merged into it
    merged = lesson(sid="PLANB", groups=(group("G1", "3", 4), group("G2", "3", 3)))
    own = [booked("G2", local(2026, 11, 9, 6, 0), 30, kind="group")]
    assert eligible(instructor(), merged, AT, SWIM, ctx(S1=own)).ok


def test_several_reasons_at_once_in_a_fixed_order():
    staff = lifeguard(
        certs=(card("cpr_aed", expires_on=date(2026, 11, 1)), card("lifeguarding")),
    )
    clash = booked("LG-0600", local(2026, 11, 9, 6, 0), 240)
    shift = lesson(groups=(group("G1", "3", 9),))
    decision = eligible(staff, shift, AT, SWIM, ctx(L1=[clash]))
    assert not decision.ok
    assert decision.codes == [
        Code.ROLE_MISSING,
        Code.CERT_MISSING,  # wsi
        Code.CERT_EXPIRED,  # cpr_aed
        Code.PROGRAM_MISSING,
        Code.OVERLAPS_COMMITMENT,
        Code.RATIO_EXCEEDED,
    ]


# --- the same code on the fitness preset -----------------------------------


def _coach(sid: str = "F1", formats=("hiit", "cycle"), certs=None, renewed_at=date(2026, 9, 1)) -> Staff:
    if certs is None:
        certs = (card("trainer_cert"), card("cpr_aed"))
    programs = {"group_classes": StaffProgram("group_classes", renewed_at, {f: date(2025, 3, 1) for f in formats})}
    return Staff(sid, f"Coach {sid}", frozenset({"group_instructor"}), certs, programs, "A")


def _class(level: str = "hiit", size: int = 18, minutes: int = 45, start=local(2026, 11, 9, 6, 0)) -> Shift:
    from datetime import timedelta

    return Shift("C1", "A", "group_instructor", start, start + timedelta(minutes=minutes), (group("CL1", level, size, "group_classes"),))


def test_fitness_preset_eligible_coach_passes():
    assert eligible(_coach(), _class(), AT, FIT, ctx()).reasons == ()


def test_fitness_preset_class_cap_expired_card_and_missing_format():
    assert eligible(_coach(), _class(size=21), AT, FIT, ctx()).codes == [Code.RATIO_EXCEEDED]
    expired = _coach(certs=(card("trainer_cert", expires_on=date(2026, 11, 8)), card("cpr_aed")))
    assert eligible(expired, _class(), AT, FIT, ctx()).codes == [Code.CERT_EXPIRED]
    assert eligible(_coach(), _class(level="yoga"), AT, FIT, ctx()).codes == [Code.LEVEL_MISSING]


def test_fitness_preset_week_starts_monday():
    # 8 h on Sunday Nov 8 belong to the previous fitness week
    sunday = [booked("SUN", local(2026, 11, 8, 8, 0), 8 * 60)]
    long_week = [booked(f"D{i}", local(2026, 11, 10 + i, 6, 0), 8 * 60) for i in range(4)]  # 32 h Tue-Fri
    decision = eligible(_coach(), _class(minutes=60), AT, FIT, ctx(F1=sunday + long_week))
    assert decision.ok  # 32 + 1
    swim_like = eligible(instructor(), lesson(minutes=60), AT, SWIM, ctx(S1=sunday + long_week))
    assert swim_like.codes == [Code.HOURS_LIMIT_EXCEEDED]  # Sunday week: 8 + 32 + 1


def test_swim_roles_do_not_exist_in_the_fitness_preset():
    with pytest.raises(ValueError, match="role 'instructor'"):
        eligible(instructor(), lesson(), AT, FIT, ctx())
