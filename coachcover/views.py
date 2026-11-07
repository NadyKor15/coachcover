"""Read models for the screens: the case page, today's screen, stats and the model ledger.

Shapes are flat fields plus arrays of rows/chips/points, so a static page can fill
its data-slot elements without logic.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg

from coachcover.models import MODEL_LABELS
from coachcover.pipeline import plain
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, eligible

CODE_TEXT = {
    Code.IS_THE_ABSENT_PERSON.value: "the absent person",
    Code.ROLE_MISSING.value: "no role",
    Code.CERT_MISSING.value: "no certificate",
    Code.CERT_NOT_YET_VALID.value: "certificate not valid yet",
    Code.CERT_EXPIRED.value: "certificate expired",
    Code.CERT_EXPIRES_DURING_SHIFT.value: "certificate runs out mid-shift",
    Code.CERT_NEEDS_REVIEW.value: "certificate to check",
    Code.CERT_EXPIRES_ON_SHIFT_DAY.value: "certificate expires that day",
    Code.PROGRAM_MISSING.value: "no program",
    Code.LEVEL_MISSING.value: "not cleared for the level",
    Code.PROGRAM_RENEWAL_OVERDUE.value: "program renewal overdue",
    Code.HOURS_LIMIT_EXCEEDED.value: "over weekly hours",
    Code.OVERLAPS_COMMITMENT.value: "busy at that time",
    Code.RATIO_EXCEEDED.value: "over the group norm",
}


def _hm(value: str | datetime, tz: ZoneInfo) -> str:
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return f"{when.astimezone(tz):%H:%M}"


def event_line(kind: str, data: dict, at: datetime, tz: ZoneInfo, names: dict) -> str:
    t = _hm(at, tz)
    who = names.get(data.get("staff_id"), data.get("name") or data.get("staff_id") or "")
    lines = {
        "callout_received": lambda: f"{t} {data.get('staff_name')} ({data.get('channel')}): «{data.get('text')}»",
        "voice_transcribed": lambda: f"{t} voice to text in {data.get('duration_ms')} ms ({data.get('model')})",
        "callout_understood": lambda: (
            f"{t} understood: {data['understood']['summary_en']}" if data.get("understood")
            else f"{t} message not understood; taking all of today's shifts"
        ),
        "shift_opened": lambda: f"{t} shift needs cover: {data.get('label')}",
        "candidates_ranked": lambda: (
            f"{t} {data.get('total')} people: {data.get('eligible')} can take it, {data.get('filtered')} filtered out"
            + (f", {data['reserve']} in reserve" if data.get("reserve") else "")
        ),
        "offer_sent": lambda: f"{t} offer to {who} (#{data.get('queue_pos')})" + (" with overtime" if data.get("overtime") else ""),
        "offer_declined": lambda: f"{t} {who}: no",
        "offer_expired": lambda: f"{t} {who}: no answer in time",
        "accept_rejected": lambda: f"{t} {who} said yes but can no longer take it: {data.get('why')}",
        "awaiting_approval": lambda: f"{t} {who} said yes; waiting for the manager",
        "shift_covered": lambda: f"{t} {who}: yes, in the schedule" + (
            f" ({data['minutes_to_cover']:g} min after the call-out)" if data.get("minutes_to_cover") is not None else ""),
        "queue_exhausted": lambda: f"{t} nobody left to ask",
        "plan_b_proposed": lambda: f"{t} plan B ready: {data.get('headline')}",
        "plan_b_approved": lambda: f"{t} plan B approved by {data.get('by')}: {data.get('option')}",
        "families_notified": lambda: f"{t} {data.get('families')} families told ({', '.join(data.get('languages', []))})",
        "zone_closed": lambda: f"{t} zone closed, {data.get('families')} families told",
        "cancelled_by_closure": lambda: f"{t} cancelled by a closure",
        "offers_off": lambda: f"{t} offers are off in the policy",
        "plan_b_off": lambda: f"{t} plan B is off in the policy",
        "approval_rejected": lambda: f"{t} the manager said no",
        "volunteered": lambda: f"{t} {who} offered to take it: «{data.get('text')}»",
        "volunteer_blocked": lambda: f"{t} {who} blocked: " + "; ".join(r["text"] for r in data.get("reasons", [])),
        "message_screened": lambda: f"{t} message filter ({data.get('model')}): {data.get('verdict')}",
        "flagged_for_manager": lambda: f"{t} message from {who} flagged for the manager; the rules were checked anyway",
        "night_check_found": lambda: f"{t} night check: {who} may not work it: " + "; ".join(r["text"] for r in data.get("reasons", [])),
        "night_cover_approved": lambda: f"{t} the manager approved finding a replacement",
        "approval_refused": lambda: f"{t} approval refused: {data.get('reason')} (shown {str(data.get('shown_hash'))[:8]}…, now {str(data.get('current_hash'))[:8]}…)",
    }
    return lines.get(kind, lambda: f"{t} {kind}")()


def stats(conn: psycopg.Connection, policy_id: str) -> dict:
    """GET /api/stats, keys as in the contract; `sim` is the 50-morning replay (synthetic)."""
    from coachcover import sim

    rows = conn.execute(
        "SELECT c.status, c.opened_at, c.closed_at, EXISTS (SELECT 1 FROM approval a WHERE a.case_id = c.id),"
        " EXISTS (SELECT 1 FROM plan_b p WHERE p.case_id = c.id AND p.status = 'approved' AND p.decided_by <> 'agent')"
        " FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s",
        (policy_id,),
    ).fetchall()
    closed = [r for r in rows if r[0] != "open"]
    by_agent = [r for r in closed if r[0] == "covered" and not r[3] and not r[4]]
    minutes = sorted((r[2] - r[1]).total_seconds() / 60 for r in rows if r[0] == "covered" and r[2])
    return plain({
        "runs": conn.execute("SELECT count(*) FROM ops.judge_run").fetchone()[0],
        "closed_without_human_pct": round(100 * len(by_agent) / len(closed), 1) if closed else None,
        "median_minutes_to_cover": minutes[len(minutes) // 2] if minutes else None,
        "eligibility_violations": eligibility_violations(conn, policy_id),
        "sim": sim.sim_stats(),
    })


def eligibility_violations(conn: psycopg.Connection, policy_id: str) -> int:
    """Audit: every booking the engine made (accepted offer, volunteer, plan B), re-checked at the moment it was made."""
    policy = load_policy(conn, policy_id)
    staff = {s.id: s for s in load_staff(conn, policy_id)}
    bad = 0
    for shift_id, at, data in conn.execute(
        "SELECT c.shift_id, e.at, e.data FROM case_event e JOIN cover_case c ON c.id = e.case_id"
        " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
        " WHERE e.kind = 'shift_covered' AND v.policy_id = %s ORDER BY e.at",
        (policy_id,),
    ).fetchall():
        shift = load_shift(conn, shift_id)
        if shift.assignee != data.get("staff_id"):
            continue  # the shift changed hands later; that booking is audited on its own
        holder = dataclasses.replace(shift, assignee=None)
        decision = eligible(staff[data["staff_id"]], holder, at, policy, load_context(conn, policy_id, shift))
        overtime = bool(data.get("overtime"))
        if any(r.blocking and not (overtime and r.code == Code.HOURS_LIMIT_EXCEEDED) for r in decision.reasons):
            bad += 1
    return bad


def ledger(conn: psycopg.Connection, limit: int = 200) -> dict:
    calls = [
        {**dict(zip(("id", "at", "purpose", "model_requested", "model_returned", "source", "ok", "error", "input_tokens",
                     "output_tokens", "ms", "cost_usd", "case_id", "synthetic", "recording"), r)),
         "model_label": MODEL_LABELS.get(r[4] or r[3], r[4] or r[3])}
        for r in conn.execute(
            "SELECT id, at, purpose, model_requested, model_returned, source, ok, error, input_tokens, output_tokens,"
            " duration_ms, cost_usd, case_id, coalesce((ref->>'synthetic')::boolean, false), ref->>'recording'"
            " FROM model_call ORDER BY id DESC LIMIT %s",
            (limit,),
        )
    ]
    per_model = [
        {"model": m, "calls": n, "failed": f, "cost_usd": float(c), "ms": int(ms or 0)}
        for m, n, f, c, ms in conn.execute(
            "SELECT model_requested, count(*), count(*) FILTER (WHERE NOT ok), coalesce(sum(cost_usd), 0),"
            " sum(duration_ms) FROM model_call GROUP BY model_requested ORDER BY model_requested"
        )
    ]
    return plain({"calls": calls, "per_model": per_model,
                  "total_cost_usd": round(sum(p["cost_usd"] for p in per_model), 6),
                  "total_calls": sum(p["calls"] for p in per_model)})
