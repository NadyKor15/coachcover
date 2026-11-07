"""The training school: a fictional, deterministic data set for demos and tests (API contract 2026-11-06).

Fixed "today": Monday 2026-11-09 05:12 at the venues (America/New_York).
Three venues, 40 staff (all lifeguards, 27 also WSI instructors), 120 weekly
groups, and one policy week of shifts (Sun 2026-11-08 .. Sat 2026-11-14): one
lesson per group plus lifeguard blocks at every venue every day.

The morning (matches the recorded Russian voice note "это Марина... в 6
дорожка и в 7 урок у малышей"): S05 Marina Petrova cannot come to her
06:00-08:00 shift L-1109-G002 at Riverside Aquatic Center: lanes 3-4 plus
Learn-to-Swim Level 3, 8 children aged 4-5 (norm 1:4, two adults on the post),
needs a lifeguard with WSI. Manager: Natalie Brooks.
  - S27 Anna Kim: free and level 3, but her lifeguard card expired 2026-11-05: blocked first.
  - Queue: S25 Sofia Reyes 6 h, S26 Leo Martins 9 h, S17 Zoe Grant 12 h (all V1),
    then S20 15 h, S22 16 h (V2), S24 18 h (V3). Everyone else is blocked by data.
  - Plan B: merging with G001 next door (6 children, 06:00-08:00) breaks 1:4; head coaches are in reserve;
    S11 could do it on overtime (39.5 h + 2 h).
  - Night check (Sunday evening): S07's CPR/AED expires 2026-11-08, before his Monday 16:00 lesson.

Other traps: S10 WSI expired 2026-11-02; S08 CPR/AED and S30 lifeguarding expire on Monday;
S11 39.5 h and S29 38 h near the limit; S13's WSI readings disagree; S14's program renewal is overdue.

Every name is invented. Run: python -m coachcover.seed --dsn postgresql://...
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coachcover.db import insert_policy, install_preset, migrate
from coachcover.presets import default_policy
from coachcover.rules import add_months

PRESET = "swim_school"
POLICY_ID = "demo-school"
POLICY_NAME = "Demo swim school (fictional)"
MANAGER = "Natalie Brooks"
TZ_NAME = "America/New_York"
TZ = ZoneInfo(TZ_NAME)
TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 5, 12, tzinfo=TZ)
WEEK_START = date(2026, 11, 9)  # Monday: the training school's week is Mon 9 - Sun 15, as on the week screen
WEEK_DAY = "monday"  # the school's policy week (the swim preset's default is Sunday)
MONDAY = date(2026, 11, 9)
GREEDY_HOURS_CAP = 32.0  # ordinary staff stay well below the 40 h limit
MIN_REST = timedelta(hours=10)  # the preset's min_rest_hours: the greedy fill never breaks it
HERO_SHIFT = "L-1109-G002"

# id, name, lat, lon, deck zone, zones in group order
VENUES = [
    ("V1", "Riverside Aquatic Center", 33.950, -84.400, "V1-LAP", [("V1-TEACH", "Teaching pool"), ("V1-LAP", "Lanes 3–4 lap swim")]),
    ("V2", "Maplewood Swim Center", 33.800, -84.250, "V2-MAIN", [("V2-WARM", "Warm-water pool"), ("V2-MAIN", "Main pool")]),
    ("V3", "Lakeview Natatorium", 33.700, -84.450, "V3-COMP", [("V3-LEARN", "Learner pool"), ("V3-COMP", "Competition pool")]),
]

# id, name, language, home venue, roles (L lifeguard, I lifeguard + instructor, IH + head coach), learn-to-swim levels
STAFF = [
    ("S01", "Emma Carter", "en", "V1", "I", "1-3"),
    ("S02", "Sofía García", "es", "V2", "I", "1-4"),
    ("S03", "Valentina Castillo", "es", "V3", "I", "2-5"),
    ("S04", "Daniel Shaw", "en", "V1", "I", "3-6"),
    ("S05", "Marina Petrova", "ru", "V1", "I", "1-4"),
    ("S06", "Liam Foster", "en", "V2", "IH", "1-6"),
    ("S07", "Javier Morales", "es", "V1", "I", "2-4"),
    ("S08", "Grace Sullivan", "en", "V1", "I", "1-3"),
    ("S09", "Lucas Reed", "en", "V3", "I", "4-6"),
    ("S10", "Dmitri Volkov", "ru", "V1", "I", "3-5"),
    ("S11", "Camila Herrera", "es", "V1", "I", "2-4"),
    ("S12", "Noah Bennett", "en", "V1", "IH", "1-6"),
    ("S13", "Elena Kuznetsova", "ru", "V2", "I", "3-6"),
    ("S14", "Irina Lebedeva", "ru", "V3", "I", "3-4"),
    ("S15", "Diego Navarro", "es", "V2", "I", "1-3"),
    ("S16", "Andrés Delgado", "es", "V3", "I", "4-6"),
    ("S17", "Zoe Grant", "es", "V1", "I", "1-6"),
    ("S18", "Chloe Hayes", "en", "V2", "I", "2-5"),
    ("S19", "Megan Coleman", "en", "V3", "I", "1-3"),
    ("S20", "Mateo Romero", "es", "V2", "I", "3-6"),
    ("S21", "Pavel Orlov", "ru", "V3", "I", "1-4"),
    ("S22", "Ethan Price", "en", "V2", "I", "2-6"),
    ("S23", "Harper Hughes", "en", "V3", "IH", "1-6"),
    ("S24", "Gabriela Vargas", "es", "V3", "I", "3-6"),
    ("S25", "Sofia Reyes", "es", "V1", "I", "2-4"),
    ("S26", "Leo Martins", "en", "V1", "I", "3-5"),
    ("S27", "Anna Kim", "en", "V1", "I", "2-4"),
    ("S28", "Ava Mitchell", "en", "V1", "L", ""),
    ("S29", "Lucía Ramos", "es", "V1", "L", ""),
    ("S30", "Nikita Morozov", "ru", "V1", "L", ""),
    ("S31", "Mason Turner", "en", "V2", "L", ""),
    ("S32", "Isabela Cruz", "es", "V2", "L", ""),
    ("S33", "Ekaterina Belova", "ru", "V2", "L", ""),
    ("S34", "Logan Pierce", "en", "V2", "L", ""),
    ("S35", "Carlos Mendoza", "es", "V2", "L", ""),
    ("S36", "Ella Morgan", "en", "V3", "L", ""),
    ("S37", "Andrei Kozlov", "ru", "V3", "L", ""),
    ("S38", "Paula Jiménez", "es", "V3", "L", ""),
    ("S39", "Caleb Ward", "en", "V3", "L", ""),
    ("S40", "Polina Novikova", "ru", "V3", "L", ""),
]

ROLE_CODES = {
    "L": ("lifeguard",),
    "I": ("lifeguard", "instructor"),
    "IH": ("lifeguard", "instructor", "head_coach"),
}
ROLE_CERTS = {"lifeguard": ("lifeguarding", "cpr_aed"), "instructor": ("wsi", "cpr_aed"), "head_coach": ("cpr_aed",)}

TRAP_EXPIRY = {
    ("S07", "cpr_aed"): date(2026, 11, 8),
    ("S10", "wsi"): date(2026, 11, 2),
    ("S27", "lifeguarding"): date(2026, 11, 5),
    ("S08", "cpr_aed"): date(2026, 11, 9),
    ("S30", "lifeguarding"): date(2026, 11, 9),
}
# not traps: still valid on Monday, for the 30/60-day radar
RADAR_EXPIRY = {
    ("S20", "cpr_aed"): date(2026, 11, 27),
    ("S33", "lifeguarding"): date(2026, 12, 4),
    ("S02", "wsi"): date(2026, 12, 28),
}
DISPUTED = ("S13", "wsi", date(2027, 3, 14), date(2026, 3, 14))  # staff, type, reader A expiry, reader B expiry
OVERDUE_PROGRAM = ("S14", date(2025, 10, 1))
# The issuer's registry is SIMULATED (training data, not a real issuer). Carlos Mendoza's lifeguard card is printed
# valid to 2028 but the issuer revoked it on Jul 24 (cards voided after the fact, as in Iowa in 2023).
REVOKED = {("S35", "lifeguarding"): date(2026, 7, 24)}
DEMO_ABSENT = "S05"

# Fixed weeks: (day offset from Sunday, venue, block start). Hours: Anna 4, Sofia 6, Leo 9, Zoe 12,
# S20 15, S22 16, S24 18, and the two people near the limit, S11 39.5 and S29 38. Nobody here works Monday morning.
FIXED_BLOCKS = {
    "S25": [(2, "V1", time(18, 0)), (3, "V1", time(18, 0))],
    "S26": [(3, "V1", time(5, 30)), (5, "V1", time(5, 30))],
    "S17": [(2, "V1", time(14, 0)), (4, "V1", time(14, 0)), (5, "V1", time(14, 0))],
    "S20": [(0, "V2", time(14, 0)), (1, "V2", time(10, 0)), (2, "V2", time(14, 0)), (3, "V2", time(18, 0))],
    "S22": [(1, "V2", time(14, 0)), (3, "V2", time(14, 0)), (5, "V2", time(10, 0)), (6, "V2", time(10, 0))],
    "S24": [(2, "V3", time(5, 30)), (4, "V3", time(5, 30)), (6, "V3", time(5, 30))],
    "S11": [
        (0, "V2", time(5, 30)), (0, "V2", time(10, 0)),
        (2, "V2", time(5, 30)), (2, "V2", time(10, 0)),
        (3, "V2", time(5, 30)), (3, "V2", time(10, 0)),
        (4, "V2", time(10, 0)), (4, "V2", time(14, 0)),
        (5, "V2", time(18, 0)), (0, "V2", time(18, 0)),  # Sunday evening, not Saturday: 10 h of rest before Sunday 05:30
    ],
    "S29": [
        (0, "V1", time(5, 30)), (0, "V1", time(10, 0)),
        (2, "V1", time(5, 30)), (2, "V1", time(10, 0)),
        (3, "V1", time(10, 0)), (3, "V1", time(14, 0)),
        (4, "V1", time(10, 0)), (4, "V1", time(18, 0)),
        (5, "V1", time(18, 0)), (0, "V1", time(18, 0)),  # Sunday evening, not Saturday: 10 h of rest before Sunday 05:30
    ],
}
FIXED_ONLY = set(FIXED_BLOCKS) | {DEMO_ABSENT}  # no greedy work at all
FREE_MONDAY = {"S07", "S10", "S13", "S14"}  # the night check meets only S07's fixed Monday lesson
RESERVE_EARLY = {"S06", "S12", "S23"}  # head coaches in reserve: free before 08:00 every day, for plan B

GUARD_BLOCKS = [(time(5, 30), 4.5), (time(10, 0), 4.0), (time(14, 0), 4.0), (time(18, 0), 3.0)]
MORNING_TIMES = (time(6, 0), time(7, 0))  # two groups side by side, 30 min unless fixed below
AFTERNOON_TIMES = (time(16, 0), time(16, 45), time(17, 30), time(18, 15))  # one group, 45 min
LEVEL_PATTERN = (1, 2, 3, 3, 4, 2, 5, 3, 6, 1, 4, 3)
LANGUAGE_PATTERN = (["en"], ["en"], ["es"], ["en"], ["ru"], ["en"], ["es"], ["en", "es"], ["en"], ["ru", "en"])


@dataclass(frozen=True)
class FixedGroup:
    level: int
    size: int
    ages: tuple[int, int]
    teacher: str
    languages: tuple[str, ...]
    minutes: int = 30
    deck: bool = False  # the post is deck and lesson: lifeguard role plus WSI
    instructors: int = 1


# Monday groups with fixed teachers: (venue, zone, weekday, start) -> group
FIXED_GROUPS = {
    ("V1", "V1-TEACH", 0, time(6, 0)): FixedGroup(4, 6, (8, 10), "S04", ("es",), minutes=120, deck=True),
    ("V1", "V1-LAP", 0, time(6, 0)): FixedGroup(3, 8, (4, 5), DEMO_ABSENT, ("ru", "en"), minutes=120, deck=True, instructors=2),
    ("V1", "V1-TEACH", 0, time(7, 0)): FixedGroup(1, 4, (4, 5), "S01", ("es", "en")),
    ("V1", "V1-LAP", 0, time(7, 0)): FixedGroup(3, 6, (6, 9), "S08", ("en", "es")),
    ("V2", "V2-WARM", 0, time(6, 0)): FixedGroup(2, 5, (6, 8), "S02", ("es",)),
    ("V2", "V2-MAIN", 0, time(6, 0)): FixedGroup(1, 3, (5, 6), "S15", ("en",)),
    ("V2", "V2-WARM", 0, time(7, 0)): FixedGroup(4, 6, (8, 11), "S18", ("en",)),
    ("V2", "V2-MAIN", 0, time(7, 0)): FixedGroup(3, 7, (7, 10), "S03", ("es", "en")),
    ("V3", "V3-LEARN", 0, time(6, 0)): FixedGroup(5, 6, (9, 12), "S09", ("en",)),
    ("V3", "V3-COMP", 0, time(6, 0)): FixedGroup(6, 6, (11, 15), "S16", ("es",)),
    ("V3", "V3-LEARN", 0, time(7, 0)): FixedGroup(2, 5, (6, 8), "S19", ("ru",)),
    ("V3", "V3-COMP", 0, time(7, 0)): FixedGroup(3, 7, (7, 10), "S21", ("ru", "en")),
    ("V1", "V1-TEACH", 0, time(16, 0)): FixedGroup(2, 5, (6, 8), "S07", ("es", "en"), minutes=45),
}

READER_A_MODEL = "muse-glimmer-30b"
READER_B_MODEL = "nemotron-3-nano-omni-30b-a3b-reasoning"


@dataclass
class School:
    venues: list[tuple] = field(default_factory=list)  # id, name, tz, lat, lon
    zones: list[tuple] = field(default_factory=list)  # id, venue, name
    staff: list[tuple] = field(default_factory=list)  # id, name, language, home venue
    registry: list[dict] = field(default_factory=list)  # simulated issuer registry: type, card number, revoked_on
    staff_roles: list[tuple] = field(default_factory=list)  # staff, role code
    certs: list[dict] = field(default_factory=list)
    programs: list[dict] = field(default_factory=list)  # staff, program, renewed_at, levels {code: since}
    groups: list[dict] = field(default_factory=list)
    shifts: list[dict] = field(default_factory=list)
    demo: dict = field(default_factory=dict)


def family_counts(languages: list[str], size: int) -> dict[str, int]:
    """Families per language: the first language has most of them, each other one a quarter of the group."""
    rest = {lang: size // 4 for lang in languages[1:]}
    return {languages[0]: size - sum(rest.values()), **rest}


def staff_languages(language: str) -> list[str]:
    """Languages a person speaks with families: their own and English (the school's working language)."""
    return list(dict.fromkeys((language, "en")))


def _levels(spec: str) -> list[str]:
    if not spec:
        return []
    low, high = (int(x) for x in spec.split("-"))
    return [str(n) for n in range(low, high + 1)]


# The evening before the week: Gabriela closes Lakeview on Sunday Nov 8 at 21:00, 9 h before the 06:00 hero shift.
EVE_BLOCKS = {"S24": [("V3", time(18, 0))]}


def block_day(offset: int) -> date:
    """FIXED_BLOCKS offsets: 0 = Sunday ... 6 = Saturday; the week runs Monday 9 to Sunday 15."""
    return WEEK_START + timedelta(days=(offset - 1) % 7)


def _at(day: date, start: time) -> datetime:
    return datetime.combine(day, start, tzinfo=TZ)


def _overlaps(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    s, e = start.astimezone(UTC), end.astimezone(UTC)
    return any(b0.astimezone(UTC) < e and s < b1.astimezone(UTC) for b0, b1 in busy)


def _short_rest(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """Work on another day that ends or starts less than MIN_REST away (the rule in coachcover.rules)."""
    s, e = start.astimezone(UTC), end.astimezone(UTC)
    day = start.astimezone(TZ).date()
    for b0, b1 in busy:
        other = b0.astimezone(TZ).date()
        if other < day and b1.astimezone(UTC) <= s and s - b1.astimezone(UTC) < MIN_REST:
            return True
        if other > day and b0.astimezone(UTC) >= e and b0.astimezone(UTC) - e < MIN_REST:
            return True
    return False


def _cleared(staff_id: str, roles: tuple[str, ...], extra: tuple[str, ...], day: date, lesson: bool,
             certs: dict[tuple[str, str], dict]) -> bool:
    """The data design keeps the week clean: the greedy fill never books a person whose card is expired, revoked or
    disputed on that day, or whose program renewal is overdue for a lesson. The traps live only where the rules
    must catch them: the hero morning's candidates, S07's Monday 16:00 lesson (the night check) and the import file."""
    for role in roles:
        for ctype in (*ROLE_CERTS[role], *extra):
            row = certs.get((staff_id, ctype))
            if row is None or row["expires_on"] < day or (staff_id, ctype) in REVOKED or (staff_id, ctype) == DISPUTED[:2]:
                return False
    return not (lesson and staff_id == OVERDUE_PROGRAM[0])


def _off(staff_id: str, start: datetime) -> bool:
    """Kept free by the data design: never picked by the greedy fill at this time."""
    if staff_id in FIXED_ONLY:
        return True
    if start.date() == MONDAY and staff_id in FREE_MONDAY:
        return True
    # head coaches in reserve stay free before 08:00 every day: plan B's reserve for the early lessons
    return start.time() < time(8, 0) and staff_id in RESERVE_EARLY


def _cert(staff_id: str, n: int, i: int, ctype: str) -> dict:
    expires = TRAP_EXPIRY.get((staff_id, ctype)) or RADAR_EXPIRY.get((staff_id, ctype))
    if expires is None:
        # Jan 2027 .. Oct 2028, so the issue date (24 months earlier) is never after today
        expires = date(2027 + (n + i) % 2, 1 + (n * 5 + i * 3) % 10, 1 + (n * 11 + i * 7) % 28)
    issued = add_months(expires, -24)
    number = f"{ctype.upper()}-{n:03d}{i}"
    fields = {"type": ctype, "card_number": number, "issued_on": issued.isoformat(), "expires_on": expires.isoformat()}
    read_at = datetime.combine(issued + timedelta(days=2), time(15, 0), tzinfo=UTC).isoformat()
    row = {
        "staff_id": staff_id,
        "cert_type": ctype,
        "card_number": number,
        "issued_on": issued,
        "expires_on": expires,
        "reader_a": None,
        "reader_b": None,
        "verified_by_human": None,
        "verified_at": None,
    }
    if (staff_id, ctype) == DISPUTED[:2]:
        a_exp, b_exp = DISPUTED[2], DISPUTED[3]
        row["expires_on"] = a_exp
        row["issued_on"] = add_months(a_exp, -24)
        fields_a = {**fields, "issued_on": row["issued_on"].isoformat(), "expires_on": a_exp.isoformat()}
        fields_b = {**fields_a, "expires_on": b_exp.isoformat()}
        row["reader_a"] = {"model": READER_A_MODEL, "fields": fields_a, "read_at": read_at}
        row["reader_b"] = {"model": READER_B_MODEL, "fields": fields_b, "read_at": read_at}
    elif n % 5 == 0:
        row["verified_by_human"] = "manager:demo"
        row["verified_at"] = datetime.combine(issued + timedelta(days=3), time(14, 0), tzinfo=UTC)
    else:
        row["reader_a"] = {"model": READER_A_MODEL, "fields": fields, "read_at": read_at}
        row["reader_b"] = {"model": READER_B_MODEL, "fields": fields, "read_at": read_at}
    return row


def build_school() -> School:
    """The whole data set as plain Python values. Same output on every call."""
    school = School()
    deck_zone = {}
    for vid, name, lat, lon, deck, zones in VENUES:
        school.venues.append((vid, name, TZ_NAME, lat, lon))
        school.zones.extend((zid, vid, zname) for zid, zname in zones)
        deck_zone[vid] = deck

    roles_of: dict[str, tuple[str, ...]] = {}
    levels_of: dict[str, list[str]] = {}
    home_of: dict[str, str] = {}
    for n, (sid, name, lang, home, roles, levels) in enumerate(STAFF, 1):
        school.staff.append((sid, name, lang, home))
        roles_of[sid], levels_of[sid], home_of[sid] = ROLE_CODES[roles], _levels(levels), home
        school.staff_roles.extend((sid, r) for r in ROLE_CODES[roles])
        needed = list(dict.fromkeys(c for r in ROLE_CODES[roles] for c in ROLE_CERTS[r]))
        school.certs.extend(_cert(sid, n, i, ctype) for i, ctype in enumerate(needed))
        for i, ctype in enumerate(needed):
            card = school.certs[-len(needed) + i]
            revoked_on = REVOKED.get((sid, ctype))
            school.registry.append({"cert_type": ctype, "card_number": card["card_number"], "revoked_on": revoked_on,
                                    "note": "simulated registry (training data)"})
        if levels_of[sid]:
            renewed = OVERDUE_PROGRAM[1] if sid == OVERDUE_PROGRAM[0] else date(2026, 1 + n % 9, 1 + (n * 7) % 28)
            base = date(2019 + n % 6, 1 + n % 12, 1 + n % 28)
            school.programs.append(
                {
                    "staff_id": sid,
                    "program": "learn_to_swim",
                    "renewed_at": renewed,
                    "levels": {lv: base + timedelta(days=90 * i) for i, lv in enumerate(levels_of[sid])},
                }
            )

    certs_by = {(c["staff_id"], c["cert_type"]): c for c in school.certs}
    busy: dict[str, list[tuple[datetime, datetime]]] = {sid: [] for sid in roles_of}
    hours: dict[str, float] = {sid: 0.0 for sid in roles_of}
    lesson_count: dict[str, int] = {sid: 0 for sid in roles_of}

    def book(sid: str, start: datetime, end: datetime) -> None:
        if _overlaps(busy[sid], start, end):
            raise RuntimeError(f"seed design error: {sid} booked twice at {start}")
        busy[sid].append((start, end))
        hours[sid] += (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600

    # fixed lifeguard blocks first, so the greedy fill works around them
    fixed_blocks = {}
    for sid, blocks in FIXED_BLOCKS.items():
        for offset, vid, start in blocks:
            key = (block_day(offset), vid, start)
            if key in fixed_blocks:
                raise RuntimeError(f"seed design error: block {key} given twice")
            fixed_blocks[key] = sid

    slots = []
    for vid, _, _, _, _, zones in VENUES:
        for weekday in range(5):
            for start in MORNING_TIMES:
                slots.extend((vid, zid, weekday, start) for zid, _ in zones)
        for weekday in range(5):
            for start in AFTERNOON_TIMES:
                slots.append((vid, zones[0][0], weekday, start))
    # fixed groups are placed before the greedy fill, so the greedy never takes their teachers' time
    order = sorted(range(len(slots)), key=lambda k: (slots[k][:2] + slots[k][2:3] + (slots[k][3],)) not in FIXED_GROUPS)
    placed: dict[int, dict] = {}
    for k in order:
        vid, zid, weekday, start = slots[k]
        fixed = FIXED_GROUPS.get((vid, zid, weekday, start))
        level = fixed.level if fixed else LEVEL_PATTERN[k % len(LEVEL_PATTERN)]
        size = fixed.size if fixed else (2 + k % 3 if level == 1 else 3 + (k * 5) % 6)
        minutes = fixed.minutes if fixed else (30 if start < time(12, 0) else 45)
        languages = list(fixed.languages) if fixed else LANGUAGE_PATTERN[k % len(LANGUAGE_PATTERN)]
        ages = fixed.ages if fixed else (4 + level, 6 + level)
        day = MONDAY + timedelta(days=weekday)
        lesson_start = _at(day, start)
        lesson_end = lesson_start + timedelta(minutes=minutes)
        teacher = fixed.teacher if fixed else None
        if teacher is not None and str(level) not in levels_of[teacher]:
            raise RuntimeError(f"seed design error: {teacher} is not cleared for level {level} ({slots[k]})")
        if teacher is None:
            candidates = [
                sid
                for sid, roles in roles_of.items()
                if "instructor" in roles
                and str(level) in levels_of[sid]
                and not _off(sid, lesson_start)
                and _cleared(sid, ("instructor",), (), lesson_start.date(), True, certs_by)
                and not _overlaps(busy[sid], lesson_start, lesson_end)
                and not _short_rest(busy[sid], lesson_start, lesson_end)
            ]
            if not candidates:
                raise RuntimeError(f"no instructor for slot {slots[k]}")
            teacher = min(candidates, key=lambda s: (home_of[s] != vid, lesson_count[s], s))
        lesson_count[teacher] += 1
        book(teacher, lesson_start, lesson_end)
        gid = f"G{k + 1:03d}"
        placed[k] = {
            "group": {
                "id": gid,
                "venue_id": vid,
                "zone_id": zid,
                "level": str(level),
                "name": f"Level {level}, {day:%a} {start:%H:%M}",
                "size": size,
                "age_min": ages[0],
                "age_max": ages[1],
                "family_languages": languages,
                "family_counts": family_counts(languages, size),
                "weekday": weekday,
                "starts_local": start,
                "minutes": minutes,
                "instructor_id": teacher,
            },
            "shift": {
                "id": f"L-{day:%m%d}-{gid}",
                "venue_id": vid,
                "zone_id": zid,
                "role": "lifeguard" if fixed and fixed.deck else "instructor",
                "extra_certs": ["wsi"] if fixed and fixed.deck else [],
                "instructors": fixed.instructors if fixed else 1,
                "starts_at": lesson_start,
                "ends_at": lesson_end,
                "staff_id": teacher,
                "groups": [gid],
            },
        }
    for k in range(len(slots)):
        school.groups.append(placed[k]["group"])
        school.shifts.append(placed[k]["shift"])

    eve = WEEK_START - timedelta(days=1)
    for sid, blocks in EVE_BLOCKS.items():
        for vid, start in blocks:
            length = dict(GUARD_BLOCKS)[start]
            block_start = _at(eve, start)
            book(sid, block_start, block_start + timedelta(hours=length))
            school.shifts.append({
                "id": f"G-{eve:%m%d}-{vid}-{start:%H%M}", "venue_id": vid, "zone_id": deck_zone[vid], "role": "lifeguard",
                "extra_certs": [], "instructors": 1, "starts_at": block_start,
                "ends_at": block_start + timedelta(hours=length), "staff_id": sid, "groups": [],
            })
    for offset in range(7):
        day = WEEK_START + timedelta(days=offset)
        for start, length in GUARD_BLOCKS:
            for vid, *_ in VENUES:
                block_start = _at(day, start)
                block_end = block_start + timedelta(hours=length)
                sid = fixed_blocks.get((day, vid, start))
                if sid is None:
                    candidates = [
                        s
                        for s, roles in roles_of.items()
                        if "lifeguard" in roles
                        and not _off(s, block_start)
                        and _cleared(s, ("lifeguard",), (), block_start.date(), False, certs_by)
                        and hours[s] + length <= GREEDY_HOURS_CAP
                        and not _overlaps(busy[s], block_start, block_end)
                        and not _short_rest(busy[s], block_start, block_end)
                    ]
                    if not candidates:
                        raise RuntimeError(f"no lifeguard for {vid} {block_start}")
                    sid = min(candidates, key=lambda s: (home_of[s] != vid, hours[s], s))
                book(sid, block_start, block_end)
                school.shifts.append(
                    {
                        "id": f"G-{day:%m%d}-{vid}-{start:%H%M}",
                        "venue_id": vid,
                        "zone_id": deck_zone[vid],
                        "role": "lifeguard",
                        "extra_certs": [],
                        "instructors": 1,
                        "starts_at": block_start,
                        "ends_at": block_end,
                        "staff_id": sid,
                        "groups": [],
                    }
                )

    school.demo = {
        "now": NOW,
        "absent_staff_id": DEMO_ABSENT,
        "hero_shift_id": HERO_SHIFT,
        "absent_shift_ids": [
            s["id"] for s in school.shifts if s["staff_id"] == DEMO_ABSENT and s["starts_at"].date() == MONDAY
        ],
        "hours": {sid: round(h, 2) for sid, h in sorted(hours.items())},
    }
    return school


def load_school(conn: psycopg.Connection, school: School | None = None) -> None:
    """Migrate, install the preset and write the school. Fails if the demo policy already exists."""
    school = school or build_school()
    migrate(conn)
    install_preset(conn, PRESET)
    with conn.transaction():
        insert_policy(conn, POLICY_ID, POLICY_NAME, default_policy(PRESET, week_start=WEEK_DAY), manager_name=MANAGER)
        role_ids = dict(conn.execute("SELECT code, id FROM role WHERE preset_code = %s", (PRESET,)).fetchall())
        cert_ids = dict(conn.execute("SELECT code, id FROM cert_type WHERE preset_code = %s", (PRESET,)).fetchall())
        program_ids = dict(conn.execute("SELECT code, id FROM program WHERE preset_code = %s", (PRESET,)).fetchall())
        level_ids = {
            (p, lv): lid
            for p, lv, lid in conn.execute(
                "SELECT p.code, l.code, l.id FROM program_level l JOIN program p ON p.id = l.program_id"
                " WHERE p.preset_code = %s",
                (PRESET,),
            )
        }
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO venue (id, policy_id, name, tz, lat, lon) VALUES (%s, %s, %s, %s, %s, %s)",
                [(v[0], POLICY_ID, v[1], v[2], v[3], v[4]) for v in school.venues],
            )
            cur.executemany("INSERT INTO zone (id, venue_id, name) VALUES (%s, %s, %s)", school.zones)
            cur.executemany(
                "INSERT INTO staff (id, policy_id, full_name, language, languages, home_venue_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [(sid, POLICY_ID, name, lang, staff_languages(lang), home) for sid, name, lang, home in school.staff],
            )
            cur.executemany(
                "INSERT INTO staff_role (staff_id, role_id) VALUES (%s, %s)",
                [(sid, role_ids[r]) for sid, r in school.staff_roles],
            )
            cur.executemany(
                "INSERT INTO staff_cert (staff_id, cert_type_id, card_number, issued_on, expires_on,"
                " reader_a, reader_b, verified_by_human, verified_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        c["staff_id"],
                        cert_ids[c["cert_type"]],
                        c["card_number"],
                        c["issued_on"],
                        c["expires_on"],
                        Jsonb(c["reader_a"]) if c["reader_a"] else None,
                        Jsonb(c["reader_b"]) if c["reader_b"] else None,
                        c["verified_by_human"],
                        c["verified_at"],
                    )
                    for c in school.certs
                ],
            )
            cur.executemany(
                "INSERT INTO issuer_registry (cert_type_id, card_number, revoked, revoked_on, note, simulated)"
                " VALUES (%s, %s, %s, %s, %s, true)",
                [(cert_ids[r["cert_type"]], r["card_number"], r["revoked_on"] is not None, r["revoked_on"], r["note"])
                 for r in school.registry],
            )
        for p in school.programs:
            pid = program_ids[p["program"]]
            sp_id = conn.execute(
                "INSERT INTO staff_program (staff_id, program_id, renewed_at) VALUES (%s, %s, %s) RETURNING id",
                (p["staff_id"], pid, p["renewed_at"]),
            ).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO staff_program_level (staff_program_id, program_id, level_id, since)"
                    " VALUES (%s, %s, %s, %s)",
                    [(sp_id, pid, level_ids[(p["program"], lv)], since) for lv, since in p["levels"].items()],
                )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO lesson_group (id, venue_id, zone_id, level_id, name, size, age_min, age_max, family_languages,"
                " family_counts, weekday, starts_local, minutes, instructor_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        g["id"],
                        g["venue_id"],
                        g["zone_id"],
                        level_ids[("learn_to_swim", g["level"])],
                        g["name"],
                        g["size"],
                        g["age_min"],
                        g["age_max"],
                        g["family_languages"],
                        Jsonb(g["family_counts"]),
                        g["weekday"],
                        g["starts_local"],
                        g["minutes"],
                        g["instructor_id"],
                    )
                    for g in school.groups
                ],
            )
            cur.executemany(
                "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id, instructors, extra_certs)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (s["id"], s["venue_id"], s["zone_id"], role_ids[s["role"]], s["starts_at"], s["ends_at"],
                     s["staff_id"], s["instructors"], s["extra_certs"])
                    for s in school.shifts
                ],
            )
            cur.executemany(
                "INSERT INTO shift_group (shift_id, group_id) VALUES (%s, %s)",
                [(s["id"], gid) for s in school.shifts for gid in s["groups"]],
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the schema and load the fictional training school.")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="Postgres DSN (default: $DATABASE_URL)")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("give --dsn or set DATABASE_URL")
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        load_school(conn)
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("venue", "staff", "lesson_group", "shift", "staff_cert")
        }
    print("loaded:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
