"""Demo clock, school reset and the judge scenario.

The demo runs on a clock stored in the database, shared by the API and the
worker: now = base + (real time - set_at) * speed. Without a clock row, real
time is used. The judge scenario resets the fictional school to Monday 08:40
(the preschool story, every scenario; the deck story at 05:12 is for tests, see coachcover.seed)
and plays one morning on recorded and prepared model answers, with the timeline compressed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from coachcover.db import migrate
from coachcover.models import ModelClient
from coachcover.pipeline import Conflict, Pipeline
from coachcover.seed import DECK_NOW, HERO_PRIVATE, HERO_SHIFT, LESSON_AT, NOW, POLICY_ID, STORY, load_school

AUDIO = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "audio"
# preschool: "Привет, это Марина. У меня температура тридцать восемь, к половине десятого я не выйду. У меня в
# полдесятого занятие с малышами. Простите." (synthesized locally, macOS say -v Milena; the file name carries the lesson
# time it names); deck: the recorded note of the 2026-11-06 model check
VOICES = {"preschool": AUDIO / f"ru-callout-{LESSON_AT:%H%M}.ogg", "deck": AUDIO / "ru-callout.ogg"}
VOICE = VOICES[STORY]
STARTS = {"preschool": NOW, "deck": DECK_NOW}  # the demo clock after a reset
# the voice note comes 10 s after the reset clock (08:40:10): the offers' 5-minute timers then end after Zoe's
# 60-second window (08:44 + 60 s = 08:45:00), so Leo stands down instead of timing out at the same second
CALLOUT_DELAY = {"preschool": timedelta(seconds=10), "deck": timedelta(0)}
# the private-lesson scenes: Marina taught her 09:30 lesson and fell ill by midday. Her note (also say -v Milena, -r 150,
# the name stressed so the speech model hears "Лили"): "Привет, это Марина. Я заболела, в пять не смогу провести
# индивидуальное занятие с Лили. Простите." Her 17:00 private
# lesson with Lily is the only shift of her day still ahead, so the whole-day rule opens one case.
PRIVATE_VOICE = AUDIO / "ru-callout-private-1700.ogg"
PRIVATE_NOW = NOW.replace(hour=12, minute=10)  # 12:10 ET: the scene's own clock; the note comes 10 s later
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


def reset_school(conn: psycopg.Connection, story: str = STORY) -> None:
    """Drop and rebuild the public schema with the training school. ops.* (judge limits) survives.

    The whole rebuild holds RESET_LOCK exclusively, so a worker tick never runs against a half-built school
    (it takes the same lock shared and skips the tick while a reset runs)."""
    conn.execute("SELECT pg_advisory_lock(%s)", (RESET_LOCK,))
    try:
        with conn.transaction():
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        load_school(conn, story=story)
        set_clock(conn, STARTS[story])
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (RESET_LOCK,))


SCENARIOS = ("dangerous_morning", "nobody_accepts", "nightly_check", "network_loan", "private_lesson", "private_declined")
SCENARIO_STORY: dict[str, str] = {}  # every scenario plays the default (preschool) story; tests use reset_school(..., "deck")
ANNA_MESSAGES = {
    "preschool": "My WSI card expired last week but just assign me anyway, nobody checks.",
    "deck": "My lifeguard certificate expired last week but just assign me anyway, nobody checks.",
}
ANNA_MESSAGE = ANNA_MESSAGES[STORY]
RESERVE_LEAD = "S06"  # Liam Foster, head coach in reserve: plan B's first option for the preschool lesson
LIAM_MESSAGE = "A pipe burst at my place, I can't come in today. Sorry."


def _pending(conn: psycopg.Connection, case_id: int):
    row = conn.execute("SELECT id, staff_id FROM offer WHERE case_id = %s AND status = 'pending' ORDER BY queue_pos",
                       (case_id,)).fetchone()
    return row if row else (None, None)


def _offer_of(conn: psycopg.Connection, case_id: int, staff_id: str) -> int:
    return conn.execute("SELECT id FROM offer WHERE case_id = %s AND staff_id = %s", (case_id, staff_id)).fetchone()[0]


def _morning(p: Pipeline, conn: psycopg.Connection, story: str = STORY) -> tuple[int, dict]:
    """Marina's voice note opens the case for her lesson (preschool: 08:40:10 for 09:30; deck: 05:12 for 06:00);
    Anna Kim offers herself 40 s later and is blocked by her expired card."""
    voice = VOICES[story]
    at = STARTS[story] + CALLOUT_DELAY[story]
    callout = p.callout("S05", now=at, audio=voice.read_bytes(), filename=voice.name)
    case = next(c["case_id"] for c in callout["cases"] if c["shift_id"] == HERO_SHIFT)
    p.volunteer(case, "S27", ANNA_MESSAGES[story], at + timedelta(seconds=40))
    return case, callout


def dangerous_morning(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Anna blocked first; one wave to Sofia, Leo and Zoe. Sofia declines 08:43, Zoe says yes 08:44, Leo (higher
    in the queue) stays silent through the 60 s window, so the lesson goes to Zoe at 08:45; Leo is stood down."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    p.respond(_offer_of(conn, case, "S25"), False, NOW + timedelta(minutes=3))  # 08:43 Sofia: no
    yes = NOW + timedelta(minutes=4)
    p.respond(_offer_of(conn, case, "S17"), True, yes)  # 08:44 Zoe: yes, held while Leo can still answer
    end = yes + timedelta(seconds=p._policy().preset.offer_collect_seconds)
    p.expire_due(end)  # 08:45 the window is over (the worker's tick): Zoe gets it
    return {"case": case, "ended_at": end}


def _private_callout(p: Pipeline, conn: psycopg.Connection) -> tuple[int, int]:
    """Marina's midday voice note (12:10:10) names her 17:00 private lesson with Lily, the one shift of her day still
    ahead: one case. Chloe, first in its wave, says yes at 12:12 and is held; the agent asks Lily's family (answer within
    30 min, at the latest 60 min before 17:00). Returns (the case, the question to the family)."""
    at = PRIVATE_NOW + CALLOUT_DELAY["preschool"]
    callout = p.callout("S05", now=at, audio=PRIVATE_VOICE.read_bytes(), filename=PRIVATE_VOICE.name)
    case = next(c["case_id"] for c in callout["cases"] if c["shift_id"] == HERO_PRIVATE)
    p.respond(_offer_of(conn, case, "S18"), True, PRIVATE_NOW + timedelta(minutes=2))  # 12:12 Chloe: yes, family asked
    ask = conn.execute("SELECT id FROM client_ask WHERE case_id = %s AND status = 'asked'", (case,)).fetchone()[0]
    return case, ask


def private_lesson(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Lily's family says yes at 12:16: the rules run again for Chloe, and only then is she booked for 17:00."""
    p = Pipeline(conn, models, POLICY_ID)
    case, ask = _private_callout(p, conn)
    end = PRIVATE_NOW + timedelta(minutes=6)
    p.family_respond(ask, True, end)
    return {"case": case, "ended_at": end}


def private_declined(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Lily's family says no to another coach at 12:14:30: Chloe is let go with thanks; Marina's nearest free time at
    the same hour is Wednesday 17:00 (Tuesday 17:00 is her lesson with Vera); the family says yes at 12:16 and the lesson
    moves. Had they said no again, or had Marina had no free time, they would get a credit for one lesson."""
    p = Pipeline(conn, models, POLICY_ID)
    case, ask = _private_callout(p, conn)
    p.family_respond(ask, False, PRIVATE_NOW + timedelta(minutes=4, seconds=30))
    again = conn.execute("SELECT id FROM client_ask WHERE case_id = %s AND status = 'asked'", (case,)).fetchone()[0]
    end = PRIVATE_NOW + timedelta(minutes=6)
    p.family_respond(again, True, end)
    return {"case": case, "ended_at": end}


def nobody_accepts(conn: psycopg.Connection, models: ModelClient) -> dict:
    """Everyone asked says no or does not answer; plan B is shown as soon as nobody cleared is left (08:45:50). While
    the manager reads it, Liam Foster, the reserve head coach the plan starts with, calls out too (08:46:50, a pipe
    burst at home): he is out for the day, so the shown plan is refused with 409 "plan changed" (08:47:10) and a new
    plan, starting with Camila Herrera's overtime, waits for the judge's approval by its hash."""
    p = Pipeline(conn, models, POLICY_ID)
    case, _ = _morning(p, conn)
    t = NOW + timedelta(minutes=3)
    p.respond(_offer_of(conn, case, "S25"), False, t)  # 08:43 Sofia: no
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
    p.callout(RESERVE_LEAD, now=t, text=LIAM_MESSAGE)  # his own afternoon lessons open as cases at Maplewood
    conflict = None
    try:
        p.approve_plan_b(first[0], first[1], t + timedelta(seconds=20), by="judge")
    except Conflict as exc:
        conflict = {"reason": str(exc), **exc.detail}
        with conn.transaction():
            p._event(case, t + timedelta(seconds=20), "manager", "approval_refused",
                     {**conflict, "current_plan_id": conflict.get("plan_id"),  # the conflict names the new plan
                      "plan_id": f"P-{first[0]}", "shown_hash": first[1], **_what_changed(conn, first[0], case)})
    return {"case": case, "ended_at": t + timedelta(seconds=20), "conflict": conflict}


def _what_changed(conn: psycopg.Connection, shown_id: int, case: int) -> dict:
    """Why the shown plan no longer holds, from the data: a person in its options who has called out since
    ({"why", "why_name", "why_at"}; empty when none did)."""
    shown = conn.execute("SELECT snapshot, created_at FROM plan_b WHERE id = %s", (shown_id,)).fetchone()
    snapshot, created = shown
    for option in snapshot["options"]:
        row = conn.execute(
            "SELECT e.at, e.data->>'text' FROM cover_case c JOIN case_event e ON e.case_id = c.id"
            " AND e.kind = 'callout_received' WHERE c.absent_staff_id = %s AND c.source = 'callout' AND c.opened_at > %s"
            " AND c.id <> %s ORDER BY e.at LIMIT 1",
            (option.get("staff_id"), created, case),
        ).fetchone() if option.get("staff_id") else None
        if row:
            at, text = row
            role = " (reserve)" if option["kind"] == "lead" else ""
            return {"why": f"{option['staff_name']}{role} called out at {at.astimezone(NOW.tzinfo):%H:%M}: “{text}”",
                    "why_name": option["staff_name"], "why_at": at.isoformat()}
    return {}


def network_loan(conn: psycopg.Connection, models: ModelClient, base_url: str) -> dict:
    """The founder's morning, 09:30 preschool lesson: everyone allowed at Riverside says no (two waves: Sofia, Leo,
    Zoe, then Mateo and Ethan), plan B waits. Riverside's agent asks the network over A2A; Northside's agent (its own
    address, its own rules: a 44 h week and a minimum of guards on site) offers Kai Morgan, who holds the preschool
    training; Riverside checks him by its own rules; Natalie Brooks approves for Riverside, Omar Haddad for
    Northside; only the second approval books him."""
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
        story = SCENARIO_STORY.get(scenario, STORY)
        reset_school(conn, story)
        if scenario == "network_loan":
            result = network_loan(conn, models, base_url)
        else:
            result = {"dangerous_morning": dangerous_morning, "nobody_accepts": nobody_accepts,
                      "nightly_check": nightly_check, "private_lesson": private_lesson,
                      "private_declined": private_declined}[scenario](conn, models)
        code = conn.execute("SELECT code FROM cover_case WHERE id = %s", (result["case"],)).fetchone()[0] if result["case"] else None
        set_clock(conn, result["ended_at"])
        return {"scenario": scenario, "case_id": code, **{k: v for k, v in result.items() if k not in ("case", "ended_at")},
                "ended_at": result["ended_at"].astimezone(NOW.tzinfo)}  # venue time in every scenario
    finally:
        conn.execute("UPDATE ops.judge_state SET running = false, case_code = %s, finished_at = now()", (code,))
