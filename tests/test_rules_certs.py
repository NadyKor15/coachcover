"""Certificates: valid for the whole shift, read in the venue's time zone."""

from datetime import date, timedelta

import pytest

from coverage.model import Shift
from coverage.rules import Code, add_months, eligible
from tests.factory import AT, LA, NY, SWIM, card, ctx, guard_shift, instructor, lesson, lifeguard, local


def test_valid_certificates_pass_without_reasons():
    decision = eligible(instructor(), lesson(), AT, SWIM, ctx())
    assert decision.ok
    assert decision.reasons == ()


def test_expired_yesterday_blocks():
    staff = instructor(certs=(card("wsi"), card("cpr_aed", expires_on=date(2026, 11, 8))))
    decision = eligible(staff, lesson(), AT, SWIM, ctx())
    assert not decision.ok
    assert decision.codes == [Code.CERT_EXPIRED]
    assert decision.reasons[0].params == {"cert_type": "cpr_aed", "expires_on": "2026-11-08"}


def test_expires_today_is_a_warning_not_a_block():
    staff = instructor(certs=(card("wsi"), card("cpr_aed", expires_on=date(2026, 11, 9))))
    decision = eligible(staff, lesson(), AT, SWIM, ctx())
    assert decision.ok
    (reason,) = decision.reasons
    assert reason.code == Code.CERT_EXPIRES_ON_SHIFT_DAY
    assert reason.severity == "warn"
    assert reason.params == {"cert_type": "cpr_aed", "expires_on": "2026-11-09", "today": True}


def test_expires_on_shift_day_seen_from_the_night_before():
    staff = instructor(certs=(card("wsi"), card("cpr_aed", expires_on=date(2026, 11, 9))))
    night_check = local(2026, 11, 8, 23, 0)
    decision = eligible(staff, lesson(), night_check, SWIM, ctx())
    assert decision.ok
    assert decision.reasons[0].params["today"] is False


def test_expires_during_an_overnight_shift_blocks():
    staff = lifeguard(certs=(card("lifeguarding", expires_on=date(2026, 11, 9)), card("cpr_aed")))
    shift = guard_shift(start=local(2026, 11, 9, 22, 0), hours=4)  # 22:00 -> 02:00
    decision = eligible(staff, shift, AT, SWIM, ctx())
    assert not decision.ok
    (reason,) = decision.reasons
    assert reason.code == Code.CERT_EXPIRES_DURING_SHIFT
    assert reason.params["valid_until"] == "2026-11-10T00:00:00-05:00"


def test_shift_ending_exactly_at_midnight_stays_on_the_expiry_day():
    staff = lifeguard(certs=(card("lifeguarding", expires_on=date(2026, 11, 9)), card("cpr_aed")))
    shift = guard_shift(start=local(2026, 11, 9, 20, 0), hours=4)  # 20:00 -> 00:00
    decision = eligible(staff, shift, AT, SWIM, ctx())
    assert decision.ok
    assert decision.codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY]


def test_not_yet_valid_card_blocks():
    staff = instructor(certs=(card("wsi", expires_on=date(2028, 11, 10), issued_on=date(2026, 11, 10)), card("cpr_aed")))
    decision = eligible(staff, lesson(), AT, SWIM, ctx())
    assert decision.codes == [Code.CERT_NOT_YET_VALID]
    assert decision.reasons[0].params["valid_from"] == "2026-11-10"


def test_missing_certificate_blocks():
    staff = instructor(certs=(card("cpr_aed"),))
    decision = eligible(staff, lesson(), AT, SWIM, ctx())
    assert decision.codes == [Code.CERT_MISSING]
    assert decision.reasons[0].params == {"cert_type": "wsi"}


def test_expiry_is_read_in_the_venue_time_zone():
    # 22:00-23:30 in Los Angeles on Nov 9 is already Nov 10 in New York and in UTC.
    staff = lifeguard(certs=(card("lifeguarding", expires_on=date(2026, 11, 9)), card("cpr_aed")))
    start = local(2026, 11, 9, 22, 0, tz=LA)
    west = Shift("LGW", "W", "lifeguard", start, start + timedelta(minutes=90))
    east = Shift("LGE", "A", "lifeguard", start.astimezone(NY), (start + timedelta(minutes=90)).astimezone(NY))
    at = local(2026, 11, 9, 12, 0, tz=LA)

    west_decision = eligible(staff, west, at, SWIM, ctx())
    east_decision = eligible(staff, east, at, SWIM, ctx())

    assert west_decision.ok and west_decision.codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY]
    assert not east_decision.ok and east_decision.codes == [Code.CERT_EXPIRED]


def test_renewed_card_wins_over_the_expired_one():
    staff = instructor(
        certs=(
            card("wsi"),
            card("cpr_aed", expires_on=date(2026, 10, 1)),
            card("cpr_aed", expires_on=date(2028, 10, 1)),
        )
    )
    assert eligible(staff, lesson(), AT, SWIM, ctx()).reasons == ()


def test_card_without_printed_expiry_uses_the_type_validity():
    on_the_day = instructor(certs=(card("wsi"), card("cpr_aed", expires_on=None, issued_on=date(2024, 11, 9))))
    day_before = instructor(certs=(card("wsi"), card("cpr_aed", expires_on=None, issued_on=date(2024, 11, 8))))
    assert eligible(on_the_day, lesson(), AT, SWIM, ctx()).codes == [Code.CERT_EXPIRES_ON_SHIFT_DAY]
    expired = eligible(day_before, lesson(), AT, SWIM, ctx())
    assert expired.codes == [Code.CERT_EXPIRED]
    assert expired.reasons[0].params["expires_on"] == "2026-11-08"


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),
        (date(2024, 2, 29), 24, date(2026, 2, 28)),
        (date(2026, 11, 9), -24, date(2024, 11, 9)),
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
    ],
)
def test_add_months_clamps_to_month_end(start, months, expected):
    assert add_months(start, months) == expected


def test_card_whose_readings_disagree_blocks_until_a_person_checks():
    disputed = instructor(certs=(card("wsi", needs_review=True), card("cpr_aed")))
    decision = eligible(disputed, lesson(), AT, SWIM, ctx())
    assert not decision.ok
    assert decision.codes == [Code.CERT_NEEDS_REVIEW]

    with_second_card = instructor(certs=(card("wsi", needs_review=True), card("wsi"), card("cpr_aed")))
    assert eligible(with_second_card, lesson(), AT, SWIM, ctx()).ok
