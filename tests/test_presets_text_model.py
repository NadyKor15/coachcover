"""Preset files, human text for every reason, and input validation of the data types."""

import copy
import re
from datetime import date, datetime
from pathlib import Path

import pytest

from coverage.model import Commitment, GroupRef, Shift, StaffCert, Venue
from coverage.presets import available, default_policy, load_preset, preset_from_dict, read_preset_file
from coverage.reasons_text import LANGUAGES, TEMPLATES, describe
from coverage.rules import Code, Decision, Reason, eligible
from tests.factory import AT, NY, SWIM, ctx, instructor, lesson, local

# --- presets ----------------------------------------------------------------


def test_both_presets_load_with_their_data():
    assert available() == ["fitness_club", "swim_school"]
    swim = load_preset("swim_school")
    levels = swim.programs["learn_to_swim"].levels
    assert list(levels) == ["1", "2", "3", "4", "5", "6"]
    assert levels["1"].max_per_instructor == 4
    assert {lv.max_per_instructor for code, lv in levels.items() if code != "1"} == {8}
    assert swim.roles["lifeguard"].required_certs == ("lifeguarding", "cpr_aed")
    assert swim.roles["instructor"].required_certs == ("wsi", "cpr_aed")
    fit = load_preset("fitness_club")
    assert fit.roles["group_instructor"].required_certs == ("trainer_cert", "cpr_aed")
    assert fit.programs["group_classes"].levels["hiit"].max_per_instructor == 20


def test_autonomy_is_policy_data_with_partial_overrides():
    assert SWIM.autonomy == {"assign_cover": "auto", "plan_b": "propose", "merge_groups": "propose", "overtime": "off"}
    bolder = default_policy("fitness_club", autonomy={"merge_groups": "auto"})
    assert bolder.autonomy["merge_groups"] == "auto" and bolder.autonomy["plan_b"] == "propose"
    with pytest.raises(ValueError, match="autonomy modes"):
        default_policy("swim_school", autonomy={"overtime": "sometimes"})
    with pytest.raises(ValueError, match="exactly"):
        default_policy("swim_school", autonomy={"fly": "auto"})


def test_policy_defaults_and_overrides():
    assert (SWIM.max_weekly_hours, SWIM.week_start, SWIM.makeup_window_days) == (40, 6, 30)
    custom = default_policy("swim_school", week_start="monday", max_weekly_hours=29)
    assert (custom.week_start, custom.max_weekly_hours) == (0, 29)
    assert default_policy("swim_school", week_start=2).week_start == 2
    with pytest.raises(ValueError, match="unknown policy settings"):
        default_policy("swim_school", overtime=True)
    with pytest.raises(ValueError):
        default_policy("swim_school", week_start="someday")
    with pytest.raises(ValueError):
        default_policy("no_such_preset")


@pytest.mark.parametrize(
    ("break_it", "message"),
    [
        (lambda d: d["roles"][0]["required_certs"].append("pilot_license"), "unknown certificate"),
        (lambda d: d["cert_types"].append(dict(d["cert_types"][0])), "duplicate code"),
        (lambda d: d["programs"][0]["levels"][0].update(max_per_instructor=0), "max_per_instructor"),
        (lambda d: d["cert_types"][0].update(validity_months=True), "validity_months"),
        (lambda d: d.update(colour="blue"), "unknown"),
        (lambda d: d["policy_defaults"].update(week_start="funday"), "week_start"),
        (lambda d: d["roles"][0].update(required_certs="cpr_aed"), "expected a list"),
        (lambda d: d["policy_defaults"]["autonomy"].pop("overtime"), "autonomy must set exactly"),
    ],
)
def test_broken_preset_is_rejected(break_it, message):
    data = copy.deepcopy(read_preset_file("swim_school"))
    break_it(data)
    with pytest.raises(ValueError, match=message):
        preset_from_dict(data)


# --- human text -------------------------------------------------------------

SAMPLE_PARAMS = {
    Code.ROLE_MISSING: {"role": "instructor"},
    Code.CERT_MISSING: {"cert_type": "wsi"},
    Code.CERT_NOT_YET_VALID: {"cert_type": "wsi", "valid_from": "2026-11-10"},
    Code.CERT_EXPIRED: {"cert_type": "cpr_aed", "expires_on": "2026-11-05"},
    Code.CERT_EXPIRES_DURING_SHIFT: {"cert_type": "lifeguarding", "expires_on": "2026-11-09", "valid_until": "2026-11-10T00:00:00-05:00"},
    Code.CERT_NEEDS_REVIEW: {"cert_type": "wsi", "expires_on": "2027-03-14"},
    Code.CERT_EXPIRES_ON_SHIFT_DAY: {"cert_type": "cpr_aed", "expires_on": "2026-11-09", "today": False},
    Code.PROGRAM_MISSING: {"program": "learn_to_swim"},
    Code.LEVEL_MISSING: {"program": "learn_to_swim", "level": "3"},
    Code.PROGRAM_RENEWAL_OVERDUE: {"program": "learn_to_swim", "renewed_at": "2025-10-01", "due_on": "2026-10-01"},
    Code.HOURS_LIMIT_EXCEEDED: {"week_start": "2026-11-08", "hours_before": 39.0, "shift_hours": 4.0, "hours_after": 43.0, "limit_hours": 40},
    Code.OVERLAPS_COMMITMENT: {"kind": "shift", "ref": "X", "start": "2026-11-09T06:00:00-05:00", "end": "2026-11-09T10:00:00-05:00"},
    Code.RATIO_EXCEEDED: {"participants": 9, "instructors": 1, "max_per_instructor": 8, "capacity": 8, "program": "learn_to_swim", "level": "3", "groups": ["G1"]},
}


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_code_has_text_in_every_language(lang):
    assert set(SAMPLE_PARAMS) == set(Code)
    preset = load_preset("swim_school")
    for code, params in SAMPLE_PARAMS.items():
        assert code in TEMPLATES[lang]
        for with_preset in (None, preset):
            text = describe(Reason(code, params), lang, with_preset)
            assert text and "{" not in text


def test_text_uses_labels_local_dates_and_today():
    preset = load_preset("swim_school")
    expired = Reason(Code.CERT_EXPIRED, SAMPLE_PARAMS[Code.CERT_EXPIRED])
    assert describe(expired, "ru", preset) == "CPR/AED истёк 05.11.2026"
    assert describe(expired, "en", preset) == "CPR/AED expired on 2026-11-05"
    today = Reason(Code.CERT_EXPIRES_ON_SHIFT_DAY, {**SAMPLE_PARAMS[Code.CERT_EXPIRES_ON_SHIFT_DAY], "today": True})
    assert describe(today, "ru", preset) == "CPR/AED истекает сегодня"
    level = Reason(Code.LEVEL_MISSING, SAMPLE_PARAMS[Code.LEVEL_MISSING])
    assert describe(level, "en", preset) == "not cleared for Learn-to-Swim, Level 3"
    with pytest.raises(ValueError):
        describe(expired, "de")


# --- data type validation ---------------------------------------------------


def test_naive_or_reversed_times_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Shift("X", "A", "lifeguard", datetime(2026, 11, 9, 6), datetime(2026, 11, 9, 7))
    with pytest.raises(ValueError, match="end must be after start"):
        Shift("X", "A", "lifeguard", local(2026, 11, 9, 7), local(2026, 11, 9, 6))
    with pytest.raises(ValueError, match="timezone-aware"):
        eligible(instructor(), lesson(), datetime(2026, 11, 9, 5, 12), SWIM, ctx())
    with pytest.raises(ValueError, match="end must be after start"):
        Commitment("C", "shift", local(2026, 11, 9, 7), local(2026, 11, 9, 7))


def test_end_after_start_is_judged_in_real_time_across_dst():
    # 01:30 EDT -> 01:10 EST is 40 real minutes later although the wall clock goes back
    shift = Shift("X", "A", "lifeguard", local(2026, 11, 1, 1, 30), local(2026, 11, 1, 1, 10, fold=1))
    assert shift.end.utcoffset() != shift.start.utcoffset()


def test_other_invalid_inputs_are_rejected():
    with pytest.raises(ValueError, match="time zone"):
        Venue("X", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="expires_on before issued_on"):
        StaffCert("wsi", date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(ValueError, match="size"):
        GroupRef("G", "learn_to_swim", "3", -1)
    with pytest.raises(ValueError, match="listed twice"):
        lesson(groups=(GroupRef("G", "learn_to_swim", "3", 2), GroupRef("G", "learn_to_swim", "3", 2)))
    with pytest.raises(ValueError, match="unknown venue"):
        eligible(instructor(), lesson(venue="ZZ"), AT, SWIM, ctx())
    with pytest.raises(ValueError, match="ok must be True exactly"):
        Decision(ok=True, reasons=(Reason(Code.ROLE_MISSING, {}),))


# --- the logic stays free of one industry's words ---------------------------

ROOT = Path(__file__).resolve().parents[1]
LOGIC_FILES = ["coverage/model.py", "coverage/rules.py", "coverage/presets.py", "coverage/reasons_text.py"]
INDUSTRY_WORDS = re.compile(r"swim|pool|lifeguard|aquatic|\bwsi\b|бассейн|плава|спасат", re.IGNORECASE)


def test_logic_files_have_no_industry_words():
    hits = []
    for rel in LOGIC_FILES:
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if INDUSTRY_WORDS.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []


def test_industry_word_guard_is_not_blind():
    assert INDUSTRY_WORDS.search('"code": "wsi"') and INDUSTRY_WORDS.search("Swim instructor")
    assert INDUSTRY_WORDS.search((ROOT / "coverage/presets/swim_school.json").read_text(encoding="utf-8"))
