"""Read models for the screens: the case page, today's screen, stats and the model ledger.

Shapes are flat fields plus arrays of rows/chips/points, so a static page can fill
its data-slot elements without logic.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg

from coachcover.pipeline import Pipeline, plain
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


def _names(conn: psycopg.Connection, policy_id: str) -> dict:
    return dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())


def case_page(conn: psycopg.Connection, policy_id: str, case_id: int) -> dict:
    row = conn.execute(
        "SELECT c.id, c.shift_id, c.absent_staff_id, c.source, c.status, c.opened_at, c.closed_at, c.callout_id, c.code"
        " FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
        " WHERE c.id = %s AND v.policy_id = %s",
        (case_id, policy_id),
    ).fetchone()
    if row is None:
        return {}
    cid, shift_id, absent, source, status, opened, closed, callout_id, code = row
    shift = Pipeline(conn, None, policy_id).shift_info(shift_id)  # type: ignore[arg-type] - read-only use
    tz = ZoneInfo(shift["tz"])
    names = _names(conn, policy_id)
    events = conn.execute("SELECT seq, at, actor, kind, data FROM case_event WHERE case_id = %s ORDER BY seq", (cid,)).fetchall()
    by_kind = {}
    for _, _, _, kind, data in events:
        by_kind.setdefault(kind, data)
    ranked = by_kind.get("candidates_ranked", {})
    offers = [
        dict(zip(("offer_id", "staff_id", "queue_pos", "line", "sent_at", "expires_at", "status", "responded_at", "overtime"), r))
        for r in conn.execute(
            "SELECT id, staff_id, queue_pos, place->>'line', sent_at, expires_at, status, responded_at, overtime"
            " FROM offer WHERE case_id = %s ORDER BY id",
            (cid,),
        )
    ]
    for o in offers:
        o["name"] = names.get(o["staff_id"])
        o["sent"] = _hm(o["sent_at"], tz)
        o["answered"] = _hm(o["responded_at"], tz) if o["responded_at"] else None
    plans = []
    for pid, snap, digest, pstatus, chosen, by, decided in conn.execute(
        "SELECT id, snapshot, snapshot_hash, status, chosen_option, decided_by, decided_at FROM plan_b WHERE case_id = %s ORDER BY id",
        (cid,),
    ):
        lines = snap.get("text", {}).get("lines", [])
        plans.append({
            "plan_id": pid, "status": pstatus, "hash": digest, "headline": snap.get("text", {}).get("headline"),
            "text_source": snap.get("text_source"), "recommended": snap.get("recommended"), "chosen": chosen,
            "decided_by": by, "decided_at": decided,
            "options": [{"index": i, "kind": o["kind"], "mode": o["mode"], "line": lines[i] if i < len(lines) else o["kind"],
                         "cost_usd": o.get("cost_usd")} for i, o in enumerate(snap.get("options", []))],
            "rejected": snap.get("rejected", []),
        })
    calls = [
        dict(zip(("at", "purpose", "model", "ok", "error", "ms", "cost_usd", "input_tokens", "output_tokens", "source", "synthetic"), r))
        for r in conn.execute(
            "SELECT at, purpose, model_returned, ok, error, duration_ms, cost_usd, input_tokens, output_tokens, source,"
            " coalesce((ref->>'synthetic')::boolean, false) FROM model_call"
            " WHERE case_id = %s OR (%s::uuid IS NOT NULL AND ref->>'callout_id' = %s::text) ORDER BY at, id",
            (cid, callout_id, callout_id),
        )
    ]
    msgs = conn.execute(
        "SELECT m.audience, m.language, m.group_id, g.size FROM message m LEFT JOIN lesson_group g ON g.id = m.group_id"
        " WHERE m.case_id = %s",
        (cid,),
    ).fetchall()
    family_groups = {(g, size) for aud, _, g, size in msgs if aud == "family"}
    covered = by_kind.get("shift_covered")
    received = by_kind.get("callout_received", {})
    return plain({
        "case_id": code,
        "case_number": cid,
        "status": status,
        "source": source,
        "opened_at": opened,
        "closed_at": closed,
        "opened": _hm(opened, tz),
        "closed": _hm(closed, tz) if closed else None,
        "minutes_to_close": round((closed - opened).total_seconds() / 60, 1) if closed else None,
        "shift_id": shift_id,
        "shift_label": shift["label"],
        "venue": shift["venue"],
        "zone": shift["zone"],
        "role": shift["role_label"],
        "groups": [{"id": g["id"], "name": g["name"], "size": g["size"], "languages": g["languages"]} for g in shift["groups"]],
        "absent_staff_id": absent,
        "absent_name": names.get(absent),
        "message_text": received.get("text"),
        "message_channel": received.get("channel"),
        "transcript_original": received.get("text"),
        "transcript_en": received.get("text_en"),
        "language": received.get("language"),
        "audio_url": f"/media/callouts/{code}.ogg" if conn.execute(
            "SELECT 1 FROM callout_audio WHERE callout_id = %s", (callout_id,)).fetchone() else None,
        "filter": by_kind.get("message_screened"),
        "understood": (by_kind.get("callout_understood") or {}).get("understood"),
        "understood_by": (by_kind.get("callout_understood") or {}).get("by"),
        "filter_total": ranked.get("total"),
        "filter_eligible": ranked.get("eligible"),
        "filter_out": ranked.get("filtered"),
        "filter_chips": [{**c, "text": CODE_TEXT.get(c["code"], c["code"])} for c in ranked.get("chips", [])],
        "filter_grid": ranked.get("grid", []),
        "queue_top": ranked.get("top", []),
        "offers": offers,
        "covered_by": covered.get("staff_id") if covered else None,
        "covered_by_name": covered.get("name") if covered else None,
        "plans": plans,
        "timeline": [
            {"seq": seq, "at": at, "at_utc": at.astimezone(UTC).isoformat(), "time": _hm(at, tz), "actor": actor,
             "kind": kind, "line": event_line(kind, data, at, tz, names), "data": data}
            for seq, at, actor, kind, data in events
        ],
        "model_calls": calls,
        "model_cost_usd": round(sum(float(c["cost_usd"] or 0) for c in calls), 6),
        "total_cost_usd": round(sum(float(c["cost_usd"] or 0) for c in calls), 6),
        "model_ms": sum(c["ms"] or 0 for c in calls),
        "messages_staff": sum(1 for aud, *_ in msgs if aud == "staff"),
        "messages_family": sum(1 for aud, *_ in msgs if aud == "family"),
        "families_told": sum(size or 0 for _, size in family_groups),
        "family_languages": sorted({lang for aud, lang, _, _ in msgs if aud == "family"}),
    })


def stats(conn: psycopg.Connection, policy_id: str) -> dict:
    """Run numbers for the screens. `sim` comes from the simulation lane when it is part of the build."""
    rows = conn.execute(
        "SELECT c.id, c.status, c.opened_at, c.closed_at, EXISTS (SELECT 1 FROM approval a WHERE a.case_id = c.id),"
        " EXISTS (SELECT 1 FROM plan_b p WHERE p.case_id = c.id AND p.status = 'approved' AND p.decided_by NOT IN ('agent'))"
        " FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s",
        (policy_id,),
    ).fetchall()
    closed = [r for r in rows if r[1] != "open"]
    by_agent = [r for r in closed if r[1] == "covered" and not r[4] and not r[5]]
    minutes = sorted((r[3] - r[2]).total_seconds() / 60 for r in rows if r[1] == "covered" and r[3])
    plan_b = Counter(
        snap["options"][chosen]["kind"]
        for snap, chosen in conn.execute("SELECT p.snapshot, p.chosen_option FROM plan_b p WHERE p.status = 'approved'")
    )
    cost, calls = conn.execute("SELECT coalesce(sum(cost_usd), 0), count(*) FROM model_call").fetchone()
    runs = conn.execute("SELECT count(*) FROM ops.judge_run").fetchone()[0]
    return plain({
        "runs": runs,
        "cases": len(rows),
        "closed_without_human_pct": round(100 * len(by_agent) / len(closed), 1) if closed else None,
        "median_minutes_to_cover": minutes[len(minutes) // 2] if minutes else None,
        "eligibility_violations": eligibility_violations(conn, policy_id),
        "plan_b_by_option": dict(plan_b),
        "model_calls": calls,
        "model_cost_usd": round(float(cost), 6),
        "sim": None,
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
        dict(zip(("id", "at", "purpose", "model_requested", "model_returned", "source", "ok", "error", "input_tokens",
                  "output_tokens", "ms", "cost_usd", "case_id", "synthetic"), r))
        for r in conn.execute(
            "SELECT id, at, purpose, model_requested, model_returned, source, ok, error, input_tokens, output_tokens,"
            " duration_ms, cost_usd, case_id, coalesce((ref->>'synthetic')::boolean, false)"
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
