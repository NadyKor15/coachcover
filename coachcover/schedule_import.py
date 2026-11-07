"""The school's own schedule file into the database, then the rules over it.

We don't replace the schedule; we add the certification layer on top of it. The file (generic CSV or iCal) is read
by coachcover.importers, every good row is saved as a shift (an assigned row as `scheduled`, a row with nobody or
with a name we do not know as `open`, so the night check sees the gap), and every assigned row is checked against
the rules over the imported schedule plus the shifts already stored. A row the rules block is still saved: it is
the school's schedule, and the night check keeps flagging it until someone fixes it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

import psycopg
from psycopg import errors as pg_errors

from coachcover.importers import ImportResult, check_import, import_context, read_csv, read_ics, to_shifts
from coachcover.model import Commitment
from coachcover.repo import load_policy, load_staff, load_venues

MAX_IMPORT_BYTES = 2 * 1024 * 1024
ICAL_SUFFIXES = (".ics", ".ical", ".ifb")


def read_schedule(data: bytes, filename: str, *, tz: str, date_order: str = "MDY",
                  window: tuple[date, date] | None = None) -> ImportResult:
    """CSV or iCal, by the file name or the first line."""
    head = data.lstrip(b"\xef\xbb\xbf \r\n\t")[:15].upper()
    if filename.lower().endswith(ICAL_SUFFIXES) or head.startswith(b"BEGIN:VCALENDAR"):
        return read_ics(data, tz=tz, window=window)
    return read_csv(data, tz=tz, date_order=date_order)


def import_schedule(
    conn: psycopg.Connection,
    policy_id: str,
    data: bytes,
    filename: str,
    now: datetime,
    *,
    tz: str | None = None,
    date_order: str = "MDY",
    default_venue: str | None = None,
    window: tuple[date, date] | None = None,
) -> dict:
    """Read, save, check. Bad rows are named and skipped; good rows are saved and checked at `now`.

    Re-importing the same file saves nothing new (the shift ids are stable). A row that would book a person twice
    at the same time is not saved (the database refuses double bookings) and is listed in `not_saved`; the check
    names the overlap."""
    policy = load_policy(conn, policy_id)
    venue_rows = conn.execute("SELECT id, name, tz FROM venue WHERE policy_id = %s ORDER BY id", (policy_id,)).fetchall()
    if tz is None:
        zones = {row[2] for row in venue_rows}
        if len(zones) != 1:
            raise ValueError("give tz: the venues are not in one time zone")
        tz = zones.pop()
    if default_venue is not None and default_venue not in {row[0] for row in venue_rows}:
        raise ValueError(f"unknown venue {default_venue!r}")
    result = read_schedule(data, filename, tz=tz, date_order=date_order, window=window)
    staff = load_staff(conn, policy_id)
    shifts, errors, warnings = to_shifts(result, policy.preset, venues={name: vid for vid, name, _ in venue_rows},
                                         staff=staff, default_venue=default_venue)
    bad_lines = {e.line for e in errors}
    lines = [item.line for item in result.shifts if item.line not in bad_lines]
    if len(lines) != len(shifts):
        raise RuntimeError("imported rows and shifts are out of step")
    line_of = {sh.id: line for sh, line in zip(shifts, lines)}
    names = {s.id: s.name for s in staff}

    others: dict[str, list[Commitment]] = defaultdict(list)
    if shifts:
        window_from = min(s.start for s in shifts) - timedelta(days=8)
        window_to = max(s.end for s in shifts) + timedelta(days=8)
        for sid, ref, start, end in conn.execute(
            "SELECT s.staff_id, s.id, s.starts_at, s.ends_at FROM shift s JOIN venue v ON v.id = s.venue_id"
            " WHERE v.policy_id = %s AND s.status = 'scheduled' AND s.starts_at < %s AND s.ends_at > %s"
            " AND NOT (s.id = ANY(%s)) ORDER BY s.staff_id, s.starts_at, s.id",
            (policy_id, window_to, window_from, [s.id for s in shifts]),
        ):
            others[sid].append(Commitment(ref, "shift", start, end))
    context = import_context(shifts, load_venues(conn, policy_id), others)
    checks = check_import(shifts, staff, policy, context, now)

    role_ids = dict(conn.execute(
        "SELECT r.code, r.id FROM role r JOIN policy p ON p.preset_code = r.preset_code WHERE p.id = %s", (policy_id,)
    ).fetchall())
    saved, already, opened, not_saved = 0, 0, 0, []
    with conn.transaction():
        for sh in shifts:
            try:
                with conn.transaction():
                    cur = conn.execute(
                        "INSERT INTO shift (id, venue_id, role_id, starts_at, ends_at, staff_id, status)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (sh.id, sh.venue, role_ids[sh.role], sh.start, sh.end, sh.assignee,
                         "scheduled" if sh.assignee else "open"),
                    )
            except pg_errors.ExclusionViolation:
                not_saved.append({"line": line_of[sh.id], "shift_id": sh.id, "staff_id": sh.assignee,
                                  "name": names.get(sh.assignee),
                                  "reason": "the person already has another shift at that time"})
                continue
            if cur.rowcount:
                saved += 1
                opened += sh.assignee is None
            else:
                already += 1
    refused = {row["shift_id"] for row in not_saved}
    for row in checks:
        row.update(line=line_of[row["shift_id"]], name=names.get(row["staff_id"]), saved=row["shift_id"] not in refused)
    return {
        "format": result.format,
        "rows": len(result.shifts),
        "people": len(result.people),
        "duplicates": result.duplicates,
        "errors": [str(e) for e in (*result.errors, *errors)],
        "warnings": [str(w) for w in (*result.warnings, *warnings)],
        "saved": saved,
        "already_there": already,
        "open": opened,
        "not_saved": not_saved,
        "checked_at": now.isoformat(),
        "checked": len(checks),
        "blocked": sum(not row["ok"] for row in checks),
        "checks": checks,
    }
