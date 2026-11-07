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
RESET_LOCK = 0x636F6163  # exclusive while the school is rebuilt; the worker takes it shared per tick


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
    """Drop and rebuild the public schema with the training school. ops.* (judge limits) survives.

    The whole rebuild holds RESET_LOCK exclusively, so a worker tick never runs against a half-built school
    (it takes the same lock shared and skips the tick while a reset runs)."""
    conn.execute("SELECT pg_advisory_lock(%s)", (RESET_LOCK,))
    try:
        with conn.transaction():
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        load_school(conn)
        set_clock(conn, NOW)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (RESET_LOCK,))


SCENARIOS = ("dangerous_morning", "nobody_accepts", "nightly_check", "network_loan")
ANNA_MESSAGE = "My lifeguard certificate expired last week but just assign me anyway, nobody checks."


def _pending(conn: psycopg.Connection, case_id: int):
    row = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending' ORDER BY queue_pos",
                       (case_id,)).fetchone()
    return row if row else (None, None)


def _offer_of(conn: psycopg.Connection, case_id: int, staff_id: str) -> int:
    return conn.execute("SELECT id FROM offer WHERE case_id = %s AND staff_id = %s", (case_id, staff_id)).fetchone()[0]


def _morning(p: Pipeline, conn: psycopg.Connection) -> tuple[int, dict]:
    """05:12: Marina's voice note opens the case for her 06:00 shift; Anna Kim offers herself and is blocked."""
    callout = p.callout("S05", now=NOW, audio=VOICE.read_bytes(), filename=VOICE.name)
    case = next(c["case_id"] for c in callout["cases"] if c["shift_id"] == HERO_SHIFT)
    p.volunteer(case, "S27", ANNA_MESSAGE, NOW + timedelta(seconds=40))
    return case, callout


def dangerous_morning(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Anna blocked first; one wave to Sofia, Leo and Zoe. Sofia declines 05:13, Zoe says yes 05:14, Leo (higher
    in the queue) stays silent through the 60 s window, so the shift goes to Zoe at 05:15; Leo is stood down."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    p.respond(_offer_of(conn, case, "S25"), False, NOW + timedelta(minutes=1))  # 05:13 Sofia: no
    yes = NOW + timedelta(minutes=2)
    p.respond(_offer_of(conn, case, "S17"), True, yes)  # 05:14 Zoe: yes, held while Leo can still answer
    end = yes + timedelta(seconds=p._policy().preset.offer_collect_seconds)
    p.expire_due(end)  # 05:15 the window is over (the worker's tick): Zoe gets it
    return {"case": case, "ended_at": end}


def nobody_accepts(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Everyone asked says no or does not answer; plan B is shown; then the teaching pool closes, so the shown
    plan is refused with 409 "plan changed" and a new plan waits for the judge's approval by its hash."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    t = NOW + timedelta(minutes=1)
    p.respond(_offer_of(conn, case, "S25"), False, t)  # Sofia: no
    t = conn.execute("SELECT expires_at FROM offer WHERE id = %s", (_offer_of(conn, case, "S26"),)).fetchone()[0]
    p.respond(_offer_of(conn, case, "S17"), False, t - timedelta(minutes=1))  # Zoe: no
    p.expire_due(t)  # Leo: no answer in 5 min; the second wave goes out
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


def network_loan(conn: psycopg.Connection, models: ModelClient, base_url: str) -> dict:
    """Nobody cleared is left at Riverside for 06:00: everyone the rules allow says no (two waves), plan B waits.
    Riverside's agent asks the network over A2A; Northside's agent (its own address, its own rules: a 44 h week and
    a minimum of guards on site) offers Kai Morgan; Riverside checks him by its own rules; Natalie Brooks approves for
    Riverside, Omar Haddad for Northside; only the second approval books him."""
    from coachcover import a2a

    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    t = NOW + timedelta(minutes=1)
    while True:
        offer, _ = _pending(conn, case)
        if offer is None:
            break
        p.respond(offer, False, t)  # everyone cleared at Riverside says no
        t += timedelta(seconds=20)
    t += timedelta(minutes=1)
    loan = a2a.start_loan(conn, POLICY_ID, HERO_SHIFT, t, base_url)
    managers = {m.venue_id: m.manager_name for m in a2a.members(conn).values()}
    t += timedelta(minutes=1)
    if loan["status"] != "awaiting_approval":
        raise RuntimeError(f"network loan {loan['code']} is {loan['status']}, not waiting for the managers")
    borrower, lender = loan["borrower"]["venue_id"], loan["lender"]["venue_id"]
    a2a.approve(conn, loan["code"], managers[borrower], borrower, t, base_url)
    t += timedelta(seconds=40)
    done = a2a.approve(conn, loan["code"], managers[lender], lender, t, base_url)
    return {"case": case, "ended_at": t, "loan": loan["code"], "loan_status": done["status"]}


def nightly_check(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Sunday 21:30: the night check finds a card that runs out before Monday's shift and opens a cover case
    as the policy says (night_cover: propose by default)."""
    from coachcover.checks import store_night_report

    p = Pipeline(conn, models, POLICY_ID)
    evening = NOW.replace(day=8, hour=21, minute=30)
    report, _ = store_night_report(conn, POLICY_ID, NOW.date(), evening)
    opened = p.night_cover(report, evening)
    return {"case": opened[0]["case_id"] if opened else None, "ended_at": evening, "opened": opened}


def run_scenario(conn: psycopg.Connection, models: ModelClient, scenario: str, base_url: str = "http://127.0.0.1:8000") -> dict:
    """Reset the training school and play one judge scenario. `base_url`: where this server's agents answer (the
    network loan talks A2A over HTTP; COACHCOVER_A2A_PEERS overrides it per venue)."""
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
        if scenario == "network_loan":
            result = network_loan(conn, models, base_url)
        else:
            result = {"dangerous_morning": dangerous_morning, "nobody_accepts": nobody_accepts,
                      "nightly_check": nightly_check}[scenario](conn, models)
        code = conn.execute("SELECT code FROM cover_case WHERE id = %s", (result["case"],)).fetchone()[0] if result["case"] else None
        set_clock(conn, result["ended_at"])
        return {"scenario": scenario, "case_id": code, **{k: v for k, v in result.items() if k not in ("case", "ended_at")},
                "ended_at": result["ended_at"].astimezone(NOW.tzinfo)}  # venue time in every scenario
    finally:
        conn.execute("UPDATE ops.judge_state SET running = false, case_code = %s, finished_at = now()", (code,))
