"""Read rows from Postgres into the plain data types the rules work on. Read-only."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import psycopg

from coachcover.model import (
    CertType,
    Commitment,
    Context,
    GroupRef,
    Level,
    Policy,
    Preset,
    Program,
    Role,
    Shift,
    Staff,
    StaffCert,
    StaffProgram,
    Venue,
)


def load_policy(conn: psycopg.Connection, policy_id: str) -> Policy:
    row = conn.execute(
        "SELECT p.preset_code, s.label, p.max_weekly_hours, p.week_start, p.makeup_window_days, p.autonomy"
        " FROM policy p JOIN preset s ON s.code = p.preset_code WHERE p.id = %s",
        (policy_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown policy {policy_id!r}")
    code, label, hours, week_start, makeup, autonomy = row
    cert_types = {
        c: CertType(c, lab, months)
        for c, lab, months in conn.execute(
            "SELECT code, label, validity_months FROM cert_type WHERE preset_code = %s ORDER BY id", (code,)
        )
    }
    required: dict[str, list[str]] = defaultdict(list)
    for role_code, cert_code in conn.execute(
        "SELECT r.code, c.code FROM role_cert rc JOIN role r ON r.id = rc.role_id"
        " JOIN cert_type c ON c.id = rc.cert_type_id WHERE r.preset_code = %s ORDER BY r.code, rc.ord",
        (code,),
    ):
        required[role_code].append(cert_code)
    roles = {
        r: Role(r, lab, tuple(required[r]))
        for r, lab in conn.execute("SELECT code, label FROM role WHERE preset_code = %s ORDER BY id", (code,))
    }
    levels: dict[int, dict[str, Level]] = defaultdict(dict)
    for program_id, lv, lab, per in conn.execute(
        "SELECT l.program_id, l.code, l.label, l.max_per_instructor FROM program_level l"
        " JOIN program p ON p.id = l.program_id WHERE p.preset_code = %s ORDER BY l.program_id, l.ord",
        (code,),
    ):
        levels[program_id][lv] = Level(lv, lab, per)
    programs = {
        p: Program(p, lab, months, levels[pid])
        for pid, p, lab, months in conn.execute(
            "SELECT id, code, label, renewal_months FROM program WHERE preset_code = %s ORDER BY id", (code,)
        )
    }
    preset = Preset(code, label, roles, cert_types, programs)
    return Policy(preset, float(hours), week_start, makeup, dict(autonomy))


def load_venues(conn: psycopg.Connection, policy_id: str) -> dict[str, Venue]:
    return {
        vid: Venue(vid, tz, lat, lon)
        for vid, tz, lat, lon in conn.execute(
            "SELECT id, tz, lat, lon FROM venue WHERE policy_id = %s ORDER BY id", (policy_id,)
        )
    }


def load_staff(conn: psycopg.Connection, policy_id: str) -> list[Staff]:
    """Active staff of the policy with roles, cards and programs, ordered by id."""
    roles: dict[str, set[str]] = defaultdict(set)
    for sid, role in conn.execute(
        "SELECT sr.staff_id, r.code FROM staff_role sr JOIN role r ON r.id = sr.role_id"
        " JOIN staff s ON s.id = sr.staff_id WHERE s.policy_id = %s",
        (policy_id,),
    ):
        roles[sid].add(role)
    certs: dict[str, list[StaffCert]] = defaultdict(list)
    for sid, ctype, issued, expires, review in conn.execute(
        "SELECT sc.staff_id, c.code, sc.issued_on, sc.expires_on, sc.needs_review FROM staff_cert sc"
        " JOIN cert_type c ON c.id = sc.cert_type_id JOIN staff s ON s.id = sc.staff_id"
        " WHERE s.policy_id = %s ORDER BY sc.staff_id, sc.id",
        (policy_id,),
    ):
        certs[sid].append(StaffCert(ctype, issued, expires, review))
    levels: dict[int, dict[str, object]] = defaultdict(dict)
    for sp_id, lv, since in conn.execute(
        "SELECT spl.staff_program_id, l.code, spl.since FROM staff_program_level spl"
        " JOIN program_level l ON l.id = spl.level_id JOIN staff_program sp ON sp.id = spl.staff_program_id"
        " JOIN staff s ON s.id = sp.staff_id WHERE s.policy_id = %s ORDER BY spl.staff_program_id, l.ord",
        (policy_id,),
    ):
        levels[sp_id][lv] = since
    programs: dict[str, dict[str, StaffProgram]] = defaultdict(dict)
    for sp_id, sid, prog, renewed in conn.execute(
        "SELECT sp.id, sp.staff_id, p.code, sp.renewed_at FROM staff_program sp JOIN program p ON p.id = sp.program_id"
        " JOIN staff s ON s.id = sp.staff_id WHERE s.policy_id = %s ORDER BY sp.id",
        (policy_id,),
    ):
        programs[sid][prog] = StaffProgram(prog, renewed, levels[sp_id])
    return [
        Staff(sid, name, frozenset(roles[sid]), tuple(certs[sid]), programs[sid], home)
        for sid, name, home in conn.execute(
            "SELECT id, full_name, home_venue_id FROM staff WHERE policy_id = %s AND active ORDER BY id", (policy_id,)
        )
    ]


def load_shift(conn: psycopg.Connection, shift_id: str) -> Shift:
    row = conn.execute(
        "SELECT s.venue_id, r.code, s.starts_at, s.ends_at, s.instructors, s.zone_id FROM shift s"
        " JOIN role r ON r.id = s.role_id WHERE s.id = %s",
        (shift_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown shift {shift_id!r}")
    venue, role, start, end, instructors, zone = row
    groups = tuple(
        GroupRef(gid, prog, lv, size)
        for gid, prog, lv, size in conn.execute(
            "SELECT g.id, p.code, l.code, g.size FROM shift_group sg JOIN lesson_group g ON g.id = sg.group_id"
            " JOIN program_level l ON l.id = g.level_id JOIN program p ON p.id = l.program_id"
            " WHERE sg.shift_id = %s ORDER BY g.id",
            (shift_id,),
        )
    )
    return Shift(shift_id, venue, role, start, end, groups, instructors, zone)


def load_context(conn: psycopg.Connection, policy_id: str, around: Shift) -> Context:
    """Venues plus every scheduled shift near `around` (any week the shift can touch)."""
    window_from, window_to = around.start - timedelta(days=8), around.end + timedelta(days=8)
    commitments: dict[str, list[Commitment]] = defaultdict(list)
    for sid, ref, start, end in conn.execute(
        "SELECT s.staff_id, s.id, s.starts_at, s.ends_at FROM shift s JOIN venue v ON v.id = s.venue_id"
        " WHERE v.policy_id = %s AND s.status = 'scheduled' AND s.starts_at < %s AND s.ends_at > %s"
        " ORDER BY s.staff_id, s.starts_at, s.id",
        (policy_id, window_to, window_from),
    ):
        commitments[sid].append(Commitment(ref, "shift", start, end))
    return Context(load_venues(conn, policy_id), {k: tuple(v) for k, v in commitments.items()})
