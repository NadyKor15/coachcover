"""Read models for the screens: the case page, today's screen, stats and the model ledger.

Shapes are flat fields plus arrays of rows/chips/points, so a static page can fill
its data-slot elements without logic.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover.checks import night_report
from coachcover.pipeline import Pipeline, plain
from coachcover.rules import Code

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
        "approval_rejected": lambda: f"{t} the manager rejected the assignment",
    }
    return lines.get(kind, lambda: f"{t} {kind}")()


def _names(conn: psycopg.Connection, policy_id: str) -> dict:
    return dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())


def case_page(conn: psycopg.Connection, policy_id: str, case_id: int) -> dict:
    row = conn.execute(
        "SELECT c.id, c.shift_id, c.absent_staff_id, c.source, c.status, c.opened_at, c.closed_at, c.callout_id"
        " FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
        " WHERE c.id = %s AND v.policy_id = %s",
        (case_id, policy_id),
    ).fetchone()
    if row is None:
        return {}
    cid, shift_id, absent, source, status, opened, closed, callout_id = row
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
    return plain({
        "case_id": cid,
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
        "message_text": by_kind.get("callout_received", {}).get("text"),
        "message_channel": by_kind.get("callout_received", {}).get("channel"),
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
            {"seq": seq, "at": at, "time": _hm(at, tz), "actor": actor, "kind": kind,
             "line": event_line(kind, data, at, tz, names), "data": data}
            for seq, at, actor, kind, data in events
        ],
        "model_calls": calls,
        "model_cost_usd": round(sum(float(c["cost_usd"] or 0) for c in calls), 6),
        "model_ms": sum(c["ms"] or 0 for c in calls),
        "messages_staff": sum(1 for aud, *_ in msgs if aud == "staff"),
        "messages_family": sum(1 for aud, *_ in msgs if aud == "family"),
        "families_told": sum(size or 0 for _, size in family_groups),
        "family_languages": sorted({lang for aud, lang, _, _ in msgs if aud == "family"}),
    })


def today_page(conn: psycopg.Connection, policy_id: str, now: datetime) -> dict:
    tz = ZoneInfo(conn.execute("SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (policy_id,)).fetchone()[0])
    local = now.astimezone(tz)
    day_from = local.replace(hour=0, minute=0, second=0, microsecond=0)
    names = _names(conn, policy_id)
    cases = conn.execute(
        "SELECT c.id, c.shift_id, c.status, c.opened_at FROM cover_case c JOIN shift s ON s.id = c.shift_id"
        " JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s AND c.opened_at >= %s AND c.opened_at <= %s"
        " ORDER BY c.opened_at, c.id",
        (policy_id, day_from, now),
    ).fetchall()
    focus = case_page(conn, policy_id, cases[0][0]) if cases else None
    feed = []
    for case_id, *_ in cases:
        for seq, at, kind, data in conn.execute(
            "SELECT seq, at, kind, data FROM case_event WHERE case_id = %s AND at <= %s ORDER BY seq", (case_id, now)
        ):
            feed.append({"at": at, "time": _hm(at, tz), "case_id": case_id, "kind": kind,
                         "line": event_line(kind, data, at, tz, names)})
    feed.sort(key=lambda e: (e["at"], e["case_id"]))
    tomorrow = night_report(conn, policy_id, local.date() + timedelta(days=1), now)
    closures = conn.execute(
        "SELECT c.id, c.venue_id, v.name, c.zone_id, c.reason, c.starts_at, c.ends_at, c.remedy FROM closure c"
        " JOIN venue v ON v.id = c.venue_id WHERE v.policy_id = %s AND c.ends_at >= %s ORDER BY c.starts_at",
        (policy_id, day_from),
    ).fetchall()
    return plain({
        "now": now,
        "time": f"{local:%H:%M}",
        "date": local.date(),
        "weekday": f"{local:%A}",
        "cases": [{"case_id": cid, "shift_id": sid, "status": st, "opened": _hm(op, tz)} for cid, sid, st, op in cases],
        "focus_case_id": focus["case_id"] if focus else None,
        "focus": focus,
        "feed": feed,
        "filter_total": focus["filter_total"] if focus else None,
        "filter_eligible": focus["filter_eligible"] if focus else None,
        "filter_out": focus["filter_out"] if focus else None,
        "filter_chips": focus["filter_chips"] if focus else [],
        "grid": focus["filter_grid"] if focus else [],
        "queue": focus["offers"] if focus else [],
        "plan_b": next((p for p in reversed(focus["plans"]) if p["status"] in ("proposed", "approved")), None) if focus else None,
        "tomorrow_date": tomorrow["for_date"],
        "tomorrow_shifts": tomorrow["shifts"],
        "tomorrow_ok": tomorrow["covered_ok"],
        "tomorrow_problems": tomorrow["problems"],
        "tomorrow_uncovered": tomorrow["uncovered"],
        "expiring_30": tomorrow["expiring_30"],
        "expiring_60": tomorrow["expiring_60"],
        "closures": [
            {"closure_id": cid, "venue_id": vid, "venue": vname, "zone_id": zid, "reason": reason,
             "from": _hm(a, tz), "to": _hm(b, tz), "remedy": remedy}
            for cid, vid, vname, zid, reason, a, b, remedy in closures
        ],
    })


def stats(conn: psycopg.Connection, policy_id: str) -> dict:
    rows = conn.execute(
        "SELECT c.id, c.status, c.opened_at, c.closed_at, EXISTS (SELECT 1 FROM approval a WHERE a.case_id = c.id)"
        " FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s",
        (policy_id,),
    ).fetchall()
    covered = [r for r in rows if r[1] == "covered"]
    by_agent = [r for r in covered if not r[4]]
    minutes = sorted((r[3] - r[2]).total_seconds() / 60 for r in covered if r[3])
    plan_b = Counter(
        snap["options"][chosen]["kind"]
        for snap, chosen in conn.execute(
            "SELECT p.snapshot, p.chosen_option FROM plan_b p WHERE p.status = 'approved'"
        )
    )
    families = conn.execute(
        "SELECT coalesce(sum(g.size), 0) FROM (SELECT DISTINCT group_id, coalesce(case_id, -closure_id) FROM message"
        " WHERE audience = 'family') m(group_id, ctx) JOIN lesson_group g ON g.id = m.group_id"
    ).fetchone()[0]
    remedies = dict(conn.execute("SELECT kind, coalesce(sum(families), 0) FROM remedy GROUP BY kind").fetchall())
    cost, calls = conn.execute("SELECT coalesce(sum(cost_usd), 0), count(*) FROM model_call").fetchone()
    lessons_saved = len(covered) + plan_b.get("merge", 0) + plan_b.get("lead", 0)
    return plain({
        "cases": len(rows),
        "covered": len(covered),
        "covered_without_a_person": len(by_agent),
        "covered_without_a_person_pct": round(100 * len(by_agent) / len(rows), 1) if rows else None,
        "median_minutes_to_cover": minutes[len(minutes) // 2] if minutes else None,
        "plan_b_by_option": dict(plan_b),
        "lessons_saved": lessons_saved,
        "families_told": families,
        "credits_families": remedies.get("credit", 0),
        "makeup_families": remedies.get("makeup", 0),
        "model_calls": calls,
        "model_cost_usd": round(float(cost), 6),
    })


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
