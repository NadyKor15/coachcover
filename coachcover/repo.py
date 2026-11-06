"""Read rows from Postgres into the plain data types the rules work on. Read-only."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import psycopg

from coachcover.model import Commitment, Context, GroupRef, Policy, Shift, Staff, StaffCert, StaffProgram, Venue
from coachcover.presets import preset_from_dict


def load_policy(conn: psycopg.Connection, policy_id: str) -> Policy:
    """The policy row on top of its preset; the preset is rebuilt from the installed preset file."""
    row = conn.execute(
        "SELECT s.source, p.max_weekly_hours, p.week_start, p.makeup_window_days, p.autonomy,"
        " p.offer_timeout_minutes, p.overtime_cost_per_hour, p.default_remedy"
        " FROM policy p JOIN preset s ON s.code = p.preset_code WHERE p.id = %s",
        (policy_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown policy {policy_id!r}")
    source, hours, week_start, makeup, autonomy, timeout, overtime_cost, remedy = row
    return Policy(
        preset_from_dict(source), float(hours), week_start, makeup, dict(autonomy), timeout, float(overtime_cost), remedy
    )


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
    # the assignee is the person on the shift, or, once it is open, the person who called out
    row = conn.execute(
        "SELECT s.venue_id, r.code, s.starts_at, s.ends_at, s.instructors, s.zone_id, s.extra_certs,"
        " coalesce(s.staff_id, (SELECT c.absent_staff_id FROM cover_case c WHERE c.shift_id = s.id"
        "  ORDER BY c.opened_at DESC, c.id DESC LIMIT 1))"
        " FROM shift s JOIN role r ON r.id = s.role_id WHERE s.id = %s",
        (shift_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown shift {shift_id!r}")
    venue, role, start, end, instructors, zone, extra_certs, assignee = row
    groups = tuple(
        GroupRef(gid, prog, lv, size, age_min)
        for gid, prog, lv, size, age_min in conn.execute(
            "SELECT g.id, p.code, l.code, g.size, g.age_min FROM shift_group sg JOIN lesson_group g ON g.id = sg.group_id"
            " JOIN program_level l ON l.id = g.level_id JOIN program p ON p.id = l.program_id"
            " WHERE sg.shift_id = %s ORDER BY g.id",
            (shift_id,),
        )
    )
    return Shift(shift_id, venue, role, start, end, groups, instructors, zone, assignee, extra_certs=tuple(extra_certs))


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
