"""MCP server for a manager's assistant: official MCP Python SDK (FastMCP), streamable HTTP at /mcp.

Tools answer with reasons from the rules engine. They read, and `request_cover` leaves a proposal for the
manager; nothing here sends a message or changes the schedule. `approve_plan_b` exists so an assistant can
try it: it is always refused by the autonomy policy (N1), because approving plan B is a manager's action.

Access: with `COACHCOVER_MCP_KEY` set, `Authorization: Bearer <key>` gives the configured policy. Without a
key the endpoint is public, serves only the training school, and allows `COACHCOVER_MCP_CALLS_PER_HOUR`
requests per client address per hour (default 120).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from zoneinfo import ZoneInfo

import psycopg
from anyio import to_thread
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.types.json import Jsonb

from coachcover import ext_db, forecast, notify
from coachcover.a2a import _shift_label
from coachcover.demo import clock_now
from coachcover.reasons_text import describe, describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import assess, rank
from coachcover.seed import POLICY_ID

log = logging.getLogger("coachcover.mcp")

INSTRUCTIONS = (
    "CoachCover checks who may cover a shift: certificates on the shift's date, programs and levels, weekly "
    "hours, overlaps and the participant norm. Every answer carries its reasons. You can read and propose; "
    "you cannot approve. Approving plan B or an assignment is the manager's action in CoachCover "
    "(autonomy policy N1). Times are venue local time."
)


def _env_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise ToolError("the server has no database configured")
    return dsn


def _connect(dsn: str) -> psycopg.Connection:
    return ext_db.connect(dsn)


def _caller(ctx: Context | None) -> str:
    try:
        state = ctx.request_context.request.scope.get("state", {}).get("coachcover_mcp") or {}
        return f"mcp:{state.get('mode', 'unknown')}:{state.get('client', 'unknown')}"
    except (AttributeError, ValueError):
        return "mcp:unknown"


def _dsn_from(ctx: Context | None) -> str:
    try:
        state = ctx.request_context.request.scope.get("state", {}).get("coachcover_mcp") or {}
        if state.get("dsn"):
            return state["dsn"]
    except (AttributeError, ValueError):
        pass
    return _env_dsn()


def _policy_shift(conn: psycopg.Connection, policy_id: str, shift_id: str):
    row = conn.execute(
        "SELECT 1 FROM shift s JOIN venue v ON v.id = s.venue_id WHERE s.id = %s AND v.policy_id = %s", (shift_id, policy_id)
    ).fetchone()
    if row is None:
        raise ToolError(f"unknown shift {shift_id!r}; use shifts_on(day) or fragile_shifts(week_start) to find shift ids")
    return load_shift(conn, shift_id)


def allowed(conn: psycopg.Connection, policy_id: str, shift_id: str) -> dict:
    """Everyone (except reserve roles) checked for one shift, in queue order, with reasons."""
    shift = _policy_shift(conn, policy_id, shift_id)
    now = clock_now(conn)
    policy = load_policy(conn, policy_id)
    staff = load_staff(conn, policy_id)
    names = {s.id: s.name for s in staff}
    reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
    people = [s for s in staff if not (s.roles & reserve_roles)]
    ranked = rank(assess(s, shift, now, policy, load_context(conn, policy_id, shift)) for s in people)
    yes, no = [], []
    for r in ranked:
        c = r.candidate
        if c.decision.ok:
            yes.append({"staff_id": c.staff_id, "name": names[c.staff_id],
                        "why_first": describe_place(r, names[c.staff_id].split()[0], "en", policy.preset),
                        "warnings": [describe(x, "en", policy.preset) for x in c.decision.reasons if not x.blocking]})
        else:
            no.append({"staff_id": c.staff_id, "name": names[c.staff_id],
                       "reasons": [describe(x, "en", policy.preset) for x in c.decision.reasons if x.blocking]})
    label = _shift_label(conn, shift_id)[0]
    return {"shift": {"id": shift_id, "label": label}, "checked_at": now.isoformat(),
            "counts": {"checked": len(people), "allowed": len(yes), "not_allowed": len(no),
                       "reserve_not_in_queue": len(staff) - len(people)},
            "allowed": yes, "not_allowed": no,
            "note": "Order: fewer hours this week, same venue, level match, tenure. Nothing was sent."}


def shifts_on(conn: psycopg.Connection, policy_id: str, day: str, start: str | None) -> dict:
    when = date.fromisoformat(day)
    rows = conn.execute(
        "SELECT s.id, s.status, s.staff_id, v.tz, s.starts_at FROM shift s JOIN venue v ON v.id = s.venue_id"
        " WHERE v.policy_id = %s AND s.status <> 'cancelled'"
        " AND (s.starts_at AT TIME ZONE v.tz)::date = %s ORDER BY s.starts_at, s.id",
        (policy_id, when),
    ).fetchall()
    names = dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())
    out = []
    for sid, status, holder, tz, starts in rows:
        if start and f"{starts.astimezone(ZoneInfo(tz)):%H:%M}" != start:
            continue
        out.append({"shift_id": sid, "label": _shift_label(conn, sid)[0], "status": status,
                    "holder": names.get(holder) if holder else None})
    return {"day": day, "start": start, "shifts": out}


def case_summary(conn: psycopg.Connection, policy_id: str, case_ref: str) -> dict:
    text = str(case_ref).strip()
    code_col = notify._has_column(conn, "cover_case", "code")
    row = conn.execute(
        "SELECT c.id, c.status, c.shift_id, c.absent_staff_id, c.opened_at, c.closed_at FROM cover_case c"
        " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s AND ("
        + ("c.code = %s OR " if code_col else "") + "c.id::text = %s)",
        (policy_id, *((text,) if code_col else ()), text.removeprefix("C-")),
    ).fetchone()
    if row is None:
        raise ToolError(f"unknown case {case_ref!r}")
    case_id, status, shift_id, absent, opened, closed = row
    names = dict(conn.execute("SELECT id, full_name FROM staff").fetchall())
    events = conn.execute("SELECT seq, at, actor, kind, data FROM case_event WHERE case_id = %s ORDER BY seq", (case_id,)).fetchall()
    offers = conn.execute("SELECT staff_id, status, sent_at, responded_at FROM offer WHERE case_id = %s ORDER BY id", (case_id,)).fetchall()
    plans = conn.execute("SELECT id, status, snapshot_hash FROM plan_b WHERE case_id = %s ORDER BY id", (case_id,)).fetchall()
    covered = [d for _, _, _, kind, d in events if kind == "shift_covered"]
    ref = notify.case_ref(conn, case_id)
    # a private lesson: what the family was asked and what they answered (read-only; only the family answers)
    questions = conn.execute(
        "SELECT id, kind, staff_id, status, asked_at, answer_by, answered_at, proposed_start FROM client_ask"
        " WHERE case_id = %s ORDER BY id", (case_id,)).fetchall() if notify._has_column(conn, "client_ask", "id") else []
    return {
        "case": ref, "status": status, "shift": {"id": shift_id, "label": _shift_label(conn, shift_id)[0]},
        "absent": names.get(absent), "opened_at": opened.isoformat(), "closed_at": closed.isoformat() if closed else None,
        "covered_by": covered[-1].get("name") if covered else None,
        "offers": [{"name": names.get(s), "status": st, "sent_at": a.isoformat(),
                    "answered_at": b.isoformat() if b else None} for s, st, a, b in offers],
        "plan_b": [{"plan_id": pid, "status": st, "hash": h} for pid, st, h in plans],
        "steps": [{"seq": seq, "at": at.isoformat(), "actor": actor, "kind": kind} for seq, at, actor, kind, _ in events],
        "family_questions": [
            {"question": f"Q-{qid}", "about": "another coach" if kind == "cover" else "a new time",
             "coach": names.get(staff), "status": st, "asked_at": asked.isoformat(), "answer_by": by.isoformat(),
             "answered_at": answered.isoformat() if answered else None,
             "new_time": proposed.isoformat() if proposed else None}
            for qid, kind, staff, st, asked, by, answered, proposed in questions],
        "link": notify.case_link(ref),
    }


def propose_cover(conn: psycopg.Connection, policy_id: str, shift_id: str, requested_by: str) -> dict:
    report = allowed(conn, policy_id, shift_id)
    top = report["allowed"][:5]
    pid = conn.execute(
        "INSERT INTO ext_cover_request (shift_id, requested_by, created_at, candidates) VALUES (%s, %s, %s, %s) RETURNING id",
        (shift_id, requested_by, clock_now(conn), Jsonb(top)),
    ).fetchone()[0]
    return {"proposal_id": pid, "status": "proposed", "shift": report["shift"], "candidates": top,
            "sent": False, "booked": False,
            "note": "A proposal for the manager. CoachCover did not message anyone and did not change the schedule."}


def refuse_approval(conn: psycopg.Connection, policy_id: str, plan_id: int) -> str:
    mode = load_policy(conn, policy_id).autonomy.get("plan_b", "propose")
    exists = conn.execute("SELECT status FROM plan_b WHERE id = %s", (plan_id,)).fetchone()
    state = f"plan {plan_id} is {exists[0]}" if exists else f"there is no plan {plan_id}"
    return (f"Refused by the autonomy policy: approving plan B is the manager's action. This school sets "
            f"plan_b = '{mode}': CoachCover proposes, and only the manager approves, in CoachCover, against the hash "
            f"of the plan they saw. An outside assistant can read and propose; it cannot approve. ({state}; nothing changed.)")


def build() -> FastMCP:
    server = FastMCP(
        "CoachCover",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def run(fn, ctx: Context | None, *args):
        dsn = _dsn_from(ctx)
        policy_id = os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)

        def work():
            with _connect(dsn) as conn:
                return fn(conn, policy_id, *args)

        return await to_thread.run_sync(work)

    @server.tool()
    async def who_is_allowed(shift_id: str, ctx: Context) -> dict:
        """Who may take this shift right now, in queue order, and why every other person may not."""
        return await run(allowed, ctx, shift_id)

    @server.tool()
    async def shifts_on_day(day: str, ctx: Context, start: str | None = None) -> dict:
        """Shift ids on a day (YYYY-MM-DD), optionally only those starting at HH:MM (venue time)."""
        return await run(shifts_on, ctx, day, start)

    @server.tool()
    async def fragile_shifts(week_start: str, ctx: Context) -> dict:
        """Shifts of the week with 0 or 1 cleared backups, and the fixes that make the most of them safe."""
        day = date.fromisoformat(week_start)

        def go(conn, policy_id):
            return forecast.fragile_summary(forecast.week(conn, policy_id, clock_now(conn), None, day))

        return await run(go, ctx)

    @server.tool()
    async def get_case(case_id: str, ctx: Context) -> dict:
        """One cover case (C-1109-01 or its number): steps, offers, plan B and who covered."""
        return await run(case_summary, ctx, case_id)

    @server.tool()
    async def request_cover(shift_id: str, ctx: Context) -> dict:
        """Leave a cover proposal for the manager with the cleared people in order. Sends nothing, books nothing."""
        return await run(propose_cover, ctx, shift_id, _caller(ctx))

    @server.tool()
    async def approve_plan_b(plan_id: int, hash: str, ctx: Context) -> dict:
        """Approve a plan B. Always refused for assistants: the manager approves in CoachCover (policy N1)."""
        reason = await run(refuse_approval, ctx, plan_id)
        log.info("refused plan B approval from %s: plan %s (autonomy policy N1)", _caller(ctx), plan_id)
        raise ToolError(reason)

    @server.tool()
    async def answer_for_family(question_id: str, answer: str, ctx: Context) -> dict:
        """Answer a private lesson's question in the family's name. Always refused: only the family answers, on
        their own page; an assistant can read the status with get_case."""
        log.info("refused an answer for a family from %s: %s (only the family answers)", _caller(ctx), question_id)
        raise ToolError(f"Refused: only the family answers {question_id}, on their own page. CoachCover never takes "
                        "another coach, a new time or a credit as agreed without the family's own yes or no; an "
                        "assistant can read where it stands with get_case. Nothing changed.")

    return server


# --- HTTP: access mode, limits, and the session manager's life -----------------------------

class Endpoint:
    """ASGI app at /mcp: checks the key or the public limit, then hands the request to the MCP session manager."""

    async def __call__(self, scope, receive, send) -> None:
        app = scope.get("app")
        manager = getattr(getattr(app, "state", None), "ext_mcp_manager", None)
        if manager is None:
            return await _json(send, 503, {"error": "MCP server is not running (app lifespan not started)"})
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        key = os.environ.get("COACHCOVER_MCP_KEY", "")
        auth = headers.get("authorization", "")
        keyed = bool(key) and hmac.compare_digest(auth.encode(), f"Bearer {key}".encode())
        if auth and not keyed:
            return await _json(send, 401, {"error": "wrong key"})
        policy_id = os.environ.get("COACHCOVER_POLICY_ID", POLICY_ID)
        client = (scope.get("client") or ("unknown", 0))[0]
        if not keyed:
            if policy_id != POLICY_ID:
                return await _json(send, 401, {"error": "without a key this endpoint serves only the training school"})
            if scope.get("method") == "POST":
                limited = await to_thread.run_sync(_count_call, app, client)
                if limited:
                    return await _json(send, 429, {"error": limited}, {"retry-after": "3600"})
        scope.setdefault("state", {})["coachcover_mcp"] = {
            "mode": "key" if keyed else "public", "client": client,
            "dsn": getattr(app.state, "dsn", None) or os.environ.get("DATABASE_URL"),
        }
        await manager.handle_request(scope, receive, send)


def _count_call(app, client: str) -> str | None:
    limit = int(os.environ.get("COACHCOVER_MCP_CALLS_PER_HOUR", "120"))
    dsn = getattr(app.state, "dsn", None) or os.environ.get("DATABASE_URL")
    with _connect(dsn) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"mcp:{client}",))
        used = conn.execute(
            "SELECT count(*) FROM ops.ext_mcp_call WHERE client = %s AND at > now() - interval '1 hour'", (client,)
        ).fetchone()[0]
        if used >= limit:
            return f"public MCP limit of {limit} requests per hour reached for {client}"
        conn.execute("INSERT INTO ops.ext_mcp_call (client) VALUES (%s)", (client,))
    return None


async def _json(send, status: int, body: dict, extra: dict | None = None) -> None:
    data = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})


@asynccontextmanager
async def running(app):
    """Start a fresh MCP session manager for this app (a manager can run only once)."""
    server = build()
    server.streamable_http_app()  # creates the session manager
    app.state.ext_mcp_manager = server.session_manager
    try:
        async with server.session_manager.run():
            yield
    finally:
        app.state.ext_mcp_manager = None
