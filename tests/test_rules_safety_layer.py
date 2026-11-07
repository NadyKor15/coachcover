"""Ideas 2-4 of 2026-11-06: a card the issuer revoked, rest between work days, the families' language in the queue."""

import dataclasses
from datetime import date

from coachcover.model import GroupRef, Staff, StaffCert
from coachcover.presets import default_policy
from coachcover.reasons_text import describe, describe_place
from coachcover.rules import Candidate, Code, Decision, assess, eligible, family_language, rank
from coachcover.screens import reason_label
from tests.factory import AT, NY, SWIM, booked, ctx, guard_shift, instructor, lesson, lifeguard, local

LEVEL_3 = (("learn_to_swim", "3"),)


def _codes(decision):
    return [r.code for r in decision.reasons]


# --- idea 2: revoked by the issuer ----------------------------------------------------------


def _revoked_guard(revoked_on=date(2026, 7, 24), others=()):
    printed = StaffCert("lifeguarding", date(2025, 6, 22), date(2027, 6, 22), revoked_on=revoked_on)
    return lifeguard(certs=(printed, StaffCert("cpr_aed", date(2025, 1, 1), date(2027, 12, 31)), *others))


def test_a_revoked_card_blocks_even_though_its_printed_date_is_ahead():
    decision = eligible(_revoked_guard(), guard_shift(), AT, SWIM, ctx())
    assert _codes(decision) == [Code.CERT_REVOKED] and not decision.ok
    assert dict(decision.reasons[0].params) == {"cert_type": "lifeguarding", "revoked_on": "2026-07-24",
                                                "expires_on": "2027-06-22"}
    assert describe(decision.reasons[0], "en", SWIM.preset) == (
        "Lifeguarding valid until 2027-06-22, but revoked by the issuer on 2026-07-24")
    label = reason_label("CERT_REVOKED", dict(decision.reasons[0].params), SWIM.preset, None, NY)
    assert label == "Valid until Jun 2027 — REVOKED by issuer on Jul 24 (simulated registry)"


def test_revocation_counts_from_its_day_and_a_new_card_still_covers():
    # revoked after the shift: the card still counted on the day of the shift
    assert eligible(_revoked_guard(revoked_on=date(2026, 11, 10)), guard_shift(), AT, SWIM, ctx()).ok
    # revoked on the day of the shift: it stops at 00:00 that day
    assert _codes(eligible(_revoked_guard(revoked_on=date(2026, 11, 9)), guard_shift(), AT, SWIM, ctx())) == [Code.CERT_REVOKED]
    # a newer card of the same type that was not revoked covers the shift
    newer = StaffCert("lifeguarding", date(2026, 8, 1), date(2028, 8, 1))
    assert eligible(_revoked_guard(others=(newer,)), guard_shift(), AT, SWIM, ctx()).ok


def test_an_expired_revoked_card_is_reported_as_expired():
    old = StaffCert("lifeguarding", date(2024, 1, 1), date(2026, 1, 1), revoked_on=date(2025, 6, 1))
    person = lifeguard(certs=(old, StaffCert("cpr_aed", date(2025, 1, 1), date(2027, 12, 31))))
    assert _codes(eligible(person, guard_shift(), AT, SWIM, ctx())) == [Code.CERT_EXPIRED]


# --- idea 3: rest between work on different days ------------------------------------------


def test_closing_at_22_then_opening_at_06_is_blocked():
    closing = booked("SUN-EVE", local(2026, 11, 8, 18, 0), 240)  # Sunday 18:00-22:00
    decision = eligible(lifeguard(), guard_shift(), AT, SWIM, ctx(L1=[closing]))
    assert _codes(decision) == [Code.REST_TOO_SHORT] and not decision.ok
    params = dict(decision.reasons[0].params)
    assert (params["side"], params["rest_hours"], params["min_rest_hours"], params["ref"]) == ("before", 8.0, 10, "SUN-EVE")
    assert reason_label("REST_TOO_SHORT", params, SWIM.preset, None, NY) == "Only 8 h rest since 22:00"
    assert describe(decision.reasons[0], "en") == "only 8 h of rest since 22:00 (needs 10 h)"


def test_rest_is_checked_after_the_shift_too_and_ten_hours_are_enough():
    evening = guard_shift(start=local(2026, 11, 9, 18, 0), hours=4)  # Monday 18:00-22:00
    opening = booked("TUE-0600", local(2026, 11, 10, 6, 0), 240)
    decision = eligible(lifeguard(), evening, AT, SWIM, ctx(L1=[opening]))
    assert _codes(decision) == [Code.REST_TOO_SHORT]
    assert reason_label("REST_TOO_SHORT", dict(decision.reasons[0].params), SWIM.preset, None, NY) == "Only 8 h rest before 6:00"
    ten = booked("SUN-EVE", local(2026, 11, 8, 16, 0), 240)  # ends 20:00: exactly 10 h before 06:00
    assert eligible(lifeguard(), guard_shift(), AT, SWIM, ctx(L1=[ten])).ok


def test_a_split_shift_on_the_same_day_is_not_a_rest_question():
    morning = booked("MON-0530", local(2026, 11, 9, 5, 30), 30)  # ends 06:00, the shift starts at 10:00
    later = guard_shift(start=local(2026, 11, 9, 10, 0))
    assert eligible(lifeguard(), later, AT, SWIM, ctx(L1=[morning])).ok


def test_without_the_preset_parameter_there_is_no_rest_rule():
    loose = dataclasses.replace(SWIM, preset=dataclasses.replace(SWIM.preset, min_rest_hours=None))
    closing = booked("SUN-EVE", local(2026, 11, 8, 18, 0), 240)
    assert eligible(lifeguard(), guard_shift(), AT, loose, ctx(L1=[closing])).ok


def test_both_presets_set_ten_hours():
    assert SWIM.preset.min_rest_hours == 10 and default_policy("fitness_club").preset.min_rest_hours == 10


# --- idea 4: the families' language, the last key after the product queue order ----------------


def _spanish_group(es=6, en=2):
    return GroupRef("G1", "learn_to_swim", "3", es + en, None, (("en", en), ("es", es)))


def _speaker(sid, languages):
    return dataclasses.replace(instructor(sid), languages=frozenset(languages))


def test_majority_language_needs_more_than_half_of_the_families():
    assert family_language([_spanish_group()]) == ("es", 6, 8)
    assert family_language([_spanish_group(es=4, en=4)]) == (None, 0, 8)
    assert family_language([GroupRef("G2", "learn_to_swim", "3", 5)]) == (None, 0, 0)


def test_language_breaks_a_full_tie_and_never_beats_the_product_queue_order():
    shift = lesson(groups=(_spanish_group(),))
    a, b = _speaker("S1", {"en"}), _speaker("S2", {"es", "en"})
    tied = rank([assess(a, shift, AT, SWIM, ctx()), assess(b, shift, AT, SWIM, ctx())])
    assert [r.candidate.staff_id for r in tied] == ["S2", "S1"]  # same hours, venue, level, tenure: the speaker first
    assert tied[0].above_next["factor"] == "family_language"
    line = describe_place(tied[0], "Maria", "en", SWIM.preset)
    assert line == "Maria first: 0 h this week, Level 3, same pool, speaks Spanish like 6 of 8 families"
    assert "speaks" not in describe_place(tied[1], "Ann", "en", SWIM.preset)
    # one more hour of work this week puts the speaker behind: hours come first (product queue order)
    busy = ctx(S2=[booked("X", local(2026, 11, 8, 10, 0), 60)])
    ordered = rank([assess(a, shift, AT, SWIM, busy), assess(b, shift, AT, SWIM, busy)])
    assert [r.candidate.staff_id for r in ordered] == ["S1", "S2"]
    assert ordered[0].above_next["factor"] == "week_hours"


def test_the_working_language_is_not_called_out_and_old_candidates_still_rank():
    english = GroupRef("G1", "learn_to_swim", "3", 8, None, (("en", 7), ("es", 1)))
    first = rank([assess(_speaker("S1", {"en"}), lesson(groups=(english,)), AT, SWIM, ctx())])[0]
    assert first.place()["family_language"] == {"language": "en", "families": 7, "of": 8, "speaks": True}
    assert describe_place(first, "Ann", "en", SWIM.preset) == "Ann first: 0 h this week, Level 3, same pool"
    plain = Candidate("S9", Decision.of([]), 1.0, True, 0.0, True, 10, LEVEL_3)  # built without language facts
    assert rank([plain])[0].place()["family_language"] is None


def test_staff_languages_are_part_of_the_person():
    assert Staff("S1", "A", frozenset()).languages == frozenset()
