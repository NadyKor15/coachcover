"""Import a schedule from the tool a business already uses: shifts and staff from CSV, shifts from iCal.

We don't replace your schedule — we add the certification layer on top of it.

The formats are named for what they are:

- "generic CSV": one row per shift with date, start, end, location, position,
  employee and e-mail. Header names are matched loosely (`COLUMNS`). The words
  When I Work uses for its objects (Schedule, Job Site, Position, First Name,
  Last Name, Email) are accepted as synonyms, but When I Work does not document
  the columns of its schedule export (checked 2026-11-06), so this is not called
  a When I Work importer. A CSV without start and end is read as a staff list.
- "generic iCal": RFC 5545 VEVENTs: DTSTART with DTEND or DURATION, TZID, UTC and
  floating times, RRULE with FREQ=DAILY or WEEKLY (INTERVAL, COUNT, UNTIL, BYDAY,
  WKST), EXDATE, RECURRENCE-ID, STATUS:CANCELLED. Position = CATEGORIES, else
  SUMMARY; place = LOCATION; person = each ATTENDEE (CN and mailto). Anything else
  in an RRULE is an error, not a guess.

No network. A bad row becomes a `RowError` with its line number and the good rows
still import; a file that cannot be read at all raises `ImportFormatError`.
`to_shifts` maps the rows onto the policy's roles, venues and staff, and
`check_import` runs `rules.eligible` on every assigned imported shift.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coachcover.model import Commitment, Context, Policy, Preset, Shift, Staff, Venue
from coachcover.rules import eligible

GENERIC_CSV = "generic CSV"
GENERIC_ICAL = "generic iCal"
MAX_SHIFT_HOURS = 24
MAX_OCCURRENCES = 5000


class ImportFormatError(ValueError):
    """The file as a whole cannot be read (no header, unknown columns, not iCalendar...)."""


@dataclass(frozen=True)
class RowError:
    line: int
    field: str | None
    message: str

    def __str__(self) -> str:
        return f"line {self.line}" + (f", {self.field}" if self.field else "") + f": {self.message}"


@dataclass(frozen=True)
class ImportedShift:
    line: int
    start: datetime
    end: datetime
    location: str | None
    position: str | None
    employee: str | None
    email: str | None
    uid: str | None = None

    def key(self) -> tuple:
        who = (self.email or "").lower() or _norm(self.employee or "")
        return (self.start.astimezone(UTC), self.end.astimezone(UTC), _norm(self.location or ""),
                _norm(self.position or ""), who)


@dataclass(frozen=True)
class ImportedPerson:
    line: int
    name: str | None
    email: str | None
    positions: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    format: str
    shifts: tuple[ImportedShift, ...]
    people: tuple[ImportedPerson, ...]
    errors: tuple[RowError, ...] = ()
    warnings: tuple[RowError, ...] = ()
    duplicates: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict:
        return {
            "format": self.format,
            "shifts": len(self.shifts),
            "people": len(self.people),
            "duplicates": self.duplicates,
            "errors": [str(e) for e in self.errors],
            "warnings": [str(w) for w in self.warnings],
        }


# --- small parsers -------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_WEEKDAY_PREFIX = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+", re.IGNORECASE)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError(f"unknown time zone {name!r} (use an IANA name such as America/New_York)") from None


def parse_date(text: str, order: str = "MDY") -> date:
    """2026-11-09, 11/9/2026, 11/9/26, 9.11.2026 (order DMY), Nov 9 2026, 9 Nov 2026, with an optional weekday."""
    raw = text
    text = _WEEKDAY_PREFIX.sub("", text.strip())
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})", text):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        if order == "MDY":
            mo, d = a, b
        elif order == "DMY":
            d, mo = a, b
        else:
            raise ValueError("date order must be MDY or DMY")
    elif m := re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", text):
        mo, d, y = _MONTHS.get(m.group(1)[:3].lower()), int(m.group(2)), int(m.group(3))
    elif m := re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", text):
        d, mo, y = int(m.group(1)), _MONTHS.get(m.group(2)[:3].lower()), int(m.group(3))
    else:
        raise ValueError(f"cannot read the date {raw!r}")
    if mo is None:
        raise ValueError(f"cannot read the month in {raw!r}")
    try:
        return date(y, mo, d)
    except ValueError:
        raise ValueError(f"no such date {raw!r}") from None


def parse_time(text: str) -> time:
    """06:00, 6:00, 6:00:00, 6:00 AM, 6am, 6:00p, 18:15, noon, midnight."""
    raw = text
    text = text.strip().lower().replace(".", "")
    if text == "noon":
        return time(12, 0)
    if text == "midnight":
        return time(0, 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm|a|p)?", text)
    if not m:
        raise ValueError(f"cannot read the time {raw!r}")
    h, mi, s, half = int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), m.group(4)
    if half:
        if not 1 <= h <= 12:
            raise ValueError(f"cannot read the time {raw!r}: {h} is not a 12-hour clock hour")
        h = (h % 12) + (12 if half.startswith("p") else 0)
    elif m.group(2) is None:
        raise ValueError(f"cannot read the time {raw!r}: give minutes or am/pm")
    if h > 23 or mi > 59 or s > 59:
        raise ValueError(f"no such time {raw!r}")
    return time(h, mi, s)


def _split_moment(text: str, order: str) -> tuple[date | None, time | None, datetime | None]:
    """A cell that may hold a date, a time, both, or an ISO date-time with an offset."""
    text = text.strip()
    iso = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})?", text)
    if iso:
        day, clock, offset = date.fromisoformat(iso.group(1)), parse_time(iso.group(2)), iso.group(3)
        if offset:
            fixed = datetime.fromisoformat(f"{iso.group(1)}T{clock.isoformat()}{'+00:00' if offset == 'Z' else offset}")
            return day, clock, fixed
        return day, clock, None
    parts = text.rsplit(" ", 2)
    for cut in (2, 1):  # "11/9/2026 6:00 AM" or "11/9/2026 18:00"
        if len(parts) > cut:
            head, tail = " ".join(parts[:-cut]), " ".join(parts[-cut:])
            try:
                return parse_date(head, order), parse_time(tail), None
            except ValueError:
                pass
    try:
        return None, parse_time(text), None
    except ValueError:
        pass
    try:
        return parse_date(text, order), None, None
    except ValueError:
        raise ValueError(f"cannot read {text!r} as a time, a date or a date and time") from None


def localize(day: date, clock: time, tz: ZoneInfo) -> tuple[datetime, str | None]:
    """Wall time in a zone. Error for a time that does not exist (clocks jump forward); a warning for an
    ambiguous one (clocks go back): the first, daylight-time reading is taken."""
    value = datetime.combine(day, clock, tzinfo=tz)
    back = value.astimezone(UTC).astimezone(tz)
    if back.replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError(f"{day} {clock:%H:%M} does not exist in {tz.key} (the clocks jump forward)")
    if value.replace(fold=1).utcoffset() != value.utcoffset():
        return value, f"{day} {clock:%H:%M} happens twice in {tz.key} (the clocks go back); took the first"
    return value, None


# --- CSV ------------------------------------------------------------------------------------

COLUMNS: dict[str, tuple[str, ...]] = {
    "date": ("date", "shift date", "day", "start date"),
    "end_date": ("end date",),
    "start": ("start", "start time", "shift start", "shift start time", "begin", "from", "time in"),
    "end": ("end", "end time", "shift end", "shift end time", "finish", "to", "until", "time out"),
    "location": ("location", "job site", "site", "venue", "place", "schedule", "branch"),
    "position": ("position", "role", "job", "job title"),
    "employee": ("employee", "employee name", "name", "staff", "staff member", "user", "full name"),
    "first_name": ("first name", "first"),
    "last_name": ("last name", "last"),
    "email": ("email", "e mail", "email address", "e mail address"),
    "timezone": ("timezone", "time zone", "tz"),
}


def _decode(source: str | bytes | Path) -> tuple[str, list[RowError]]:
    if isinstance(source, Path):
        source = source.read_bytes()
    if isinstance(source, str):
        return source.lstrip("﻿"), []
    try:
        return source.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        return source.decode("cp1252", errors="replace"), [RowError(0, None, "not UTF-8; read as Windows-1252")]


def _header_map(header: Sequence[str]) -> dict[str, int]:
    normalized = [_norm(h or "") for h in header]
    seen: dict[str, int] = {}
    for i, name in enumerate(normalized):
        if name and name in seen:
            raise ImportFormatError(f"the column {header[i]!r} appears twice in the header")
        seen[name] = i
    found: dict[str, int] = {}
    for key, aliases in COLUMNS.items():
        for alias in aliases:
            if alias in seen and seen[alias] not in found.values():
                found[key] = seen[alias]
                break
    return found


def read_csv(source: str | bytes | Path, *, tz: str, date_order: str = "MDY") -> ImportResult:
    """Shifts (or, without start and end columns, a staff list) from a generic CSV."""
    default_tz = zone(tz)
    if date_order not in ("MDY", "DMY"):
        raise ValueError("date_order must be MDY or DMY")
    text, warnings = _decode(source)
    first = next((line for line in text.splitlines() if line.strip()), None)
    if first is None:
        raise ImportFormatError("the file is empty")
    delimiter = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = None
    for header in reader:
        if any(cell.strip() for cell in header):
            break
    cols = _header_map(header)
    has_person = "employee" in cols or "email" in cols or ("first_name" in cols and "last_name" in cols)
    if "start" in cols and "end" in cols:
        mode = "shifts"
    elif has_person:
        mode = "people"
    else:
        raise ImportFormatError(
            "no shift columns (start, end) and no person columns (name or first and last name, email); "
            f"found {[h for h in header if h.strip()]}"
        )
    errors: list[RowError] = []
    shifts: list[ImportedShift] = []
    rows_people: list[ImportedPerson] = []
    seen: dict[tuple, int] = {}
    duplicates = 0
    width = len(header)
    for row in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in row):
            continue
        if len(row) > width and any(cell.strip() for cell in row[width:]):
            warnings.append(RowError(line, None, f"{len(row) - width} cell(s) past the last column ignored"))

        def cell(key: str) -> str | None:
            i = cols.get(key)
            return _clean(row[i]) if i is not None and i < len(row) else None

        name = cell("employee")
        if name is None and (cell("first_name") or cell("last_name")):
            name = " ".join(x for x in (cell("first_name"), cell("last_name")) if x)
        email = cell("email")
        if email is not None and not _EMAIL.match(email):
            errors.append(RowError(line, "email", f"{email!r} is not an e-mail address; row skipped"))
            continue
        if mode == "people":
            if name is None and email is None:
                errors.append(RowError(line, "employee", "no name and no e-mail; row skipped"))
                continue
            rows_people.append(ImportedPerson(line, name, email, tuple(x for x in (cell("position"),) if x),
                                              tuple(x for x in (cell("location"),) if x)))
            continue
        try:
            item, note = _csv_shift(line, cell, default_tz, date_order, name, email)
        except ValueError as exc:
            field_name, _, message = str(exc).partition("|")
            errors.append(RowError(line, field_name or None, message or str(exc)))
            continue
        if note:
            warnings.append(RowError(line, None, note))
        key = item.key()
        if key in seen:
            duplicates += 1
            warnings.append(RowError(line, None, f"same shift as line {seen[key]}; skipped"))
            continue
        seen[key] = line
        if item.employee is None and item.email is None:
            warnings.append(RowError(line, "employee", "nobody on this shift (open shift)"))
        shifts.append(item)
    people = _people(shifts, rows_people, warnings)
    return ImportResult(GENERIC_CSV, tuple(shifts), people, tuple(errors), tuple(warnings), duplicates)


def _csv_shift(line, cell, default_tz: ZoneInfo, order: str, name, email) -> tuple[ImportedShift, str | None]:
    def fail(field_name: str, message: str) -> ValueError:
        return ValueError(f"{field_name}|{message}")

    tz = default_tz
    if cell("timezone"):
        try:
            tz = zone(cell("timezone"))
        except ValueError as exc:
            raise fail("timezone", str(exc)) from None
    raw_start, raw_end = cell("start"), cell("end")
    if raw_start is None:
        raise fail("start", "empty")
    if raw_end is None:
        raise fail("end", "empty")
    day = None
    if cell("date"):
        try:
            day = parse_date(cell("date"), order)
        except ValueError as exc:
            raise fail("date", str(exc)) from None
    try:
        s_day, s_clock, s_fixed = _split_moment(raw_start, order)
    except ValueError as exc:
        raise fail("start", str(exc)) from None
    try:
        e_day, e_clock, e_fixed = _split_moment(raw_end, order)
    except ValueError as exc:
        raise fail("end", str(exc)) from None
    if s_clock is None:
        raise fail("start", f"no time in {raw_start!r}")
    if e_clock is None:
        raise fail("end", f"no time in {raw_end!r}")
    s_day = s_day or day
    if s_day is None:
        raise fail("date", "empty, and the start has no date")
    end_day_given = e_day is not None or cell("end_date") is not None
    if e_day is None and cell("end_date"):
        try:
            e_day = parse_date(cell("end_date"), order)
        except ValueError as exc:
            raise fail("end_date", str(exc)) from None
    notes = []
    if s_fixed is not None:
        start = s_fixed
    else:
        try:
            start, note = localize(s_day, s_clock, tz)
        except ValueError as exc:
            raise fail("start", str(exc)) from None
        notes.append(note)
    if e_fixed is not None:
        end = e_fixed
    else:
        end_day = e_day or s_day
        overnight = not end_day_given and s_fixed is None and e_clock <= s_clock
        if overnight:  # "22:00-06:00" on one date: the end is on the next day
            end_day += timedelta(days=1)
            notes.append("ends the next day (overnight shift)")
        try:
            end, note = localize(end_day, e_clock, tz)
        except ValueError as exc:
            raise fail("end", str(exc)) from None
        notes.append(note)
    if end.astimezone(UTC) <= start.astimezone(UTC):
        raise fail("end", "the shift ends before it starts")
    if end.astimezone(UTC) - start.astimezone(UTC) > timedelta(hours=MAX_SHIFT_HOURS):
        raise fail("end", f"longer than {MAX_SHIFT_HOURS} hours")
    item = ImportedShift(line, start, end, cell("location"), cell("position"), name, email)
    return item, "; ".join(n for n in notes if n) or None


def _people(shifts: Iterable[ImportedShift], rows: Iterable[ImportedPerson], warnings: list[RowError]) -> tuple[ImportedPerson, ...]:
    acc: dict[str, dict] = {}
    for p in [*rows, *(ImportedPerson(s.line, s.employee, s.email, tuple(x for x in (s.position,) if x),
                                       tuple(x for x in (s.location,) if x)) for s in shifts)]:
        if p.name is None and p.email is None:
            continue
        key = (p.email or "").lower() or "name:" + _norm(p.name or "")
        entry = acc.setdefault(key, {"line": p.line, "name": p.name, "email": p.email, "positions": {}, "locations": {}})
        if p.name and entry["name"] and _norm(p.name) != _norm(entry["name"]):
            warnings.append(RowError(p.line, "employee",
                                     f"{p.email} is {entry['name']!r} on line {entry['line']} and {p.name!r} here"))
        entry["name"] = entry["name"] or p.name
        entry["positions"].update(dict.fromkeys(p.positions))
        entry["locations"].update(dict.fromkeys(p.locations))
    return tuple(
        ImportedPerson(e["line"], e["name"], e["email"], tuple(e["positions"]), tuple(e["locations"]))
        for e in sorted(acc.values(), key=lambda e: e["line"])
    )


# --- iCal -----------------------------------------------------------------------------------

WINDOWS_ZONES = {  # TZID names some calendar programs write instead of IANA names
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "US Mountain Standard Time": "America/Phoenix",
    "Pacific Standard Time": "America/Los_Angeles",
    "Alaskan Standard Time": "America/Anchorage",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Russian Standard Time": "Europe/Moscow",
}
_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"}


@dataclass
class _Prop:
    name: str
    params: dict[str, str]
    value: str
    line: int


@dataclass
class _Event:
    line: int
    props: dict[str, list[_Prop]] = field(default_factory=lambda: defaultdict(list))

    def one(self, name: str) -> _Prop | None:
        items = self.props.get(name)
        return items[0] if items else None


def _unfold(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if raw[:1] in (" ", "\t") and out:
            out[-1] = (out[-1][0], out[-1][1] + raw[1:])
        elif raw.strip():
            out.append((n, raw))
    return out


def _content_line(line: int, text: str) -> _Prop:
    i, quoted = 0, False
    while i < len(text):
        ch = text[i]
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
        i += 1
    if i >= len(text):
        raise ImportFormatError(f"line {line}: no ':' in {text[:60]!r}")
    head, value = text[:i], text[i + 1:]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    if not parts:
        raise ImportFormatError(f"line {line}: no property name")
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return _Prop(parts[0].strip().upper(), params, value, line)


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _ical_zone(name: str) -> ZoneInfo:
    candidates = [name, WINDOWS_ZONES.get(name, "")]
    if "/" in name:
        candidates.append("/".join(name.strip("/").split("/")[-2:]))
    for candidate in candidates:
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    raise ValueError(f"unknown TZID {name!r}")


def _ical_moment(prop: _Prop, default_tz: ZoneInfo) -> tuple[datetime | None, ZoneInfo | None, str | None]:
    """(aware start, the zone its recurrences follow, note). None for an all-day DATE value."""
    value = prop.value.strip()
    if prop.params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return None, None, None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", value)
    if not m:
        raise ValueError(f"cannot read the date-time {value!r}")
    y, mo, d, h, mi, s = map(int, m.groups()[:6])
    try:
        day, clock = date(y, mo, d), time(h, mi, s)
    except ValueError:
        raise ValueError(f"no such date-time {value!r}") from None
    if m.group(7):
        return datetime.combine(day, clock, tzinfo=UTC), ZoneInfo("UTC"), None
    tz = _ical_zone(prop.params["TZID"]) if "TZID" in prop.params else default_tz
    moment, note = localize(day, clock, tz)
    return moment, tz, note


def _duration(text: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text.strip())
    if not m or text.strip() in ("P", "PT", "+P", "-P"):
        raise ValueError(f"cannot read the duration {text!r}")
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def _rrule(text: str) -> dict:
    parts = {}
    for chunk in text.strip().split(";"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.upper()] = value.upper()
    unknown = set(parts) - _RRULE_KEYS
    if unknown:
        raise ValueError(f"RRULE part(s) {sorted(unknown)} not supported")
    if parts.get("FREQ") not in ("DAILY", "WEEKLY"):
        raise ValueError(f"RRULE FREQ={parts.get('FREQ')} not supported (DAILY or WEEKLY)")
    rule = {"freq": parts["FREQ"], "interval": int(parts.get("INTERVAL", "1") or 1)}
    if rule["interval"] < 1:
        raise ValueError("RRULE INTERVAL must be >= 1")
    if "COUNT" in parts:
        rule["count"] = int(parts["COUNT"])
    if "UNTIL" in parts:
        u = parts["UNTIL"]
        if re.fullmatch(r"\d{8}", u):
            rule["until_day"] = date(int(u[:4]), int(u[4:6]), int(u[6:]))
        elif m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?", u):
            rule["until"] = datetime(*map(int, m.groups()), tzinfo=UTC)
        else:
            raise ValueError(f"cannot read RRULE UNTIL={u}")
    if "count" in rule and ("until" in rule or "until_day" in rule):
        raise ValueError("RRULE has both COUNT and UNTIL")
    if "BYDAY" in parts:
        days = parts["BYDAY"].split(",")
        bad = [x for x in days if x not in _DAYS]
        if bad:
            raise ValueError(f"RRULE BYDAY {bad} not supported (plain MO..SU only)")
        rule["byday"] = sorted({_DAYS.index(x) for x in days})
    rule["wkst"] = _DAYS.index(parts.get("WKST", "MO")) if parts.get("WKST", "MO") in _DAYS else 0
    return rule


def _occurrence_days(first: date, rule: dict, last_day: date | None) -> Iterable[date]:
    """Calendar days of the occurrences, in order, starting with the first (RFC 5545 for DAILY and WEEKLY)."""
    if rule["freq"] == "DAILY":
        k = 0
        while True:
            day = first + timedelta(days=k * rule["interval"])
            if last_day is not None and day > last_day:
                return
            yield day
            k += 1
    days = rule.get("byday") or [first.weekday()]
    week0 = first - timedelta(days=(first.weekday() - rule["wkst"]) % 7)
    w = 0
    while True:
        base = week0 + timedelta(weeks=w * rule["interval"])
        for wd in sorted(days, key=lambda x: (x - rule["wkst"]) % 7):
            day = base + timedelta(days=(wd - rule["wkst"]) % 7)
            if day < first:
                continue
            if last_day is not None and day > last_day:
                return
            yield day
        w += 1


def read_ics(source: str | bytes | Path, *, tz: str, window: tuple[date, date] | None = None) -> ImportResult:
    """Shifts from an iCalendar file. `window` (first day, last day) bounds repeating events and filters the rest;
    a repeating event without COUNT or UNTIL needs a window."""
    default_tz = zone(tz)
    text, warnings = _decode(source)
    lines = _unfold(text)
    if not lines or lines[0][1].strip().upper() != "BEGIN:VCALENDAR":
        raise ImportFormatError("not an iCalendar file (it must start with BEGIN:VCALENDAR)")
    events: list[_Event] = []
    stack: list[str] = []
    current: _Event | None = None
    for n, raw in lines:
        prop = _content_line(n, raw)
        if prop.name == "BEGIN":
            stack.append(prop.value.strip().upper())
            if stack[-1] == "VEVENT":
                current = _Event(n)
            continue
        if prop.name == "END":
            name = prop.value.strip().upper()
            if not stack or stack[-1] != name:
                raise ImportFormatError(f"line {n}: END:{name} without a matching BEGIN")
            stack.pop()
            if name == "VEVENT":
                events.append(current)
                current = None
            continue
        if stack == ["VCALENDAR"] and prop.name == "X-WR-TIMEZONE":
            try:
                default_tz = _ical_zone(prop.value.strip())
            except ValueError:
                warnings.append(RowError(n, "X-WR-TIMEZONE", f"unknown zone {prop.value!r}; using {default_tz.key}"))
        elif current is not None and stack[-1] == "VEVENT":
            current.props[prop.name].append(prop)
    if stack:
        raise ImportFormatError(f"the file ends inside {stack[-1]} (missing END)")
    win_from = win_to = None
    if window is not None:
        if window[1] < window[0]:
            raise ValueError("window: the last day is before the first")
        win_from = datetime.combine(window[0], time(0), tzinfo=default_tz)
        win_to = datetime.combine(window[1] + timedelta(days=1), time(0), tzinfo=default_tz)
    overrides: dict[str, set[datetime]] = defaultdict(set)
    for ev in events:
        rid, uid = ev.one("RECURRENCE-ID"), ev.one("UID")
        if rid is not None and uid is not None:
            try:
                moment, _, _ = _ical_moment(rid, default_tz)
                if moment is not None:
                    overrides[uid.value.strip()].add(moment.astimezone(UTC))
            except ValueError:
                pass  # reported when the event itself is read
    errors: list[RowError] = []
    shifts: list[ImportedShift] = []
    seen: dict[tuple, int] = {}
    duplicates = 0
    for ev in events:
        try:
            items, notes = _event_shifts(ev, default_tz, win_from, win_to, overrides)
        except ValueError as exc:
            errors.append(RowError(ev.line, "VEVENT", str(exc)))
            continue
        warnings.extend(RowError(ev.line, "VEVENT", note) for note in notes)
        for item in items:
            key = item.key()
            if key in seen:
                duplicates += 1
                warnings.append(RowError(ev.line, "VEVENT", f"same shift as the event on line {seen[key]}; skipped"))
                continue
            seen[key] = ev.line
            shifts.append(item)
    shifts.sort(key=lambda s: (s.start.astimezone(UTC), s.line, s.email or "", s.employee or ""))
    people = _people(shifts, (), warnings)
    return ImportResult(GENERIC_ICAL, tuple(shifts), people, tuple(errors), tuple(warnings), duplicates)


def _event_shifts(ev: _Event, default_tz: ZoneInfo, win_from, win_to, overrides) -> tuple[list[ImportedShift], list[str]]:
    notes: list[str] = []
    status = ev.one("STATUS")
    if status is not None and status.value.strip().upper() == "CANCELLED":
        return [], ["cancelled event skipped"]
    dtstart = ev.one("DTSTART")
    if dtstart is None:
        raise ValueError("no DTSTART")
    start, rule_tz, note = _ical_moment(dtstart, default_tz)
    if start is None:
        return [], ["all-day event skipped: not a shift"]
    notes.append(note)
    dtend, dur = ev.one("DTEND"), ev.one("DURATION")
    if dtend is not None:
        end, _, note = _ical_moment(dtend, default_tz)
        if end is None:
            raise ValueError("DTEND is a date but DTSTART is a date-time")
        notes.append(note)
        length = end.astimezone(UTC) - start.astimezone(UTC)
    elif dur is not None:
        length = _duration(dur.value)
    else:
        raise ValueError("no DTEND and no DURATION")
    if length <= timedelta(0):
        raise ValueError("the event ends before it starts")
    if length > timedelta(hours=MAX_SHIFT_HOURS):
        raise ValueError(f"longer than {MAX_SHIFT_HOURS} hours")
    uid_prop = ev.one("UID")
    uid = uid_prop.value.strip() if uid_prop else None
    starts = [start]
    rrule = ev.one("RRULE")
    if rrule is not None and ev.one("RECURRENCE-ID") is None:
        rule = _rrule(rrule.value)
        if "count" not in rule and "until" not in rule and "until_day" not in rule and win_to is None:
            raise ValueError("repeats forever (no COUNT or UNTIL); give a date window")
        local = start.astimezone(rule_tz)
        last_day = win_to.astimezone(rule_tz).date() if win_to is not None else None
        if "until_day" in rule:
            last_day = min(last_day, rule["until_day"]) if last_day else rule["until_day"]
        starts = []
        for k, day in enumerate(_occurrence_days(local.date(), rule, last_day)):
            if k >= MAX_OCCURRENCES:
                raise ValueError(f"more than {MAX_OCCURRENCES} occurrences; give a shorter date window")
            if "count" in rule and k >= rule["count"]:
                break
            try:
                moment, note = localize(day, local.timetz().replace(tzinfo=None), rule_tz)
            except ValueError as exc:
                notes.append(f"occurrence skipped: {exc}")
                continue
            notes.append(note)
            if "until" in rule and moment.astimezone(UTC) > rule["until"]:
                break
            starts.append(moment)
        excluded = set()
        for exdate in ev.props.get("EXDATE", ()):
            for value in exdate.value.split(","):
                moment, _, _ = _ical_moment(_Prop("EXDATE", exdate.params, value, exdate.line), rule_tz)
                if moment is not None:
                    excluded.add(moment.astimezone(UTC))
        moved = overrides.get(uid, set()) if uid else set()
        starts = [s for s in starts if s.astimezone(UTC) not in excluded and s.astimezone(UTC) not in moved]
    if win_from is not None:
        starts = [s for s in starts if s.astimezone(UTC) < win_to.astimezone(UTC)
                  and (s + length).astimezone(UTC) > win_from.astimezone(UTC)]
    categories = ev.one("CATEGORIES")
    summary = ev.one("SUMMARY")
    position = None
    if categories is not None:
        position = _clean(_unescape(re.split(r"(?<!\\),", categories.value)[0]))
    if position is None and summary is not None:
        position = _clean(_unescape(summary.value))
    location = _clean(_unescape(ev.one("LOCATION").value)) if ev.one("LOCATION") else None
    people = []
    for att in ev.props.get("ATTENDEE", ()):
        if att.params.get("ROLE", "").upper() == "NON-PARTICIPANT":
            continue
        value = att.value.strip()
        email = value[7:] if value.lower().startswith("mailto:") else None
        if email is not None and not _EMAIL.match(email):
            notes.append(f"attendee {value!r} has no usable e-mail")
            email = None
        people.append((_clean(att.params.get("CN")), email))
    if not people:
        people = [(None, None)]
        notes.append("nobody on this event (open shift)")
    out = []
    for s in starts:
        end = (s.astimezone(UTC) + length).astimezone(s.tzinfo)
        for name, email in people:
            out.append(ImportedShift(ev.line, s, end, location, position, name, email, uid))
    return out, [n for n in notes if n]


# --- onto the policy: roles, venues, staff; then the rules ------------------------------------


def _shift_id(item: ImportedShift) -> str:
    raw = json.dumps([x.isoformat() if isinstance(x, datetime) else x for x in item.key()], ensure_ascii=False)
    return "IMP-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def to_shifts(
    result: ImportResult,
    preset: Preset,
    *,
    venues: Mapping[str, str],
    staff: Sequence[Staff] = (),
    staff_emails: Mapping[str, str] | None = None,
    positions: Mapping[str, str] | None = None,
    default_venue: str | None = None,
) -> tuple[list[Shift], list[RowError], list[RowError]]:
    """Imported rows as rule shifts. Position -> role (by the `positions` map, else role code or label);
    location -> venue (by the `venues` map of names to ids, else `default_venue`); person -> staff id (by e-mail
    through `staff_emails`, else by exact name). Unknown people stay unassigned with a warning."""
    role_by = {_norm(k): v for k, v in (positions or {}).items()}
    for role in preset.roles.values():
        role_by.setdefault(_norm(role.code), role.code)
        role_by.setdefault(_norm(role.label), role.code)
    venue_by = {_norm(k): v for k, v in venues.items()}
    email_to = {k.lower(): v for k, v in (staff_emails or {}).items()}
    name_to = {_norm(s.name): s.id for s in staff}
    out, errors, warnings = [], [], []
    for item in result.shifts:
        role = role_by.get(_norm(item.position or ""))
        if role is None:
            known = sorted(r.label for r in preset.roles.values())
            errors.append(RowError(item.line, "position", f"{item.position!r} is not a role of this business {known}"))
            continue
        venue = venue_by.get(_norm(item.location)) if item.location else None
        if venue is None and default_venue is not None:
            if item.location:
                warnings.append(RowError(item.line, "location", f"{item.location!r} is not a known place; used {default_venue}"))
            venue = default_venue
        if venue is None:
            errors.append(RowError(item.line, "location", f"{item.location!r} is not a known place"))
            continue
        assignee = None
        if item.email and item.email.lower() in email_to:
            assignee = email_to[item.email.lower()]
        elif item.employee and _norm(item.employee) in name_to:
            assignee = name_to[_norm(item.employee)]
        elif item.employee or item.email:
            warnings.append(RowError(item.line, "employee",
                                     f"{item.employee or item.email} is not on the staff list; left unassigned"))
        out.append(Shift(_shift_id(item), venue, role, item.start, item.end, (), 1, None, assignee=assignee))
    return out, errors, warnings


def import_context(shifts: Iterable[Shift], venues: Mapping[str, Venue],
                   others: Mapping[str, Iterable[Commitment]] | None = None) -> Context:
    """Bookings from the imported schedule (plus any others), for the hours and overlap checks."""
    booked: dict[str, list[Commitment]] = defaultdict(list)
    for sid, items in (others or {}).items():
        booked[sid].extend(items)
    for sh in shifts:
        if sh.assignee is not None:
            booked[sh.assignee].append(Commitment(sh.id, "shift", sh.start, sh.end))
    return Context(venues, {k: tuple(v) for k, v in booked.items()})


def check_import(shifts: Sequence[Shift], staff: Sequence[Staff], policy: Policy, context: Context,
                 at: datetime) -> list[dict]:
    """The certification layer: the rules on every assigned imported shift, judged at `at`.

    Imported shifts carry no groups, so program, level and group-norm checks need the groups added later;
    roles, cards, weekly hours and double bookings are checked here."""
    people = {s.id: s for s in staff}
    rows = []
    for sh in sorted(shifts, key=lambda s: (s.start.astimezone(UTC), s.id)):
        if sh.assignee is None or sh.assignee not in people:
            continue
        decision = eligible(people[sh.assignee], _for_the_person_on_it(sh), at, policy, context)
        rows.append({
            "shift_id": sh.id,
            "staff_id": sh.assignee,
            "start": sh.start.isoformat(),
            "role": sh.role,
            "ok": decision.ok,
            "blocking": [r.code.value for r in decision.reasons if r.blocking],
            "warnings": [r.code.value for r in decision.reasons if not r.blocking],
        })
    return rows


def _for_the_person_on_it(shift: Shift) -> Shift:
    """The rules treat `assignee` as the absent person; an imported shift is judged for the person on it."""
    return Shift(shift.id, shift.venue, shift.role, shift.start, shift.end, shift.groups, shift.instructors,
                 shift.zone, assignee=None, merged_shift_ids=shift.merged_shift_ids)


# --- command line ---------------------------------------------------------------------------


def read_file(path: Path, *, tz: str, date_order: str = "MDY", window: tuple[date, date] | None = None) -> ImportResult:
    data = path.read_bytes()
    head = data.lstrip(b"\xef\xbb\xbf \r\n\t")[:15].upper()
    if path.suffix.lower() in (".ics", ".ical", ".ifb") or head.startswith(b"BEGIN:VCALENDAR"):
        return read_ics(data, tz=tz, window=window)
    return read_csv(data, tz=tz, date_order=date_order)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read a schedule file (generic CSV or iCal) and print what was found.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--tz", required=True, help="IANA time zone of the venue, e.g. America/New_York")
    parser.add_argument("--date-order", default="MDY", choices=("MDY", "DMY"))
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat)
    args = parser.parse_args(argv)
    if (args.date_from is None) != (args.date_to is None):
        parser.error("give --from and --to together")
    window = (args.date_from, args.date_to) if args.date_from else None
    try:
        result = read_file(args.file, tz=args.tz, date_order=args.date_order, window=window)
    except (ImportFormatError, ValueError) as exc:
        print(f"cannot import {args.file}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.summary(), ensure_ascii=False, indent=1))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
