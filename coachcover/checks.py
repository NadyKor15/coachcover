"""The night check: is tomorrow covered, and whose cards run out in 30 and 60 days.

Each scheduled shift of the day is checked against the person who holds it, at
the moment of the check. A problem is reported, not acted on: the report goes
to the manager's screen (opening cover cases at night is a product decision for
later).
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coachcover.pipeline import plain
from coachcover.reasons_text import describe
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import eligible


def night_report(conn: psycopg.Connection, policy_id: str, day: date, now: datetime) -> dict:
    policy = load_policy(conn, policy_id)
    staff = {s.id: s for s in load_staff(conn, policy_id)}
    tz_name = conn.execute("SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (policy_id,)).fetchone()[0]
    tz = ZoneInfo(tz_name)
    day_from = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    day_to = day_from + timedelta(days=1)
    rows = conn.execute(
        "SELECT s.id, s.staff_id, s.status FROM shift s JOIN venue v ON v.id = s.venue_id"
        " WHERE v.policy_id = %s AND s.status IN ('scheduled', 'open') AND s.starts_at >= %s AND s.starts_at < %s"
        " ORDER BY s.starts_at, s.id",
        (policy_id, day_from, day_to),
    ).fetchall()
    problems, warnings, uncovered = [], [], []
    checked = 0
    for shift_id, staff_id, status in rows:
        if status == "open" or staff_id is None:
            uncovered.append({"shift_id": shift_id})
            continue
        shift = dataclasses.replace(load_shift(conn, shift_id), assignee=None)  # the holder, not an absent person
        decision = eligible(staff[staff_id], shift, now, policy, load_context(conn, policy_id, shift))
        checked += 1
        entry = {
            "shift_id": shift_id,
            "staff_id": staff_id,
            "name": staff[staff_id].name,
            "start": shift.start,
            "reasons": [
                {"code": r.code.value, "text": describe(r, "en", policy.preset),
                 "params": {**dict(r.params), **({"cert_type_label": policy.preset.cert_types[r.params["cert_type"]].short
                                                    or policy.preset.cert_types[r.params["cert_type"]].label}
                                                   if "cert_type" in r.params else {})}}
                for r in decision.reasons
            ],
        }
        if not decision.ok:
            problems.append(entry)
        elif decision.reasons:
            warnings.append(entry)
    expiring = conn.execute(
        "SELECT sc.staff_id, s.full_name, c.code, c.label, sc.expires_on, sc.expires_on - %s AS days_left"
        " FROM staff_cert sc JOIN staff s ON s.id = sc.staff_id JOIN cert_type c ON c.id = sc.cert_type_id"
        " WHERE s.policy_id = %s AND s.active AND sc.expires_on BETWEEN %s AND %s"
        " AND NOT EXISTS (SELECT 1 FROM staff_cert newer WHERE newer.staff_id = sc.staff_id"
        "   AND newer.cert_type_id = sc.cert_type_id AND newer.expires_on > sc.expires_on)"
        " ORDER BY sc.expires_on, sc.staff_id",
        (day, policy_id, day, day + timedelta(days=60)),
    ).fetchall()
    radar = [
        {"staff_id": sid, "name": name, "cert_type": code, "label": label, "expires_on": exp, "days_left": left}
        for sid, name, code, label, exp, left in expiring
    ]
    return plain({
        "for_date": day,
        "checked_at": now,
        "shifts": len(rows),
        "covered_ok": checked - len(problems),
        "problems": problems,
        "warnings": warnings,
        "uncovered": uncovered,
        "expiring_30": [r for r in radar if r["days_left"] <= 30],
        "expiring_60": [r for r in radar if 30 < r["days_left"] <= 60],
    })


def store_night_report(conn: psycopg.Connection, policy_id: str, day: date, now: datetime) -> tuple[dict, bool]:
    """Write the report once per day; a second call (another worker, a restart) returns the stored one."""
    existing = conn.execute(
        "SELECT data FROM night_report WHERE policy_id = %s AND for_date = %s", (policy_id, day)
    ).fetchone()
    if existing:
        return existing[0], False
    report = night_report(conn, policy_id, day, now)
    inserted = conn.execute(
        "INSERT INTO night_report (policy_id, for_date, created_at, data) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (policy_id, for_date) DO NOTHING RETURNING data",
        (policy_id, day, now, Jsonb(report)),
    ).fetchone()
    if inserted is None:
        return conn.execute(
            "SELECT data FROM night_report WHERE policy_id = %s AND for_date = %s", (policy_id, day)
        ).fetchone()[0], False
    return report, True
