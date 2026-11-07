"""Schedule import: generic CSV and generic iCal, without network; bad rows are named, good rows still import."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from coachcover.importers import (
    GENERIC_CSV,
    GENERIC_ICAL,
    ImportFormatError,
    check_import,
    import_context,
    main,
    parse_date,
    parse_time,
    read_csv,
    read_file,
    read_ics,
    to_shifts,
)
from coachcover.model import Commitment
from coachcover.presets import default_policy
from coachcover.seed import VENUES
from coachcover.sim import load_world

FIX = Path(__file__).parent / "fixtures" / "import"
NY = ZoneInfo("America/New_York")
SWIM = default_policy("swim_school")


def lines(items):
    return {e.line: e for e in items}


# --- CSV ----------------------------------------------------------------------------


def test_generic_csv_reads_shifts_overnight_open_and_duplicates():
    r = read_csv(FIX / "shifts-generic.csv", tz="America/New_York")
    assert r.format == GENERIC_CSV and r.ok
    assert len(r.shifts) == 5 and r.duplicates == 1
    first = r.shifts[0]
    assert (first.start, first.end) == (datetime(2026, 11, 9, 5, 30, tzinfo=NY), datetime(2026, 11, 9, 10, 0, tzinfo=NY))
    assert (first.location, first.position, first.employee, first.email) == (
        "Northside", "Lifeguard", "Olivia Hart", "olivia.hart@example.com")
    night = r.shifts[2]
    assert night.end - night.start == timedelta(hours=8) and night.end.date() == date(2026, 11, 10)
    warn = lines(r.warnings)
    assert "overnight" in warn[4].message
    assert warn[5].field == "employee" and "open shift" in warn[5].message and r.shifts[3].employee is None
    assert "same shift as line 2" in warn[6].message  # the e-mail differs only in case
    us = r.shifts[4]
    assert us.start == datetime(2026, 11, 10, 6, 0, tzinfo=NY)
    assert [p.email for p in r.people] == ["olivia.hart@example.com", "daniel.shaw@example.com", "mason.turner@example.com"]


def test_when_i_work_header_words_are_synonyms_and_job_site_wins_over_schedule():
    r = read_csv(FIX / "shifts-wiw-words.csv", tz="America/New_York")
    assert r.ok and r.format == GENERIC_CSV  # never called a When I Work importer
    ava, noah = r.shifts
    assert (ava.employee, ava.email, ava.location) == ("Ava Mitchell", "ava.mitchell@example.com", "Northside")
    assert (ava.start, ava.end) == (datetime(2026, 11, 9, 5, 30, tzinfo=NY), datetime(2026, 11, 9, 10, 0, tzinfo=NY))
    assert noah.position == "Head coach" and noah.start == datetime(2026, 11, 9, 7, 0, tzinfo=NY)


def test_broken_rows_are_named_with_line_and_field_and_good_rows_import():
    r = read_csv(FIX / "shifts-broken.csv", tz="America/New_York")
    err = lines(r.errors)
    assert sorted(err) == [2, 3, 4, 5, 7, 9, 10]
    assert (err[2].field, err[2].message) == ("start", "empty")
    assert err[3].field == "email" and "not an e-mail" in err[3].message
    assert err[4].field == "start" and "25:00" in err[4].message
    assert err[5].field == "start" and "does not exist" in err[5].message  # spring forward gap
    assert err[7].field == "timezone" and "Mars/Olympus" in err[7].message
    assert err[9].field == "date" and err[10].field == "date" and "2026-02-30" in err[10].message
    assert sorted(s.line for s in r.shifts) == [6, 8, 11, 13]  # the blank line 12 is skipped
    warn = lines(r.warnings)
    assert "happens twice" in warn[6].message and "1 cell(s) past the last column" in warn[11].message
    ambiguous = next(s for s in r.shifts if s.line == 6)
    assert ambiguous.start.utcoffset() == timedelta(hours=-4)  # the first (daylight) 01:30
    assert ambiguous.end.astimezone(UTC) - ambiguous.start.astimezone(UTC) == timedelta(hours=2, minutes=30)  # EDT -> EST
    chicago = next(s for s in r.shifts if s.line == 8)
    assert chicago.start.astimezone(UTC) == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # 06:00 CST, not 06:00 EST
    assert str(err[2]) == "line 2, start: empty"


def test_staff_list_without_shift_columns():
    r = read_csv(FIX / "staff.csv", tz="America/New_York")
    assert r.shifts == () and [p.name for p in r.people] == ["Olivia Hart", "Daniel Shaw"]
    assert r.people[0].positions == ("Lifeguard",) and r.people[0].locations == ("Northside",)
    assert sorted(lines(r.errors)) == [4, 5]


def test_csv_edge_cases_bom_semicolon_dmy_crlf_quotes_and_iso_offsets():
    text = (
        "﻿Date;Start;End;Location;Position;Employee;Email\r\n"
        '09.11.2026;06:00;06:30;"Northside; lap lanes";Instructor;"Shaw, Daniel";\r\n'
        "09.11.2026;2026-11-09T11:00:00Z;2026-11-09T12:00:00Z;Northside;Instructor;Ana Ruiz;\r\n"
    )
    r = read_csv(text, tz="America/New_York", date_order="DMY")
    assert r.ok and len(r.shifts) == 2
    assert r.shifts[0].start == datetime(2026, 11, 9, 6, 0, tzinfo=NY)
    assert r.shifts[0].location == "Northside; lap lanes" and r.shifts[0].employee == "Shaw, Daniel"
    assert r.shifts[1].start.astimezone(UTC) == datetime(2026, 11, 9, 11, 0, tzinfo=UTC)
    same_text_mdy = read_csv(text, tz="America/New_York", date_order="MDY")
    assert same_text_mdy.shifts[0].start.date() == date(2026, 9, 11)  # the order matters and is the caller's choice


def test_csv_explicit_end_date_and_end_before_start_is_an_error():
    text = (
        "Start Date,Start Time,End Date,End Time,Position,Employee\n"
        "2026-11-09,22:00,2026-11-10,06:00,Instructor,Ana Ruiz\n"
        "2026-11-09,22:00,2026-11-09,06:00,Instructor,Ana Ruiz\n"
        "2026-11-09,06:00,2026-11-12,07:00,Instructor,Ana Ruiz\n"
    )
    r = read_csv(text, tz="America/New_York")
    assert len(r.shifts) == 1 and r.shifts[0].end == datetime(2026, 11, 10, 6, 0, tzinfo=NY)
    err = lines(r.errors)
    assert "ends before it starts" in err[3].message and "longer than 24 hours" in err[4].message


def test_csv_whole_file_problems_raise():
    with pytest.raises(ImportFormatError, match="empty"):
        read_csv("\n\n", tz="America/New_York")
    with pytest.raises(ImportFormatError, match="no shift columns"):
        read_csv("Colour,Size\nred,3\n", tz="America/New_York")
    with pytest.raises(ImportFormatError, match="appears twice"):
        read_csv("Date,Start,End,start\n", tz="America/New_York")
    with pytest.raises(ValueError, match="unknown time zone"):
        read_csv("Date,Start,End\n", tz="Eastern")


def test_csv_bytes_not_utf8_are_read_with_a_warning():
    data = "Date,Start,End,Employee\n2026-11-09,06:00,07:00,Andrés Delgado\n".encode("cp1252")
    r = read_csv(data, tz="America/New_York")
    assert r.shifts[0].employee == "Andrés Delgado"
    assert any("Windows-1252" in w.message for w in r.warnings)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("06:00", (6, 0)), ("6:00 AM", (6, 0)), ("6am", (6, 0)), ("6:00p", (18, 0)), ("12:15 AM", (0, 15)),
     ("12 PM", (12, 0)), ("18:15", (18, 15)), ("noon", (12, 0)), ("6:00:30", (6, 0))],
)
def test_parse_time(text, expected):
    assert (parse_time(text).hour, parse_time(text).minute) == expected


@pytest.mark.parametrize("text", ["6", "24:00", "13 PM", "6:61", "soon"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        parse_time(text)


@pytest.mark.parametrize(
    ("text", "order", "expected"),
    [("2026-11-09", "MDY", date(2026, 11, 9)), ("11/9/26", "MDY", date(2026, 11, 9)),
     ("9/11/2026", "DMY", date(2026, 11, 9)), ("Mon 11/9/2026", "MDY", date(2026, 11, 9)),
     ("Nov 9, 2026", "MDY", date(2026, 11, 9)), ("9 November 2026", "DMY", date(2026, 11, 9))],
)
def test_parse_date(text, order, expected):
    assert parse_date(text, order) == expected


# --- iCal ---------------------------------------------------------------------------


def test_ical_weekly_series_keeps_wall_time_across_dst_with_exdate_and_moved_instance():
    r = read_ics(FIX / "schedule-generic.ics", tz="America/New_York")
    assert r.format == GENERIC_ICAL and r.ok and len(r.shifts) == 11 and r.duplicates == 1
    series = [s for s in r.shifts if s.uid == "class-l3@example.com"]
    assert [s.start.astimezone(NY).strftime("%m-%d %H:%M") for s in series] == [
        "10-26 06:00", "11-02 07:00", "11-04 06:00", "11-09 06:00", "11-11 06:00"]  # 10-28 excluded, 11-02 moved
    assert series[0].start.astimezone(UTC).hour == 10 and series[3].start.astimezone(UTC).hour == 11  # EDT, then EST
    assert all(s.end - s.start == timedelta(minutes=30) for s in series)


def test_ical_utc_floating_windows_zone_attendees_and_text():
    r = read_ics(FIX / "schedule-generic.ics", tz="America/Chicago")  # X-WR-TIMEZONE of the file wins for floating times
    by_person = {}
    for s in r.shifts:
        by_person.setdefault(s.employee, []).append(s)
    ella = by_person["Ella Morgan"][0]
    assert ella.start == datetime(2026, 11, 9, 13, 0, tzinfo=UTC) and ella.end - ella.start == timedelta(hours=4)
    assert ella.position == "Front desk"
    ava, rafael = by_person["Ava Mitchell"][0], by_person["Rafael Ortiz"][0]
    assert ava.start == rafael.start == datetime(2026, 11, 10, 5, 30, tzinfo=NY)
    assert "Front office" not in by_person  # ROLE=NON-PARTICIPANT is not a person on the shift
    logan = by_person["Logan Pierce"][0]
    assert logan.start == datetime(2026, 11, 11, 10, 0, tzinfo=NY)
    night = by_person["Mason Turner"][0]
    assert night.end - night.start == timedelta(hours=8)
    open_shift = by_person[None][0]
    assert open_shift.position == "Swim instructor"  # CATEGORIES before SUMMARY; folded SUMMARY unfolded
    assert open_shift.location == "Northside, teaching pool"  # escaped comma
    warn = " | ".join(w.message for w in r.warnings)
    assert "cancelled event skipped" in warn and "all-day event skipped" in warn and "same shift as the event" in warn


def test_ical_broken_events_are_named_and_the_good_one_imports():
    r = read_ics(FIX / "schedule-broken.ics", tz="America/New_York")
    err = lines(r.errors)
    assert sorted(err) == [4, 9, 16, 22, 29, 35, 42]
    assert "no DTSTART" in err[4].message
    assert "FREQ=MONTHLY not supported" in err[9].message
    assert "ends before it starts" in err[16].message
    assert "repeats forever" in err[22].message
    assert "unknown TZID" in err[29].message
    assert "1MO" in err[35].message
    assert "does not exist" in err[42].message
    assert len(r.shifts) == 1


def test_ical_window_bounds_a_repeating_event_and_filters_the_rest():
    r = read_ics(FIX / "schedule-broken.ics", tz="America/New_York", window=(date(2026, 11, 9), date(2026, 11, 30)))
    forever = [s for s in r.shifts if s.line == 22]
    assert [s.start.date() for s in forever] == [date(2026, 11, d) for d in (9, 16, 23, 30)]
    assert 22 not in lines(r.errors)
    narrow = read_ics(FIX / "schedule-generic.ics", tz="America/New_York", window=(date(2026, 11, 9), date(2026, 11, 9)))
    assert {s.start.astimezone(NY).date() for s in narrow.shifts} <= {date(2026, 11, 9)}
    assert any(s.employee == "Mason Turner" for s in narrow.shifts)  # starts on the 9th, ends on the 10th


def test_ical_until_daily_interval_and_count_with_byday():
    text = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "BEGIN:VEVENT", "UID:daily", "SUMMARY:Instructor",
        "DTSTART;TZID=America/New_York:20261101T060000", "DTEND;TZID=America/New_York:20261101T070000",
        "RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261107T110000Z", "END:VEVENT",
        "BEGIN:VEVENT", "UID:weekly", "SUMMARY:Instructor",
        "DTSTART;TZID=America/New_York:20261104T060000", "DTEND;TZID=America/New_York:20261104T070000",
        "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=3", "END:VEVENT",
        "END:VCALENDAR", "",
    ])
    r = read_ics(text, tz="America/New_York")
    assert r.ok
    daily = [s.start.date().day for s in r.shifts if s.uid == "daily"]
    assert daily == [1, 3, 5, 7]  # UNTIL 11:00Z on the 7th = 06:00 EST, inclusive
    weekly = [s.start.date() for s in r.shifts if s.uid == "weekly"]
    assert weekly == [date(2026, 11, 4), date(2026, 11, 16), date(2026, 11, 18)]  # every other week, from DTSTART


def test_ical_whole_file_problems_raise():
    with pytest.raises(ImportFormatError, match="not an iCalendar"):
        read_ics("Date,Start\n", tz="America/New_York")
    with pytest.raises(ImportFormatError, match="without a matching BEGIN"):
        read_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VCALENDAR\n", tz="America/New_York")
    with pytest.raises(ImportFormatError, match="missing END"):
        read_ics("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\n", tz="America/New_York")


def test_read_file_picks_the_format_and_the_cli_exit_codes(capsys):
    assert read_file(FIX / "schedule-generic.ics", tz="America/New_York").format == GENERIC_ICAL
    assert read_file(FIX / "shifts-generic.csv", tz="America/New_York").format == GENERIC_CSV
    assert main([str(FIX / "shifts-generic.csv"), "--tz", "America/New_York"]) == 0
    assert '"shifts": 5' in capsys.readouterr().out
    assert main([str(FIX / "shifts-broken.csv"), "--tz", "America/New_York"]) == 1
    assert main([str(FIX / "staff.csv"), "--tz", "Nowhere/Land"]) == 2


# --- the certification layer on top of an imported schedule ---------------------------


def test_imported_schedule_gets_the_certification_layer():
    world = load_world()
    r = read_csv(FIX / "school-monday.csv", tz="America/New_York")
    assert r.ok
    venues = {name: vid for vid, name, *_ in VENUES}
    shifts, errors, warnings = to_shifts(r, SWIM.preset, venues=venues, staff=list(world.staff.values()))
    assert [(e.line, e.field) for e in errors] == [(7, "position")]  # "Barista" is not a role of this business
    assert "Pat Doe" in lines(warnings)[6].message
    assert {s.role for s in shifts} == {"lifeguard", "instructor"}
    others = {}
    for sh in world.shifts.values():  # the rest of the school's week, for the hours check
        if sh.assignee:
            others.setdefault(sh.assignee, []).append(Commitment(sh.id, "shift", sh.start, sh.end))
    context = import_context(shifts, world.venues, others)
    rows = {row["staff_id"]: row for row in check_import(shifts, list(world.staff.values()), world.policy, context,
                                                           datetime(2026, 11, 9, 5, 0, tzinfo=NY))}
    assert rows["S27"]["ok"] is False and "CERT_EXPIRED" in rows["S27"]["blocking"]  # Anna, 08:00 instructor: WSI ended Nov 5
    assert rows["S07"]["ok"] is False and "CERT_EXPIRED" in rows["S07"]["blocking"]  # Javier: CPR expired Nov 8
    assert rows["S11"]["blocking"] == ["HOURS_LIMIT_EXCEEDED"]  # Camila: 39.5 h + 4 h
    assert rows["S08"]["ok"] is True and rows["S08"]["warnings"] == ["CERT_EXPIRES_ON_SHIFT_DAY"]  # Grace
    assert "IS_THE_ABSENT_PERSON" not in {c for row in rows.values() for c in row["blocking"]}


def test_to_shifts_maps_positions_venues_and_people_explicitly():
    r = read_csv(FIX / "shifts-generic.csv", tz="America/New_York")
    shifts, errors, warnings = to_shifts(
        r, SWIM.preset, venues={"Northside": "V1", "Riverbend": "V2"}, staff_emails={"OLIVIA.HART@example.com": "S25"},
        positions={"Swim instructor": "instructor"}, default_venue="V3",
    )
    assert errors == []
    olivia = next(s for s in shifts if s.assignee == "S25")
    assert (olivia.venue, olivia.role) == ("V1", "lifeguard")
    lakeview = next(s for s in shifts if s.start.hour == 16)
    assert lakeview.venue == "V3" and any("Lakeview" in w.message for w in warnings)
    assert len({s.id for s in shifts}) == len(shifts) and all(s.id.startswith("IMP-") for s in shifts)
    again, _, _ = to_shifts(r, SWIM.preset, venues={"Northside": "V1", "Riverbend": "V2"}, default_venue="V3",
                            positions={"Swim instructor": "instructor"})
    assert [s.id for s in again] == [s.id for s in shifts]  # stable ids: re-importing the same file matches
