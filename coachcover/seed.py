"""The training school: a fictional, deterministic data set for demos and tests.

Fixed "today": 2026-11-09 05:12 at the venues (America/New_York), a Monday.
Three venues, 40 staff, 120 weekly groups, and one policy week of shifts
(Sun 2026-11-08 .. Sat 2026-11-14): one lesson per group plus lifeguard
blocks at every venue every day.

Traps, all aimed at Monday morning so the 05:12 call-out meets them:
  expired:              S07 CPR/AED 2026-11-08 (yesterday), S10 WSI 2026-11-02, S27 Lifeguarding 2026-10-20
  expires on shift day: S08 CPR/AED 2026-11-09, S30 Lifeguarding 2026-11-09
  near the hour limit:  S11 39.5 h, S29 38.0 h (limit 40)
  readings disagree:    S13 WSI (reader A 2027-03-14, reader B 2026-03-14), nobody confirmed it
  program overdue:      S14 Learn-to-Swim renewed 2025-10-01, due 2026-10-01

Demo call-out: S05 Olga Petrova cannot come; her Monday 06:00 and 06:30 level-3
lessons at V1 need cover. A level-3 group of another instructor runs next to
each of them (06:00: 4 + 4 children, can merge at 1:8; 06:30: 5 + 4, cannot).

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
TZ_NAME = "America/New_York"
TZ = ZoneInfo(TZ_NAME)
TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 5, 12, tzinfo=TZ)
WEEK_START = date(2026, 11, 8)  # Sunday: the swim preset's week starts on Sunday
MONDAY = date(2026, 11, 9)
GREEDY_HOURS_CAP = 32.0  # ordinary staff stay well below the 40 h limit

VENUES = [
    ("V1", "Northside Aquatic Center", 33.950, -84.400, [("V1-LAP", "Lap pool"), ("V1-TEACH", "Teaching pool")]),
    ("V2", "Riverbend Swim Center", 33.800, -84.250, [("V2-MAIN", "Main pool"), ("V2-WARM", "Warm-water pool")]),
    ("V3", "Lakeview Natatorium", 33.700, -84.450, [("V3-COMP", "Competition pool"), ("V3-LEARN", "Learner pool")]),
]

# id, name, language, home venue, roles (I instructor, L lifeguard), learn-to-swim levels
STAFF = [
    ("S01", "Emma Carter", "en", "V1", "I", "1-3"),
    ("S02", "Sofía García", "es", "V2", "I", "1-4"),
    ("S03", "Valentina Castillo", "es", "V3", "I", "2-5"),
    ("S04", "Daniel Brooks", "en", "V1", "I", "3-6"),
    ("S05", "Olga Petrova", "ru", "V1", "I", "2-4"),
    ("S06", "Liam Foster", "en", "V2", "I", "1-6"),
    ("S07", "Javier Morales", "es", "V1", "I", "2-4"),
    ("S08", "Grace Sullivan", "en", "V1", "I", "1-3"),
    ("S09", "Lucas Reed", "en", "V3", "I", "4-6"),
    ("S10", "Dmitri Volkov", "ru", "V1", "I", "3-5"),
    ("S11", "Camila Herrera", "es", "V1", "IL", "2-4"),
    ("S12", "Noah Bennett", "en", "V1", "I", "1-6"),
    ("S13", "Anna Kuznetsova", "ru", "V2", "I", "3-6"),
    ("S14", "Irina Lebedeva", "ru", "V3", "I", "3-4"),
    ("S15", "Diego Navarro", "es", "V2", "I", "1-3"),
    ("S16", "Andrés Delgado", "es", "V3", "I", "4-6"),
    ("S17", "Maria Sokolova", "ru", "V1", "I", "1-6"),
    ("S18", "Chloe Hayes", "en", "V2", "I", "2-5"),
    ("S19", "Zoe Coleman", "en", "V3", "IL", "1-3"),
    ("S20", "Mateo Romero", "es", "V2", "IL", "3-6"),
    ("S21", "Pavel Orlov", "ru", "V3", "IL", "1-4"),
    ("S22", "Ethan Price", "en", "V2", "IL", "2-6"),
    ("S23", "Harper Hughes", "en", "V3", "IL", "1-6"),
    ("S24", "Gabriela Vargas", "es", "V3", "IL", "4-6"),
    ("S25", "Olivia Grant", "en", "V1", "L", ""),
    ("S26", "Rafael Ortiz", "es", "V1", "L", ""),
    ("S27", "Ivan Smirnov", "ru", "V1", "L", ""),
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
    ("S40", "Sofia Novikova", "ru", "V3", "L", ""),
]

ROLE_CODES = {"I": ("instructor",), "L": ("lifeguard",), "IL": ("instructor", "lifeguard")}
ROLE_CERTS = {"instructor": ("wsi", "cpr_aed"), "lifeguard": ("lifeguarding", "cpr_aed")}

# trap certificates: (staff, type) -> printed expiry
TRAP_EXPIRY = {
    ("S07", "cpr_aed"): date(2026, 11, 8),
    ("S10", "wsi"): date(2026, 11, 2),
    ("S27", "lifeguarding"): date(2026, 10, 20),
    ("S08", "cpr_aed"): date(2026, 11, 9),
    ("S30", "lifeguarding"): date(2026, 11, 9),
}
DISPUTED = ("S13", "wsi", date(2027, 3, 14), date(2026, 3, 14))  # staff, type, reader A expiry, reader B expiry
OVERDUE_PROGRAM = ("S14", date(2025, 10, 1))
DEMO_ABSENT = "S05"

# People kept free on Monday morning, so a trap is their only reason to be filtered out.
FREE_MONDAY_MORNING = {"S07", "S08", "S10", "S11", "S13", "S14", "S17", "S27", "S30"}

# lifeguard blocks every day at every venue: (start, hours)
GUARD_BLOCKS = [(time(5, 30), 4.5), (time(10, 0), 4.0), (time(14, 0), 4.0), (time(18, 0), 3.0)]

# Fixed schedules for the two people near the limit: (day offset from Sunday, venue, block start)
EDGE_BLOCKS = {
    "S11": [  # 39.5 h, nothing on Monday
        (0, "V2", time(5, 30)), (0, "V2", time(10, 0)),
        (2, "V2", time(5, 30)), (2, "V2", time(10, 0)),
        (3, "V2", time(5, 30)), (3, "V2", time(10, 0)),
        (4, "V2", time(10, 0)), (4, "V2", time(14, 0)),
        (5, "V2", time(18, 0)), (6, "V2", time(18, 0)),
    ],
    "S29": [  # 38.0 h, nothing on Monday
        (0, "V1", time(5, 30)), (0, "V1", time(10, 0)),
        (2, "V1", time(5, 30)), (2, "V1", time(10, 0)),
        (3, "V1", time(10, 0)), (3, "V1", time(14, 0)),
        (4, "V1", time(10, 0)), (4, "V1", time(18, 0)),
        (5, "V1", time(18, 0)), (6, "V1", time(18, 0)),
    ],
}

MORNING_TIMES = (time(6, 0), time(6, 30))  # two groups side by side, 30 min
AFTERNOON_TIMES = (time(16, 0), time(16, 45), time(17, 30), time(18, 15))  # one group, 45 min
LEVEL_PATTERN = (1, 2, 3, 3, 4, 2, 5, 3, 6, 1, 4, 3)
LANGUAGE_PATTERN = (["en"], ["en"], ["es"], ["en"], ["ru"], ["en"], ["es"], ["en", "es"], ["en"], ["ru", "en"])

# demo groups at V1 on Monday: (time, zone) -> (level, size, instructor or None for the greedy pick)
DEMO_GROUPS = {
    (time(6, 0), "V1-TEACH"): (3, 4, DEMO_ABSENT),
    (time(6, 0), "V1-LAP"): (3, 4, None),
    (time(6, 30), "V1-TEACH"): (3, 5, DEMO_ABSENT),
    (time(6, 30), "V1-LAP"): (3, 4, None),
}

READER_A_MODEL = "muse-glimmer-30b"
READER_B_MODEL = "nemotron-3-nano-omni"


@dataclass
class School:
    venues: list[tuple] = field(default_factory=list)  # id, name, tz, lat, lon
    zones: list[tuple] = field(default_factory=list)  # id, venue, name
    staff: list[tuple] = field(default_factory=list)  # id, name, language, home venue
    staff_roles: list[tuple] = field(default_factory=list)  # staff, role code
    certs: list[dict] = field(default_factory=list)
    programs: list[dict] = field(default_factory=list)  # staff, program, renewed_at, levels {code: since}
    groups: list[dict] = field(default_factory=list)
    shifts: list[dict] = field(default_factory=list)
    demo: dict = field(default_factory=dict)


def _levels(spec: str) -> list[str]:
    if not spec:
        return []
    low, high = (int(x) for x in spec.split("-"))
    return [str(n) for n in range(low, high + 1)]


def _at(day: date, start: time) -> datetime:
    return datetime.combine(day, start, tzinfo=TZ)


def _overlaps(busy: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    s, e = start.astimezone(UTC), end.astimezone(UTC)
    return any(b0.astimezone(UTC) < e and s < b1.astimezone(UTC) for b0, b1 in busy)


def _monday_morning(start: datetime) -> bool:
    return start.date() == MONDAY and start.time() < time(8, 0)


def _cert(staff_id: str, n: int, i: int, ctype: str) -> dict:
    expires = TRAP_EXPIRY.get((staff_id, ctype))
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
    for vid, name, lat, lon, zones in VENUES:
        school.venues.append((vid, name, TZ_NAME, lat, lon))
        school.zones.extend((zid, vid, zname) for zid, zname in zones)

    roles_of: dict[str, tuple[str, ...]] = {}
    levels_of: dict[str, list[str]] = {}
    home_of: dict[str, str] = {}
    for n, (sid, name, lang, home, roles, levels) in enumerate(STAFF, 1):
        school.staff.append((sid, name, lang, home))
        roles_of[sid], levels_of[sid], home_of[sid] = ROLE_CODES[roles], _levels(levels), home
        school.staff_roles.extend((sid, r) for r in ROLE_CODES[roles])
        needed = list(dict.fromkeys(c for r in ROLE_CODES[roles] for c in ROLE_CERTS[r]))
        school.certs.extend(_cert(sid, n, i, ctype) for i, ctype in enumerate(needed))
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

    busy: dict[str, list[tuple[datetime, datetime]]] = {sid: [] for sid in roles_of}
    hours: dict[str, float] = {sid: 0.0 for sid in roles_of}
    lesson_count: dict[str, int] = {sid: 0 for sid in roles_of}

    def book(sid: str, start: datetime, end: datetime) -> None:
        busy[sid].append((start, end))
        hours[sid] += (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 3600

    # groups, each with one lesson in the policy week
    slots = []
    for vid, _, _, _, zones in VENUES:
        for weekday in range(5):
            for start in MORNING_TIMES:
                slots.extend((vid, zid, weekday, start, 30) for zid, _ in zones)
        for weekday in range(5):
            for start in AFTERNOON_TIMES:
                slots.append((vid, zones[1][0], weekday, start, 45))
    for k, (vid, zid, weekday, start, minutes) in enumerate(slots):
        level, size, fixed = LEVEL_PATTERN[k % len(LEVEL_PATTERN)], None, None
        demo = DEMO_GROUPS.get((start, zid)) if vid == "V1" and weekday == 0 else None
        if demo:
            level, size, fixed = demo
        if size is None:
            size = 2 + k % 3 if level == 1 else 3 + (k * 5) % 6
        gid = f"G{k + 1:03d}"
        day = MONDAY + timedelta(days=weekday)
        lesson_start = _at(day, start)
        lesson_end = lesson_start + timedelta(minutes=minutes)
        if fixed is None:
            pool = [
                sid
                for sid, roles in roles_of.items()
                if "instructor" in roles
                and str(level) in levels_of[sid]
                and sid not in EDGE_BLOCKS
                and sid != DEMO_ABSENT
                and not (sid in FREE_MONDAY_MORNING and _monday_morning(lesson_start))
                and not _overlaps(busy[sid], lesson_start, lesson_end)
            ]
            if not pool:
                raise RuntimeError(f"no instructor for {gid}")
            fixed = min(pool, key=lambda s: (home_of[s] != vid, lesson_count[s], s))
        lesson_count[fixed] += 1
        book(fixed, lesson_start, lesson_end)
        school.groups.append(
            {
                "id": gid,
                "venue_id": vid,
                "zone_id": zid,
                "level": str(level),
                "name": f"Level {level}, {day:%a} {start:%H:%M}",
                "size": size,
                "family_languages": LANGUAGE_PATTERN[k % len(LANGUAGE_PATTERN)],
                "weekday": weekday,
                "starts_local": start,
                "minutes": minutes,
                "instructor_id": fixed,
            }
        )
        school.shifts.append(
            {
                "id": f"L-{day:%m%d}-{gid}",
                "venue_id": vid,
                "zone_id": zid,
                "role": "instructor",
                "starts_at": lesson_start,
                "ends_at": lesson_end,
                "staff_id": fixed,
                "groups": [gid],
            }
        )

    # lifeguard blocks: fixed ones for the two people near the limit, the rest by a greedy pick
    fixed_blocks = {}
    for sid, blocks in EDGE_BLOCKS.items():
        for offset, vid, start in blocks:
            fixed_blocks[(WEEK_START + timedelta(days=offset), vid, start)] = sid
    for offset in range(7):
        day = WEEK_START + timedelta(days=offset)
        for start, length in GUARD_BLOCKS:
            for vid, _, _, _, zones in VENUES:
                block_start = _at(day, start)
                block_end = block_start + timedelta(hours=length)
                sid = fixed_blocks.get((day, vid, start))
                if sid is None:
                    pool = [
                        s
                        for s, roles in roles_of.items()
                        if "lifeguard" in roles
                        and s not in EDGE_BLOCKS
                        and not (s in FREE_MONDAY_MORNING and _monday_morning(block_start))
                        and hours[s] + length <= GREEDY_HOURS_CAP
                        and not _overlaps(busy[s], block_start, block_end)
                    ]
                    if not pool:
                        raise RuntimeError(f"no lifeguard for {vid} {block_start}")
                    sid = min(pool, key=lambda s: (home_of[s] != vid, hours[s], s))
                book(sid, block_start, block_end)
                school.shifts.append(
                    {
                        "id": f"G-{day:%m%d}-{vid}-{start:%H%M}",
                        "venue_id": vid,
                        "zone_id": zones[0][0],
                        "role": "lifeguard",
                        "starts_at": block_start,
                        "ends_at": block_end,
                        "staff_id": sid,
                        "groups": [],
                    }
                )

    school.demo = {
        "now": NOW,
        "absent_staff_id": DEMO_ABSENT,
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
        insert_policy(conn, POLICY_ID, POLICY_NAME, default_policy(PRESET))
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
                "INSERT INTO staff (id, policy_id, full_name, language, home_venue_id) VALUES (%s, %s, %s, %s, %s)",
                [(sid, POLICY_ID, name, lang, home) for sid, name, lang, home in school.staff],
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
                "INSERT INTO lesson_group (id, venue_id, zone_id, level_id, name, size, family_languages,"
                " weekday, starts_local, minutes, instructor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        g["id"],
                        g["venue_id"],
                        g["zone_id"],
                        level_ids[("learn_to_swim", g["level"])],
                        g["name"],
                        g["size"],
                        g["family_languages"],
                        g["weekday"],
                        g["starts_local"],
                        g["minutes"],
                        g["instructor_id"],
                    )
                    for g in school.groups
                ],
            )
            cur.executemany(
                "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (s["id"], s["venue_id"], s["zone_id"], role_ids[s["role"]], s["starts_at"], s["ends_at"], s["staff_id"])
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
