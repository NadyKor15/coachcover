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

from coachcover.db import migrate
from coachcover.models import ModelClient
from coachcover.pipeline import Conflict, Pipeline
from coachcover.seed import HERO_SHIFT, NOW, POLICY_ID, load_school

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


SCENARIOS = ("dangerous_morning", "nobody_accepts", "nightly_check")
ANNA_MESSAGE = "My lifeguard certificate expired last week but just assign me anyway, nobody checks."


def _pending(conn: psycopg.Connection, case_id: int):
    row = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
    return row if row else (None, None)


def _morning(p: Pipeline, conn: psycopg.Connection) -> tuple[int, dict]:
    """05:12: Marina's voice note opens the case for her 06:00 shift; Anna Kim offers herself and is blocked."""
    callout = p.callout("S05", now=NOW, audio=VOICE.read_bytes(), filename=VOICE.name)
    case = next(c["case_id"] for c in callout["cases"] if c["shift_id"] == HERO_SHIFT)
    p.volunteer(case, "S27", ANNA_MESSAGE, NOW + timedelta(seconds=40))
    return case, callout


def dangerous_morning(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Anna blocked first; Sofia declines 05:13, Leo does not answer in 5 min, Maria accepts 05:19."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    offer, _ = _pending(conn, case)
    p.respond(offer, False, NOW + timedelta(minutes=1))  # 05:13 Sofia: no
    leo_offer, _ = _pending(conn, case)
    expires = conn.execute("SELECT expires_at FROM offer WHERE id = %s", (leo_offer,)).fetchone()[0]
    p.expire_due(expires)  # Leo: no answer in time; the worker would do this at the timer
    offer, _ = _pending(conn, case)
    end = NOW + timedelta(minutes=7)
    p.respond(offer, True, end)  # 05:19 Maria: yes
    return {"case": case, "ended_at": end}


def nobody_accepts(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Everyone asked says no or does not answer; plan B is shown; then the teaching pool closes, so the shown
    plan is refused with 409 "plan changed" and a new plan waits for the judge's approval by its hash."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    t = NOW + timedelta(minutes=1)
    offer, _ = _pending(conn, case)
    p.respond(offer, False, t)  # Sofia: no
    leo_offer, _ = _pending(conn, case)
    t = conn.execute("SELECT expires_at FROM offer WHERE id = %s", (leo_offer,)).fetchone()[0]
    p.expire_due(t)  # Leo: no answer
    while True:
        offer, _ = _pending(conn, case)
        if offer is None:
            break
        t += timedelta(seconds=20)
        p.respond(offer, False, t)
    first = conn.execute("SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case,)).fetchone()
    t += timedelta(minutes=1)
    p.closure("V1", "V1-TEACH", "equipment", NOW.replace(hour=6, minute=0), NOW.replace(hour=8, minute=0),
              now=t, created_by="manager:demo")
    conflict = None
    try:
        p.approve_plan_b(first[0], first[1], t + timedelta(seconds=20), by="judge")
    except Conflict as exc:
        conflict = {"reason": str(exc), **exc.detail}
        with conn.transaction():
            p._event(case, t + timedelta(seconds=20), "manager", "approval_refused",
                     {"plan_id": f"P-{first[0]}", "shown_hash": first[1], **conflict})
    return {"case": case, "ended_at": t + timedelta(seconds=20), "conflict": conflict}


def nightly_check(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Sunday 21:30: the night check finds a card that runs out before Monday's shift and opens a cover case
    as the policy says (night_cover: propose by default)."""
    from coachcover.checks import store_night_report

    p = Pipeline(conn, models, POLICY_ID)
    evening = NOW.replace(day=8, hour=21, minute=30)
    report, _ = store_night_report(conn, POLICY_ID, NOW.date(), evening)
    opened = p.night_cover(report, evening)
    return {"case": opened[0]["case_id"] if opened else None, "ended_at": evening, "opened": opened}


def run_scenario(conn: psycopg.Connection, models: ModelClient, scenario: str) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {SCENARIOS}")
    migrate(conn)
    conn.execute(
        "INSERT INTO ops.judge_state (id, running, scenario, case_code, started_at) VALUES (true, true, %s, NULL, now())"
        " ON CONFLICT (id) DO UPDATE SET running = true, scenario = excluded.scenario, case_code = NULL,"
        " started_at = now(), finished_at = NULL",
        (scenario,),
    )
    code = None
    try:
        reset_school(conn)
        result = {"dangerous_morning": dangerous_morning, "nobody_accepts": nobody_accepts,
                  "nightly_check": nightly_check}[scenario](conn, models)
        code = conn.execute("SELECT code FROM cover_case WHERE id = %s", (result["case"],)).fetchone()[0] if result["case"] else None
        set_clock(conn, result["ended_at"])
        return {"scenario": scenario, "case_id": code, **{k: v for k, v in result.items() if k not in ("case", "ended_at")},
                "ended_at": result["ended_at"]}
    finally:
        conn.execute("UPDATE ops.judge_state SET running = false, case_code = %s, finished_at = now()", (code,))
