"""Role, program, level and program renewal on the shift date."""

from datetime import date

import pytest

from coverage.rules import Code, eligible
from tests.factory import AT, SWIM, card, ctx, group, guard_shift, instructor, lesson, lifeguard


def test_missing_role_blocks():
    staff = lifeguard(certs=(card("lifeguarding"), card("cpr_aed"), card("wsi")))
    decision = eligible(staff, lesson(), AT, SWIM, ctx())
    assert not decision.ok
    assert decision.reasons[0].code == Code.ROLE_MISSING
    assert decision.reasons[0].params == {"role": "instructor"}


def test_missing_program_blocks():
    decision = eligible(instructor(levels=()), lesson(), AT, SWIM, ctx())
    assert decision.codes == [Code.PROGRAM_MISSING]
    assert decision.reasons[0].params == {"program": "learn_to_swim"}


def test_missing_level_blocks():
    decision = eligible(instructor(levels=("1", "2")), lesson(), AT, SWIM, ctx())
    assert decision.codes == [Code.LEVEL_MISSING]
    assert decision.reasons[0].params == {"program": "learn_to_swim", "level": "3"}


def test_level_granted_after_the_shift_date_does_not_count():
    decision = eligible(instructor(since=date(2026, 11, 10)), lesson(), AT, SWIM, ctx())
    assert decision.codes == [Code.LEVEL_MISSING]


def test_program_renewal_overdue_on_the_shift_date():
    overdue = instructor(renewed_at=date(2025, 11, 8))  # due 2026-11-08, shift on 2026-11-09
    decision = eligible(overdue, lesson(), AT, SWIM, ctx())
    assert not decision.ok
    assert decision.codes == [Code.PROGRAM_RENEWAL_OVERDUE]
    assert decision.reasons[0].params == {
        "program": "learn_to_swim",
        "renewed_at": "2025-11-08",
        "due_on": "2026-11-08",
    }


def test_program_renewal_due_on_the_shift_day_still_counts():
    assert eligible(instructor(renewed_at=date(2025, 11, 9)), lesson(), AT, SWIM, ctx()).ok


def test_renewal_reported_once_for_two_groups_of_one_program():
    staff = instructor(levels=("3", "4"), renewed_at=date(2025, 1, 1))
    shift = lesson(groups=(group("G1", "3", 3), group("G2", "4", 3)))
    assert eligible(staff, shift, AT, SWIM, ctx()).codes == [Code.PROGRAM_RENEWAL_OVERDUE]


def test_shift_without_groups_skips_program_checks():
    assert eligible(lifeguard(), guard_shift(), AT, SWIM, ctx()).reasons == ()


def test_unknown_level_in_the_shift_is_a_data_error():
    with pytest.raises(ValueError, match="level '9'"):
        eligible(instructor(), lesson(groups=(group(level="9"),)), AT, SWIM, ctx())


def test_unknown_role_in_the_shift_is_a_data_error():
    from coverage.model import Shift

    shift = guard_shift()
    bad = Shift(shift.id, shift.venue, "pilot", shift.start, shift.end)
    with pytest.raises(ValueError, match="role 'pilot'"):
        eligible(lifeguard(), bad, AT, SWIM, ctx())
