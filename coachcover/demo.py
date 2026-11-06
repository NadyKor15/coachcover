"""Demo clock, school reset and the judge scenario.

The demo runs on a clock stored in the database, shared by the API and the
worker: now = base + (real time - set_at) * speed. Without a clock row, real
time is used. The judge scenario resets the fictional school to Monday 05:12
and plays one morning on recorded model answers, with the timeline compressed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from coachcover.models import ModelClient
from coachcover.pipeline import Pipeline
from coachcover.seed import NOW, POLICY_ID, load_school

VOICE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "audio" / "ru-callout.ogg"
_RESET_LOCK = 0x636F6163


def real_now() -> datetime:
    return datetime.now(UTC)


def clock_now(conn: psycopg.Connection) -> datetime:
    row = conn.execute("SELECT base, set_at, speed FROM demo_clock").fetchone()
    if row is None:
        return real_now()
    base, set_at, speed = row
    return base + (real_now() - set_at) * float(speed)


def set_clock(conn: psycopg.Connection, base: datetime, speed: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO demo_clock (id, base, set_at, speed) VALUES (true, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET base = excluded.base, set_at = excluded.set_at, speed = excluded.speed",
        (base, real_now(), speed),
    )


def reset_school(conn: psycopg.Connection) -> None:
    """Drop and rebuild the public schema with the training school. ops.* (judge limits) survives."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RESET_LOCK,))
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    load_school(conn)
    set_clock(conn, NOW)


def run_judge_scenario(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Monday 05:12: Marina's voice note; 06:00 is covered by the second person asked; nobody takes 07:00,
    so plan B is approved as recommended. Returns what the screens need to find the result."""
    p = Pipeline(conn, models, POLICY_ID)
    t = NOW
    callout = p.callout("S05", now=t, audio=VOICE.read_bytes(), filename=VOICE.name)
    cases = {c["shift_id"]: c["case_id"] for c in callout["cases"]}
    lane, kids = cases.get("L-1109-G001"), cases.get("L-1109-G004")
    timeline = []

    def pending(case_id):
        row = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
        return row if row else (None, None)

    # 06:00: the first person says no at 05:14, the second says yes at 05:18
    if lane:
        offer_id, staff_id = pending(lane)
        t = NOW + timedelta(minutes=2)
        p.respond(offer_id, False, t)
        timeline.append({"at": t, "case_id": lane, "staff_id": staff_id, "answer": "no"})
        offer_id, staff_id = pending(lane)
        t = NOW + timedelta(minutes=6)
        p.respond(offer_id, True, t)
        timeline.append({"at": t, "case_id": lane, "staff_id": staff_id, "answer": "yes"})
    # 07:00: everyone asked says no, one a minute, then plan B as recommended
    plan = None
    if kids:
        while True:
            offer_id, staff_id = pending(kids)
            if offer_id is None:
                break
            t += timedelta(minutes=1)
            p.respond(offer_id, False, t)
            timeline.append({"at": t, "case_id": kids, "staff_id": staff_id, "answer": "no"})
        row = conn.execute(
            "SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (kids,)
        ).fetchone()
        if row:
            t += timedelta(minutes=1)
            plan = p.approve_plan_b(row[0], row[1], t, by="judge")
    set_clock(conn, t)
    return {"callout": callout, "cases": cases, "timeline": timeline, "plan_b": plan, "ended_at": t}
