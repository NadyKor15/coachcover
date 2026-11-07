"""Screen shapes of the API contract (docs/API-CONTRACT-2026-11-06.md): today's screen C and the phone offer P2.

Every number and name comes from the database and the case file. Unknown values are null, never 0 or "…".
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover import messages as M
from coachcover import money as money_model
from coachcover.checks import night_report
from coachcover.model import Preset
from coachcover.models import MODEL_LABELS, SHORT_NAMES, canonical_key
from coachcover.pipeline import NotFound, Pipeline, plain
from coachcover.reasons_text import hours_words, local_stamp
from coachcover.repo import load_policy, load_shift, load_staff
from coachcover.rules import cert_expiry, group_norm, required_certs

LANGUAGE_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish"}
RED = {"CERT_EXPIRED", "CERT_EXPIRES_DURING_SHIFT", "CERT_NOT_YET_VALID", "CERT_REVOKED", "RATIO_EXCEEDED"}
AMBER = {"CERT_NEEDS_REVIEW", "PROGRAM_RENEWAL_OVERDUE", "REST_TOO_SHORT"}
CERT_CODES = {"CERT_EXPIRED", "CERT_EXPIRES_DURING_SHIFT", "CERT_NOT_YET_VALID", "CERT_MISSING", "CERT_NEEDS_REVIEW",
              "CERT_REVOKED"}
REGISTRY_NOTE = "simulated registry"  # the issuer registry in the demo is training data, never a real issuer


def _day(value: str | date) -> str:
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d:%b} {d.day}"


def _clock(value: datetime, tz: ZoneInfo) -> str:
    return f"{value.astimezone(tz):%H:%M}"


def _short_clock(value: str | datetime, tz: ZoneInfo) -> str:
    local = (datetime.fromisoformat(value) if isinstance(value, str) else value).astimezone(tz)
    return f"{local.hour}:{local:%M}"


def _cert_short(preset: Preset, code: str) -> str:
    c = preset.cert_types.get(code)
    return (c.short or c.label) if c else code


def _program_short(preset: Preset, code: str) -> str:
    p = preset.programs.get(code)
    return (p.short or p.label) if p else code


def tone(code: str) -> str:
    return "red" if code in RED else "amber" if code in AMBER else "plain"


def case_tone(code: str) -> str:
    """The case file's two kinds of refusal, from the same lists as the morning chips: "fragile" where a person must
    check something (readers disagree, renewal overdue, too little rest), "risk" for every other block."""
    return "fragile" if code in AMBER else "risk"


def reason_label(code: str, params: dict, preset: Preset, conn: psycopg.Connection, tz: ZoneInfo) -> str:
    if code == "CERT_EXPIRED":
        return f"Certificate expired {_day(params['expires_on'])}"
    if code == "CERT_MISSING":
        return f"No {_cert_short(preset, params['cert_type'])}"
    if code == "CERT_NOT_YET_VALID":
        return f"Certificate valid from {_day(params['valid_from'])}"
    if code == "CERT_EXPIRES_DURING_SHIFT":
        return f"Certificate runs out at {_short_clock(params['valid_until'], tz)}"
    if code == "CERT_NEEDS_REVIEW":
        return "Readers disagree — review"
    if code == "ROLE_MISSING":
        role = preset.roles.get(params["role"])
        word = ((role.short or role.label) if role else params["role"]).lower()
        return f"Role: not {'an' if word[:1] in 'aeiou' else 'a'} {word}"
    if code == "PROGRAM_MISSING":
        program = preset.programs.get(params["program"])
        if program is not None and program.clearance:
            return f"No {program.clearance}"
        return f"No {program.label if program else params['program']} program"
    if code == "LEVEL_MISSING":
        program = preset.programs.get(params["program"])
        if program is not None and params["level"] in program.levels:
            return f"Not cleared for {program.title(params['level'])}"  # the program and level by name, not a code
        return f"Not cleared for {params['program']} {params['level']}"
    if code == "PROGRAM_RENEWAL_OVERDUE":
        program = preset.programs.get(params["program"])
        return f"{program.label if program else params['program']} renewal overdue"
    if code == "HOURS_LIMIT_EXCEEDED":
        return f"Over {hours_words(params['limit_hours'])} this week"
    if code == "OVERLAPS_COMMITMENT":
        teaching = conn.execute("SELECT EXISTS (SELECT 1 FROM shift_group WHERE shift_id = %s)", (params["ref"],)).fetchone()[0]
        return f"{'Teaching' if teaching else 'On deck'} at {_short_clock(params['start'], tz)}"
    if code == "CERT_REVOKED":
        until = date.fromisoformat(params["expires_on"])
        return f"Valid until {until:%b %Y} — REVOKED by issuer on {_day(params['revoked_on'])} ({REGISTRY_NOTE})"
    if code == "REST_TOO_SHORT":
        hours = f"{params['rest_hours']:g}"
        if params.get("side") == "after":
            return f"Only {hours} h rest before {_short_clock(params['until'], tz)}"
        return f"Only {hours} h rest since {_short_clock(params['since'], tz)}"
    if code == "RATIO_EXCEEDED":
        return f"Over the 1:{params['max_per_instructor']} norm"
    if code == "IS_THE_ABSENT_PERSON":
        return "Called out"
    if code == "CALLED_OUT":
        return "Called out today"  # sick, a burst pipe: the message says why
    if code == "RESERVE":
        reserve = [r for r in preset.roles.values() if r.reserve]
        return f"Reserve ({(reserve[0].short or reserve[0].label).lower()})" if reserve else "Reserve"
    return code


def _co_teachers(conn: psycopg.Connection, shift_id: str) -> list[str]:
    """People on the other shifts that teach this shift's groups at the same time (the second instructor of a lesson)."""
    return [name for name, _ in conn.execute(
        "SELECT st.full_name, min(s.id) FROM shift s JOIN shift_group sg ON sg.shift_id = s.id JOIN staff st ON st.id = s.staff_id"
        " WHERE sg.group_id IN (SELECT group_id FROM shift_group WHERE shift_id = %s) AND s.id <> %s AND s.status = 'scheduled'"
        " AND s.starts_at = (SELECT starts_at FROM shift WHERE id = %s) GROUP BY st.full_name ORDER BY min(s.id)",
        (shift_id, shift_id, shift_id),
    )]


def _private_label(name: str) -> str:
    """A private lesson's group name for screens: "Private · Lily, age 6, Level 2" -> "Private lesson · Lily, age 6, Level 2"."""
    return "Private lesson · " + name.removeprefix("Private · ") if name.startswith("Private · ") else name


def _shift_labels(info: dict, shift, preset: Preset, co_teachers: list[str] | None = None) -> dict:
    tz = ZoneInfo(info["tz"])
    role = preset.roles[shift.role]
    needs = " + ".join([role.short or role.label] + [_cert_short(preset, c) for c in shift.extra_certs])
    participants = preset.copy.get("participants", "participants")
    kids = sum(g.size for g in shift.groups)
    lanes = info["zone"] or info["venue"]
    program = None
    if shift.groups and info.get("private"):
        g = shift.groups[0]
        # "<zone> · Private lesson · Lily, age 6, Level 2": one child, no count
        lanes += " · " + _private_label(info["groups"][0]["name"])
        program = preset.programs[g.program].title(g.level)
    elif shift.groups:
        g = shift.groups[0]
        prog = preset.programs[g.program]
        lanes += f" + {prog.title(g.level)} ({kids} {participants})"
        program = prog.needs(g.level)
    if co_teachers:
        lanes += f" · with {' and '.join(co_teachers)}"
    return {
        "id": info["id"],
        "time_label": f"{_clock(info['start'], tz)}–{_clock(info['end'], tz)}",
        "lanes_label": lanes,
        "needs_label": needs,
        "program_label": program,
        "private": bool(info.get("private")),
    }


def _first_refusal(events: list, grid: list, shift, preset: Preset, conn, tz) -> dict | None:
    """The first person who would take it but may not: a blocked volunteer, else the best placed person blocked only by a card."""
    blocked = next(((data, True) for _, _, kind, data in events if kind == "volunteer_blocked"), None)
    if blocked:
        data, willing = blocked
        reasons = data["reasons"]
        name = data["name"]
        free = all(r["code"] != "OVERLAPS_COMMITMENT" for r in reasons)
    else:
        row = next((p for p in grid if p["blocking_codes"] and set(p["blocking_codes"]) <= CERT_CODES), None)
        if row is None:
            return None
        reasons = [row["first"]]
        name, willing, free = row["name"], False, True
    first = reasons[0]
    if first["code"] == "CERT_EXPIRED":
        why = f"{_cert_short(preset, first['params']['cert_type'])} certificate expired {_day(first['params']['expires_on'])}"
    else:
        why = reason_label(first["code"], first["params"], preset, conn, tz)
    parts = [" and ".join(x for x in ("free" if free else None, "willing" if willing else None) if x) or "available",
             f"BLOCKED: {why}"]
    if shift.groups and all(g.kind == "private" for g in shift.groups):
        parts.append("private lesson")  # one child, one coach: no group norm to show
    elif shift.groups:
        kids = sum(g.size for g in shift.groups)
        norm = min(group_norm(g, preset) for g in shift.groups)
        parts += [f"{kids} {preset.copy.get('participants', 'participants')}", f"ratio 1:{norm}"]
    return {"staff": name, "label": " · ".join(parts), "contrast_quote": preset.copy.get("contrast_quote")}


def _plan_option_text(option: dict, preset: Preset) -> str:
    kind = option["kind"]
    participants = preset.copy.get("participants", "participants")
    if kind == "merge":
        return f"merge into {option['staff_name']}'s group ({option['participants']} {participants}, norm ok)"
    if kind == "lead":
        return f"{option['staff_name']} (reserve) steps in"
    if kind == "overtime":
        return f"{option['staff_name']} on overtime: {option['hours_over']:g} h, ${option['cost_usd']:.2f}"
    if kind == "reschedule":
        return f"move the lesson: makeup for {option['families']} families"
    if kind == "close":
        return f"cancel: {option['remedy']} for {option['families']} families"
    if kind == "split":
        return (f"{option['staff_name']} takes {option['children']} {participants} at the same time "
                f"({option['participants_after']} of {option['capacity']}, norm ok)")
    if kind == "move":
        return f"move the {option['children']} newest {participants} to the nearest lesson of their level with room"
    if kind == "cancel_lesson":
        return f"cancel the whole lesson: {option['remedy']} for {option['families']} families"
    if kind == "reschedule_own":
        return f"move the lesson to {option['staff_name'].split()[0]}, {option['when']}"
    if kind == "credit":
        return f"cancel: a credit for one lesson (${option['amount_usd']:g})"
    return kind


def _plan_text(facts: dict, preset: Preset) -> str:
    parts = [M.plan_b_short_line(o) for o in M.plan_b_view(facts)["options"]]  # the plan in a few words
    for r in facts.get("rejected", []):
        if r["kind"] == "merge" and "RATIO_EXCEEDED" in r["codes"]:
            parts.insert(0, f"No merge: {r['participants']} {preset.copy.get('participants', 'participants')} over the norm")
    return " · ".join(parts)


def _model_log(conn, case_id: int, callout_id, live: bool) -> dict:
    rows = conn.execute(
        "SELECT model_returned, model_requested, coalesce(cost_usd, 0), ok FROM model_call"
        " WHERE case_id = %s OR (%s::uuid IS NOT NULL AND ref->>'callout_id' = %s::text) ORDER BY at, id",
        (case_id, callout_id, callout_id),
    ).fetchall()
    names = list(dict.fromkeys(SHORT_NAMES.get(m or req, m or req) for m, req, _, _ in rows))
    # the same bill as the money card's "This cover": one number for the cost of this cover on the whole screen
    bill = money_model.case_cost(conn, case_id) if case_id is not None else None
    usd = round(bill["usd"], 6) if bill else round(sum(float(c) for _, _, c, _ in rows), 6)
    calls = bill["calls"] if bill else len(rows)
    where = "running on Vultr" if live else "replay mode: recorded and prepared demo answers"
    label = f"Models: {' · '.join(names)} — {calls} calls · {money_model.cost_label(usd)} · {where}" if rows else None
    return {"label": label, "calls": calls, "usd": usd}


def judge_state(conn: psycopg.Connection) -> dict:
    row = conn.execute("SELECT running, case_code FROM ops.judge_state").fetchone()
    return {"running": bool(row[0]), "case_id": row[1]} if row else {"running": False, "case_id": None}


def today(conn: psycopg.Connection, policy_id: str, now: datetime, venue_id: str | None, live: bool) -> dict:
    policy = load_policy(conn, policy_id)
    preset = policy.preset
    manager = conn.execute("SELECT manager_name FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
    venue = conn.execute(
        "SELECT id, name, tz FROM venue WHERE policy_id = %s AND (%s::text IS NULL OR id = %s) ORDER BY id LIMIT 1",
        (policy_id, venue_id, venue_id),
    ).fetchone()
    if venue is None:
        raise NotFound(f"unknown venue {venue_id!r}")
    vid, vname, tzname = venue
    tz = ZoneInfo(tzname)
    local = now.astimezone(tz)
    judge = judge_state(conn)
    focus = None
    if judge["case_id"]:
        focus = conn.execute(
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id WHERE c.code = %s AND s.venue_id = %s",
            (judge["case_id"], vid),
        ).fetchone()
    if focus is None:
        focus = conn.execute(
            # the latest call-out; of the cases it opened (a person is out for the whole day), the one that starts first
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id WHERE s.venue_id = %s AND c.source = 'callout'"
            " AND c.opened_at <= %s ORDER BY c.opened_at DESC, s.starts_at, c.id LIMIT 1",
            (vid, now),
        ).fetchone()
    p = Pipeline(conn, None, policy_id)  # type: ignore[arg-type] - read-only use
    tomorrow = night_report(conn, policy_id, local.date() + timedelta(days=1), now)
    closures_today = conn.execute(
        "SELECT count(*) FROM closure WHERE venue_id = %s AND starts_at < %s AND ends_at > %s",
        (vid, local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
         local.replace(hour=0, minute=0, second=0, microsecond=0)),
    ).fetchone()[0]
    expiring = len(tomorrow["expiring_30"])
    out = {
        "title": None,
        "venue": {"id": vid, "name": vname, "term": preset.venue_term.get("en")},
        "now_label": f"{local:%a}, {local:%b} {local.day} · {local:%H:%M}",
        "user_label": f"{manager} · Manager" if manager else None,
        "lang": "en",
        "callout": None,
        "shift": None,
        "eligibility": None,
        "timeline": [],
        "offers": [],
        "outcome": None,
        "plan_b": None,
        "summary_cards": [
            {"title": "Tomorrow", "value": f"{tomorrow['covered_ok']} of {tomorrow['shifts']} shifts covered"},
            {"title": "Expiring in 30 days", "value": f"{expiring} certificate{'s' if expiring != 1 else ''}"},
            {"title": "Closures today", "value": "none" if not closures_today else str(closures_today)},
        ],
        "money": {"uncovered_morning_usd": None, "uncovered_morning_label": None, "case_cost_cents": None,
                  "manager_hour_usd": None},
        "model_log": {"label": None, "calls": 0, "usd": 0.0},
        "flagged": [],
        "night_check": _night_check(conn, vid, now, tz),
        "network_loan": None,
        "ratio_alert": _ratio_alert(conn, vid, now, tz, preset),
        "judge": judge,
    }
    if focus is None:
        out["title"] = preset.copy.get("title", "Who can cover {time}").replace(" at {time}", "").replace("{time}", "")
        return plain(out)
    case_id = focus[0]
    code, shift_id, status, callout_id, opened_at = conn.execute(
        "SELECT code, shift_id, status, callout_id, opened_at FROM cover_case WHERE id = %s", (case_id,)
    ).fetchone()
    info = p.shift_info(shift_id)
    shift = load_shift(conn, shift_id)
    out["title"] = preset.copy.get("title", "Who can cover {time}").format(time=_clock(info["start"], tz))
    co_teachers = _co_teachers(conn, shift_id)
    out["shift"] = _shift_labels(info, shift, preset)  # two lines on the morning screen: the other teacher goes below
    events = conn.execute("SELECT seq, at, kind, data FROM case_event WHERE case_id = %s ORDER BY seq", (case_id,)).fetchall()
    first_event = {kind: (at, data) for _, at, kind, data in reversed(events)}
    received = first_event.get("callout_received")
    if received:
        at, data = received
        voice = first_event.get("voice_transcribed", (None, {}))[1]
        audio = conn.execute("SELECT seconds FROM callout_audio WHERE callout_id = %s", (callout_id,)).fetchone()
        seconds = float(audio[0]) if audio and audio[0] is not None else None
        out["callout"] = {
            "case_id": code,
            "time_label": _clock(at, tz),
            "staff": f"{info_absent_name(conn, case_id)} · {out['shift']['needs_label']}"
                     + (f" · with {' and '.join(co_teachers)}" if co_teachers else ""),
            "audio_url": f"/media/callouts/{code}.ogg" if audio else None,
            "duration_label": f"{int(round(seconds)) // 60}:{int(round(seconds)) % 60:02d}" if seconds is not None else None,
            "language": LANGUAGE_NAMES.get(data.get("language")),
            "transcript_original": data.get("text"),
            "transcript_en": data.get("text_en"),
            # a prepared answer (synthetic) was never a model run: no "on Vultr", no timing
            "asr_label": "whisper · prepared demo answer" if voice.get("synthetic")
            else (f"whisper on Vultr · {voice['duration_ms'] / 1000:.1f} s" + ("" if live else " · recorded"))
            if voice.get("duration_ms") else None,
        }
    ranked = first_event.get("candidates_ranked", (None, {}))[1]
    covered_at, covered = first_event.get("shift_covered", (None, None))
    covered_by = covered.get("staff_id") if covered else None
    grid = ranked.get("grid", [])
    if ranked:
        chips: dict[str, dict] = {}
        staff_rows = []
        for person in grid:
            label = None
            if person["first"]:
                label = reason_label(person["first"]["code"], person["first"]["params"], preset, conn, tz)
                chip = chips.setdefault(label, {"code": person["first"]["code"], "label": label, "count": 0,
                                                "tone": tone(person["first"]["code"])})
                chip["count"] += 1
            staff_rows.append({"id": person["staff_id"], "name": person["name"], "eligible": person["eligible"],
                               "highlight": person["staff_id"] == covered_by, "reason_label": label})
        order = {"red": 0, "amber": 1, "plain": 2}
        out["eligibility"] = {
            "total": ranked["total"],
            "eligible": ranked["eligible"],
            "reasons": sorted(chips.values(), key=lambda c: (order[c["tone"]], -c["count"], c["label"])),
            "staff": staff_rows,
            "first_refusal": _first_refusal(events, grid, shift, preset, conn, tz),
        }
    names = dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())
    offers = conn.execute(
        "SELECT id, staff_id, status, sent_at, expires_at, responded_at, place->>'line' FROM offer WHERE case_id = %s ORDER BY id",
        (case_id,),
    ).fetchall()
    timeline = [{"time_label": _clock(first_event[k][0], tz), "kind": "callout", "label": "Call-out"}
                for k in ("callout_received", "night_check_found") if k in first_event][:1]
    rows = []
    stood = _stood_down(conn, case_id)
    family = _family_offers(conn, case_id)
    for oid, sid, ostatus, sent, expires, responded, line in offers:
        result, when, label, _ = _offer_result(ostatus, sid, sent, expires, responded, stood.get(oid), names,
                                               preset.offer_collect_seconds)
        if oid in family:  # a private lesson: the coach said yes, the family decides
            result, label, _ = (x.format(name=names[sid]) for x in FAMILY_OFFER[family[oid][0]])
            when = responded or when
        rows.append({"id": f"O-{oid}", "time_label": _clock(when, tz), "staff": names[sid], "result": result,
                     "why_first": line, "result_label": label[len(names[sid]):].lstrip(": ") or None})
        # on a private lesson the family's answers tell the story; the coaches let go stay in the offers list
        quiet = info["private"] and (result == "stood_down" or (oid in stood and stood[oid][2]))
        if result not in ("pending", "held") and not quiet:
            timeline.append({"time_label": _clock(when, tz), "kind": result, "label": label, "_at": when})
    if info["private"]:
        timeline.extend({**step, "time_label": _clock(step["_at"], tz)} for step in _family_steps(events))
    timeline.extend({**step, "time_label": _clock(step["_at"], tz)} for step in _network_steps(events))
    # a plan shown and then replaced because the facts changed: when it was shown, and what changed (stable sort:
    # these come before the current plan's line at the same minute)
    for (created,) in conn.execute("SELECT created_at FROM plan_b WHERE case_id = %s AND status = 'superseded'"
                                   " ORDER BY id", (case_id,)).fetchall():
        timeline.append({"time_label": _clock(created, tz), "kind": "plan_b", "label": "Plan B ready", "_at": created,
                         "_replaced": True})
    for _, at, kind, data in events:  # who changed the facts, at the moment they did (the full line is in the case)
        if kind == "approval_refused" and data.get("why_name") and data.get("why_at"):
            when = datetime.fromisoformat(data["why_at"])
            timeline.append({"time_label": _clock(when, tz), "kind": "callout", "label": f"{data['why_name']} called out",
                             "_at": when})
    replaced = any(e.pop("_replaced", False) for e in timeline)
    plans = conn.execute(
        "SELECT id, snapshot, snapshot_hash, status, created_at, decided_at FROM plan_b WHERE case_id = %s"
        " AND status IN ('proposed', 'approved') ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    autonomy = policy.autonomy["plan_b"]
    if plans:
        pid, snap, digest, pstatus, created, decided = plans
        text = _glued(snap.get("text", {}).get("lines", [])) or _plan_text(snap, preset)
        out["plan_b"] = {"id": f"P-{pid}", "state": pstatus, "text": text, "hash": digest, "autonomy": autonomy}
        when = decided if pstatus == "approved" and decided else created
        timeline.append({"time_label": _clock(when, tz), "kind": "plan_b",
                         "label": "Plan B approved" if pstatus == "approved" else "New plan B ready" if replaced
                         else "Plan B ready", "_at": when})
    elif status in ("open", "covered"):
        facts = p._plan_facts(case_id, now)
        out["plan_b"] = {"id": "P-standby", "state": "standby", "text": _plan_text(facts, preset),
                         "hash": canonical_key(facts), "autonomy": autonomy}
    timeline.sort(key=lambda e: e.pop("_at", opened_at))
    timeline.append({"time_label": _clock(info["start"], tz), "kind": "shift_start"})
    out["timeline"] = timeline
    out["offers"] = rows
    cancelled = conn.execute(  # group lessons; a private lesson's outcome is its own line (moved or a credit)
        "SELECT count(*) FROM shift WHERE venue_id = %s AND status = 'cancelled' AND starts_at >= %s AND starts_at < %s"
        " AND EXISTS (SELECT 1 FROM shift_group sg JOIN lesson_group g ON g.id = sg.group_id"
        "  WHERE sg.shift_id = shift.id AND g.kind = 'group')",
        (vid, local.replace(hour=0, minute=0, second=0, microsecond=0),
         local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)),
    ).fetchone()[0]
    # the event carries the name: a person lent by another venue of the network is not on this school's staff list
    out["outcome"] = {"covered_by": (covered.get("name") or names.get(covered_by)) if covered_by else None,
                      "covered_via": covered.get("via") if covered else None,
                      "covered_time": _clock(covered_at, tz) if covered_by else None,  # when it was closed
                      "lessons_cancelled": cancelled,
                      "private_line": next((d.get("line") for _, _, k, d in reversed(events)
                                            if k in ("private_moved", "private_credit")), None),
                      # a private lesson: the family has the question until then (the banner says so)
                      "family_wait_until": next((_clock(by, tz) for (by,) in conn.execute(
                          "SELECT answer_by FROM client_ask WHERE case_id = %s AND status = 'asked'", (case_id,))), None)}
    loan = next((data.get("loan") for _, _, kind, data in reversed(events) if kind == "network_loan_requested"), None)
    out["network_loan"] = {"code": loan, "link": f"/network.html?loan={loan}"} if loan else None
    out["model_log"] = _model_log(conn, case_id, callout_id, live)
    out["money"] = today_money_block(conn, policy_id, now, vid, private=info["private"])
    out["flagged"] = [
        {"time_label": _clock(at, tz), "staff": data.get("name"), "text": data.get("text"),
         "label": f"Message from {data.get('name')} flagged for the manager: it tries to change the rules. "
                  "The rules were checked anyway."}
        for _, at, kind, data in events if kind == "flagged_for_manager"
    ]
    return plain(out)


def _manager_minutes() -> tuple[float | None, str | None]:
    """Manager minutes per morning without CoachCover, from the 50-morning replay (synthetic)."""
    from coachcover import sim

    stats = sim.sim_stats()
    return stats.get("baseline_manager_minutes_median_per_morning"), f"synthetic replay, {stats['mornings']} mornings"


def today_money_block(conn: psycopg.Connection, policy_id: str, now: datetime, venue_id: str | None = None,
                      private: bool = False) -> dict:
    minutes, source = _manager_minutes()
    money = money_model.today_money(conn, policy_id, now, manager_minutes=minutes, manager_minutes_source=source,
                                    venue_id=venue_id, private=private)["money"]
    cents = money.get("case_cost_cents")
    money["case_cost_label"] = money_model.cost_label(cents / 100) if cents is not None else None
    prices = load_policy(conn, policy_id).preset.money
    price = (prices.get("private_lesson_price") if private else None) or prices.get("lesson_price", {})
    money["source"] = "assumption" if price.get("assumption", True) else price.get("source")
    return money


def case_risk(shift, policy) -> dict:
    """The "If nobody had accepted" card of one case: refunds and overtime at the preset's prices."""
    params = money_model.money_params(policy.preset.money, policy.overtime_cost_per_hour)
    cost = money_model.uncovered_cost([shift], params)
    refunds = sum(line["usd"] for line in cost["lines"] if line["kind"] == "refunds")
    overtime = sum(line["usd"] for line in cost["lines"] if line["kind"] == "overtime")
    private = bool(shift.groups) and all(g.kind == "private" for g in shift.groups)
    return {"kids": (cost["participants"] or None) if not private else None, "private": private,
            "refunds_usd": round(refunds, 2) if refunds else None,
            "overtime_usd": round(overtime, 2) if overtime else None, "total_usd": cost["total_usd"] or None,
            "lines": money_model.summary_lines(cost, params),
            "assumptions": params.as_dict()}


def info_absent_name(conn: psycopg.Connection, case_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.full_name FROM cover_case c JOIN staff s ON s.id = c.absent_staff_id WHERE c.id = %s", (case_id,)
    ).fetchone()
    return row[0] if row else None


def _stood_down(conn: psycopg.Connection, case_id: int) -> dict[int, tuple[datetime, str, bool]]:
    """Offers withdrawn because someone else got the shift, or was held for a private lesson's family:
    offer id -> (when, that person's staff id, held for the family)."""
    return {
        int(data["offer_id"]): (at, data.get("covered_by") or data.get("for_family"), "for_family" in data)
        for at, data in conn.execute(
            "SELECT at, data FROM case_event WHERE case_id = %s AND kind = 'offer_stood_down'", (case_id,)
        ).fetchall()
    }


def _offer_result(ostatus: str, sid: str, sent, expires, responded, stood, names: dict, collect: int) -> tuple:
    """(result, when, timeline label, short label) of one offer."""
    name = names[sid]
    if ostatus == "withdrawn" and stood:
        at, winner, for_family = stood
        first = names.get(winner, winner).split()[0]
        if responded is None:
            took = f"{first} said yes first" if for_family else f"{first} took it"
            return "no_reply", at, f"{name}: no reply, {took}", f"no reply, {took}"
        return "stood_down", at, f"{name} said yes, {first} was higher in the queue", f"said yes, {first} was higher"
    minutes = int((expires - sent).total_seconds() // 60)
    return {
        "declined": ("declined", responded, f"{name} declined", "declined"),
        "expired": ("no_reply", expires, f"{name} no reply in {minutes} min", f"no reply in {minutes} min"),
        "accepted": ("accepted", responded, f"{name} accepted", "accepted"),
        "released": ("accepted", responded, f"{name} accepted", "accepted, later gave it back"),
        "withdrawn": ("declined", responded or expires, f"{name} no longer eligible", "no longer eligible"),
        "held": ("held", responded, f"{name} said yes, waiting {collect} s for anyone higher",
                 f"said yes, waiting {collect} s for anyone higher"),
        "pending": ("pending", sent, f"{name} asked", "asked, waiting"),
    }[ostatus]


FAMILY_OFFER = {  # a coach's offer on a private lesson, by the family's answer: (result, timeline label, short label)
    "asked": ("held", "{name} said yes, waiting for the family", "said yes, waiting for the family"),
    "yes": ("accepted", "{name} said yes", "said yes, the family agreed"),
    "no": ("stood_down", "{name} said yes, the family kept their own coach", "said yes, the family kept their own coach"),
    "silent": ("stood_down", "{name} said yes, the family did not answer", "said yes, no answer from the family"),
}


def _family_offers(conn: psycopg.Connection, case_id: int) -> dict[int, tuple[str, datetime]]:
    """Offers whose coach was put to the family (private lesson): offer id -> (the family's answer, when)."""
    return {oid: (status, at) for oid, status, at in conn.execute(
        "SELECT offer_id, status, coalesce(answered_at, CASE WHEN status = 'silent' THEN answer_by END, asked_at)"
        " FROM client_ask WHERE case_id = %s AND kind = 'cover' ORDER BY id", (case_id,))}


def _network_steps(events: list) -> list[dict]:
    """A loan from another venue of the network, for the morning's timeline: asked, offered, both managers agreed."""
    out, approvals = [], 0
    for _, at, kind, data in events:
        label = None
        if kind == "network_loan_requested":
            label = "Asked the network"
        elif kind == "network_loan_offered":
            label = f"{str(data.get('from') or 'Another venue').split()[0]} offers {data.get('name')}"
        elif kind == "network_loan_approved":
            approvals += 1
            label = "Both managers agreed" if approvals == 2 else None
        if label:
            out.append({"kind": "network", "label": label, "_at": at})
    return out


def _family_steps(events: list) -> list[dict]:
    """The family's part of a private lesson, for the morning screen's timeline: asked, the answer, the outcome. The
    question about a new time and its answer fold into the outcome ("Moved to Marina, Wed 17:00")."""
    out = []
    for _, at, kind, data in events:
        label = None
        if kind == "family_asked" and data.get("kind") == "cover":
            label = "Asked the parent"
        elif kind in ("family_yes", "family_no") and data.get("kind") == "cover":
            label = "Parent said yes" if kind == "family_yes" else "Parent said no"
        elif kind == "family_silent":
            label = "No answer from the parent"
        elif kind in ("private_moved", "private_credit"):
            label = data.get("line")
        if label:
            out.append({"kind": "family", "label": label, "_at": at})
    return out


def _family_case_steps(events: list, tz: ZoneInfo) -> list[dict]:
    """A private lesson in the case file: every question to the family, its answer and what followed."""
    steps = []

    def add(at, kind, title, summary):
        steps.append({"kind": kind, "at": _utc(at), "time_label": _local_label(at, tz), "title": title, "summary": summary})

    for _, at, kind, data in events:
        name = (data.get("name") or "").split(" ")[0]
        if kind == "family_asked":
            by = _clock(datetime.fromisoformat(data["answer_by"]), tz)
            source = ("written by the text model" if data.get("text_source") == "model"
                      else "standard text: the model's draft failed the check" if data.get("model_text_rejected")
                      else "standard text")
            if data.get("kind") == "cover":
                add(at, "family", "Asked the parent", f"“{data['text']}” · {name} is held for this time · "
                                                      f"answer by {by} · {source}")
            else:
                add(at, "family", f"Asked the parent about {name}, {data.get('when')}",
                    f"“{data['text']}” · answer by {by} · {source}")
        elif kind in ("family_yes", "family_no"):
            word = "yes" if kind == "family_yes" else "no"
            what = f"to {name} instead" if data.get("kind") == "cover" else "to the new time"
            add(at, "family", f"Parent said {word}", f"{word.capitalize()} {what}")
        elif kind == "family_silent":
            add(at, "family", "No answer from the parent",
                "The coach who said yes is let go with thanks; the manager decides: move the lesson or a credit")
        elif kind == "family_yes_recheck_failed":
            add(at, "family", "Checked again: not allowed any more", data.get("why") or "")
        elif kind == "coach_released":
            add(at, "family", f"{data.get('name')} let go, with thanks", "Free for other offers again")
        elif kind == "own_coach_checked":
            tried = [f"{datetime.fromisoformat(t['start']).astimezone(tz):%a} {_short_clock(t['start'], tz)}: "
                     + ("busy" if "OVERLAPS_COMMITMENT" in t["codes"] else "not allowed" if t["codes"] else "free")
                     for t in data.get("tried", [])]
            add(at, "family", f"{data.get('name') or 'The own coach'}: nearest free time at the same hour",
                " · ".join(tried) or "No free time in the next 7 days")
        elif kind == "private_moved":
            add(at, "family", data.get("line"), f"{data.get('name')} teaches it; the old time is cancelled; nothing charged")
        elif kind == "private_credit":
            why = {"family_no": "the family said no to the new time", "no_time": "no free time with their own coach",
                   "too_late": "too close to the start to ask", "time_taken": "the new time was taken meanwhile",
                   "plan_b": "the manager chose it"}.get(data.get("why"), data.get("why"))
            add(at, "family", "Credit for one lesson", f"${data.get('amount_usd'):g} credit to the family · {why}")
    return steps


def _ratio_alert(conn: psycopg.Connection, venue_id: str, now: datetime, tz: ZoneInfo, preset: Preset) -> dict | None:
    """The banner for today's latest broken children-per-instructor norm: red while a seat is open, then closed."""
    day_start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT c.id, c.code, c.status, c.opened_at, c.closed_at, s.staff_id, st.full_name FROM cover_case c"
        " JOIN shift s ON s.id = c.shift_id LEFT JOIN staff st ON st.id = s.staff_id"
        " WHERE s.venue_id = %s AND c.source = 'ratio' AND c.opened_at <= %s AND c.opened_at >= %s"
        " ORDER BY c.opened_at DESC, c.id DESC LIMIT 1",
        (venue_id, now, day_start),
    ).fetchone()
    if row is None:
        return None
    case_id, code, status, opened_at, closed_at, staff_id, staff_name = row
    data = conn.execute("SELECT data FROM case_event WHERE case_id = %s AND kind = 'ratio_broken' ORDER BY seq LIMIT 1",
                        (case_id,)).fetchone()[0]
    word = preset.copy.get("participants", "participants")
    per = data["max_per_instructor"]
    covered = status == "covered" and closed_at is not None and closed_at <= now
    if status == "covered" and not covered:
        status = "open"  # the demo clock is before the moment it was covered
    return {
        "case_id": code,
        "status": status,
        "tone": "green" if covered else "red" if status == "open" else "amber",
        "group": data["group_name"],
        "label": f"{data['group_name']} — now {data['participants']} {word}, ratio 1:{per} broken",
        "participants": data["participants"],
        "max_per_instructor": per,
        "instructors_needed": data["instructors_needed"],
        "shift_label": data["label"],
        "opened_label": _clock(opened_at, tz),
        "action_label": f"Covered by {staff_name} · {_clock(closed_at, tz)}" if covered and staff_name
        else "One more instructor needed — asking the queue" if status == "open" else STATUS_LABELS.get(status, status),
    }


def _night_check(conn: psycopg.Connection, venue_id: str, now: datetime, tz: ZoneInfo) -> dict | None:
    row = conn.execute(
        "SELECT c.code, c.status, e.at, e.data FROM cover_case c JOIN shift s ON s.id = c.shift_id"
        " JOIN case_event e ON e.case_id = c.id AND e.kind = 'night_check_found'"
        " WHERE s.venue_id = %s AND c.source = 'night_check' AND c.opened_at <= %s ORDER BY c.opened_at DESC, c.id DESC LIMIT 1",
        (venue_id, now),
    ).fetchone()
    if row is None:
        return None
    code, status, at, data = row
    pending = conn.execute(
        "SELECT a.status FROM approval a JOIN cover_case c ON c.id = a.case_id WHERE c.code = %s AND a.action = 'night_cover'"
        " ORDER BY a.id DESC LIMIT 1",
        (code,),
    ).fetchone()
    reason = data["reasons"][0]["text"] if data.get("reasons") else "a problem"
    first = (data.get("reasons") or [{}])[0]
    if first.get("code") == "CERT_EXPIRED" and first.get("params"):
        starts = conn.execute("SELECT starts_at FROM shift WHERE id = %s", (data["shift_id"],)).fetchone()[0].astimezone(tz)
        reason = (f"{first['params']['cert_type_label']} card ends {_day(first['params']['expires_on'])}, "
                  f"before {starts:%a} {starts:%H:%M}") if first["params"].get("cert_type_label") else reason
    step = {"pending": "replacement proposed to the manager", "approved": "replacement approved",
            "rejected": "the manager kept the shift"}.get(pending[0] if pending else "", "replacement under way")
    return {"case_id": code, "label": f"Night check {_clock(at, tz)}: {data['name']} — {reason} · {step}",
            "status": status}


def offer_phone(conn: psycopg.Connection, policy_id: str, offer_ref: str, now: datetime) -> dict:
    oid = str(offer_ref).removeprefix("O-")
    row = conn.execute(
        "SELECT o.id, o.shift_id, o.staff_id, o.status, o.sent_at, o.expires_at, o.place FROM offer o"
        " JOIN shift s ON s.id = o.shift_id JOIN venue v ON v.id = s.venue_id WHERE o.id::text = %s AND v.policy_id = %s",
        (oid, policy_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown offer {offer_ref}")
    offer_id, shift_id, staff_id, status, sent, expires, place = row
    policy = load_policy(conn, policy_id)
    preset = policy.preset
    p = Pipeline(conn, None, policy_id)  # type: ignore[arg-type] - read-only use
    info = p.shift_info(shift_id)
    shift = load_shift(conn, shift_id)
    tz = ZoneInfo(info["tz"])
    local_day = info["start"].astimezone(tz).date()
    today_local = now.astimezone(tz).date()
    day = "today" if local_day == today_local else "tomorrow" if local_day == today_local + timedelta(days=1) \
        else f"{local_day:%a}, {local_day:%b} {local_day.day}"
    person = next(s for s in load_staff(conn, policy_id) if s.id == staff_id)
    why = []
    needed = required_certs(preset.roles[shift.role], shift, preset)
    order = [needed[0], *shift.extra_certs, *needed[1:]] if needed else []  # main card, the shift's extra, the rest
    for code in dict.fromkeys(order):
        cards = [c for c in person.certs if c.cert_type == code and not c.needs_review]
        if cards:
            exp = max(cert_expiry(c, preset.cert_types[code]) for c in cards)
            why.append(f"{_cert_short(preset, code)} — valid to {exp:%b %Y}")
    for g in {(g.program, g.level) for g in shift.groups}:
        held = person.programs.get(g[0])
        if held:
            prog = preset.programs.get(g[0])
            why.append(f"{prog.needs(g[1])} — current" if prog is not None and prog.clearance
                       else f"{prog.title(g[1]) if prog is not None else g[0]} — current")
    if place and place.get("week_hours") is not None:
        why.append(f"{hours_words(place['week_hours'])} this week")  # "5 h 45 min", as on Today
    manager = conn.execute("SELECT manager_name FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
    labels = _shift_labels(info, shift, preset, _co_teachers(conn, shift_id))
    return plain({
        "offer_id": f"O-{offer_id}",
        "state": status,
        "staff": person.name,
        "shift_day": day,
        "shift_start": _clock(info["start"], tz),
        "shift_end": _clock(info["end"], tz),
        "venue": info["venue"],
        "shift_detail": labels["lanes_label"],
        "needs_label": labels["needs_label"],
        "why": why,
        "why_first": (place or {}).get("line"),
        "private": bool(info.get("private")),  # a private lesson: a yes waits for the family
        "expires_in_s": max(0, int((expires - now).total_seconds())) if status == "pending" else 0,
        "window_s": int((expires - sent).total_seconds()),
        "manager": {"name": manager, "role": "Manager"} if manager else None,
    })


def _family_headline(kind: str, outcome: dict | None, coach: str, child: str, day: str) -> str:
    """The family's page headline: the open question, or, once the case is decided, what happened."""
    if outcome is None:
        return f"Can {coach.split()[0]} teach {child} {day}?" if kind == "cover" else f"Move {child}'s lesson to {day}?"
    if outcome["kind"] == "covered":
        return f"{outcome['coach']} will teach {child} {outcome['day']}"
    if outcome["kind"] == "moved":
        return f"{child}'s lesson is moved to {outcome['day']}"
    return f"{child}'s lesson is cancelled, with a credit"


def client_page(conn: psycopg.Connection, policy_id: str, ask_ref: str, now: datetime) -> dict:
    """The family's page of a private lesson (`/client?id=Q-n`): what is asked, the thread with the school, and the
    answer buttons while the question is open. Signed "Parent of <child>": the parent is never named."""
    qid = str(ask_ref).removeprefix("Q-")
    row = conn.execute(
        "SELECT a.id, a.case_id, a.kind, a.staff_id, a.status, a.proposed_start, a.proposed_end, a.asked_at, a.answer_by,"
        " a.group_id, c.shift_id, c.absent_staff_id FROM client_ask a JOIN cover_case c ON c.id = a.case_id"
        " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE a.id::text = %s AND v.policy_id = %s",
        (qid, policy_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown question {ask_ref}")
    ask_id, case_id, kind, staff_id, status, p_start, p_end, asked_at, answer_by, group_id, shift_id, absent = row
    p = Pipeline(conn, None, policy_id)  # type: ignore[arg-type] - read-only use
    info = p.shift_info(shift_id)
    tz = ZoneInfo(info["tz"])
    child = info["groups"][0]["participant"] or info["groups"][0]["name"]
    names = dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())

    def day_label(when: datetime) -> str:
        local = when.astimezone(tz)
        today_local = now.astimezone(tz).date()
        if local.date() == today_local:
            return "today"
        if local.date() == today_local + timedelta(days=1):
            return "tomorrow"
        return f"{local:%a}, {local:%b} {local.day}"

    start, end = (p_start, p_end) if kind == "reschedule" else (info["start"], info["end"])
    thread = []
    for at, body in conn.execute("SELECT created_at, body FROM message WHERE case_id = %s AND audience = 'family'"
                                 " AND group_id = %s ORDER BY created_at, id", (case_id, group_id)):
        text = body.removeprefix("CoachCover: ")
        thread.append({"from": "school", "time_label": _clock(at, tz), "text": text[:1].upper() + text[1:], "_at": at})
    for at, akind, astatus in conn.execute("SELECT answered_at, kind, status FROM client_ask WHERE case_id = %s"
                                           " AND status IN ('yes', 'no') ORDER BY id", (case_id,)):
        thread.append({"from": "family", "time_label": _clock(at, tz), "text": "Yes" if astatus == "yes" else "No",
                       "_at": at})
    thread.sort(key=lambda m: (m.pop("_at"), m["from"] == "school"))  # an answer comes before the reply to it
    current = conn.execute("SELECT id FROM client_ask WHERE case_id = %s AND status = 'asked' ORDER BY id DESC LIMIT 1",
                           (case_id,)).fetchone()
    outcome = None
    for _, kind_e, data in conn.execute("SELECT seq, kind, data FROM case_event WHERE case_id = %s AND kind IN"
                                        " ('shift_covered', 'private_moved', 'private_credit') ORDER BY seq DESC LIMIT 1",
                                        (case_id,)).fetchall():
        if kind_e == "shift_covered":
            outcome = {"kind": "covered", "line": f"{data['name'].split()[0]} teaches {child} {day_label(info['start'])}"
                                                  f" at {_clock(info['start'], tz)}",
                       "coach": data["name"].split()[0], "day": day_label(info["start"])}
        elif kind_e == "private_moved":
            local = datetime.fromisoformat(data["start"]).astimezone(tz)
            outcome = {"kind": "moved", "line": f"{child}'s lesson with {data['name'].split()[0]} is on "
                                               f"{local:%a}, {local:%b} {local.day} at {local:%H:%M}",
                       "day": f"{local:%a}, {local:%b} {local.day}"}
        else:
            outcome = {"kind": "credit", "line": f"A credit for one lesson (${data['amount_usd']:g})"}
    manager = conn.execute("SELECT manager_name FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
    coach = names.get(staff_id, staff_id)
    return plain({
        "ask_id": f"Q-{ask_id}",
        "kind": kind,
        "state": status,
        "signed": f"Parent of {child}",
        "child": child,
        "lesson": _private_label(info["groups"][0]["name"]),
        "venue": info["venue"],
        "question": _family_headline(kind, outcome, coach, child, day_label(start)),
        "coach": coach,
        "instead_of": names.get(absent) if kind == "cover" else None,
        "day_label": day_label(start),
        "time_label": f"{_clock(start, tz)}–{_clock(end, tz)}",
        "answer_in_s": max(0, int((answer_by - now).total_seconds())) if status == "asked" else 0,
        "answer_by_label": _clock(answer_by, tz),
        "thread": thread,
        "current": f"Q-{current[0]}" if current else None,
        "outcome": outcome,
        "manager": {"name": manager, "role": "Manager"} if manager else None,
    })


STATUS_LABELS = {"open": "Open", "covered": "Covered", "plan_b": "Plan B", "cancelled": "Cancelled", "uncovered": "Uncovered"}


def _utc(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _glued(lines: list[str]) -> str:
    """Plan lines glued into one strip: "Liam Foster (reserve) steps in · Camila Herrera works 15 minutes of overtime
    ($8.50) · …", never "steps in. · Camila". A line's own end period goes; the stored plan keeps its sentences."""
    return " · ".join(re.sub(r"\.\s*$", "", line.strip()) for line in lines if line and line.strip())


def _local_label(value: datetime, tz: ZoneInfo) -> str:
    return local_stamp(value, tz)  # "08:40:10 ET": the venue's time; machine fields stay in UTC


# why each model was called, in the words of the case page's Proof table
PURPOSE_LABELS = {
    "transcribe": "Voice note to text",
    "screen_message": "Message check",
    "extract_callout": "Read the call-out",
    "translate": "Translate to English",
    "family_question": "Question to the family",
    "family_message": "Message to the family",
    "plan_b_text": "Plan B in plain words",
    "read_card": "Read a certificate card",
    "whatif_parse": "Read a what-if question",
}


def model_calls(conn: psycopg.Connection, case_id: int, callout_id, tz: ZoneInfo | None = None) -> list[dict]:
    """The case's model calls, each priced as money.case_cost prices it: the case's own calls in full, the call-out's
    calls (made once for the whole message) at an equal share between the cases that message opened. So the case page,
    Today and the downloaded case show one cost, and the cases of a call-out add up to the ledger. Each row also
    carries its words for the Proof table: the step's time in the venue's zone, why the model was called, seconds,
    cents (rounded so the rows add up to the case's cost label), "shared by N cases" and where the answer came from."""
    rows = conn.execute(
        "SELECT at, purpose, model_requested, model_returned, duration_ms, input_tokens, output_tokens, cost_usd, source,"
        " ref->>'recording', coalesce((ref->>'synthetic')::boolean, false), ok, error, case_id FROM model_call"
        " WHERE case_id = %s OR (%s::uuid IS NOT NULL AND case_id IS NULL AND ref->>'callout_id' = %s::text) ORDER BY at, id",
        (case_id, callout_id, callout_id),
    ).fetchall()
    shared_by = conn.execute("SELECT count(*) FROM cover_case WHERE callout_id = %s", (callout_id,)).fetchone()[0] \
        if callout_id is not None else 0
    prices = money_model.load_prices()
    out = []
    for at, purpose, requested, returned, ms, tin, tout, cost, source, recording, synthetic, ok, error, own in rows:
        price, _ = money_model.call_price({"model_returned": returned, "model_requested": requested, "input_tokens": tin,
                                           "output_tokens": tout, "cost_usd": cost}, prices)
        share = 1.0 if own is not None or not shared_by else 1 / shared_by
        if source == "live":
            where = "live call on Vultr"
        else:
            where = f"recorded answer tests/fixtures/models/{recording}" + (" (synthetic)" if synthetic else "") if recording \
                else "no recorded answer"
        out.append({
            "at": _utc(at), "purpose": purpose, "model": returned or requested,
            "model_label": MODEL_LABELS.get(returned or requested, returned or requested),
            "ms": ms, "input_tokens": tin, "output_tokens": tout,
            "cost_usd": round(price * share, 10) if price is not None else None,
            "source": (where if ok else f"{where} · failed: {error}")
            + (f" · the call-out's call, shared by {shared_by} cases" if share < 1 else ""),
            "time_label": local_stamp(at, tz) if tz is not None else None,
            "purpose_label": PURPOSE_LABELS.get(purpose, purpose.replace("_", " ").capitalize()),
            "seconds_label": f"{ms / 1000:.1f} s" if ms is not None else None,
            "shared_label": f"shared by {shared_by} cases" if share < 1 else None,
            "source_label": ("live" if source == "live" else "prepared" if synthetic else "recorded" if recording
                             else "no answer") + ("" if ok else " · failed"),
        })
    total = money_model.case_cost(conn, case_id)["usd"]
    for row, cents in zip(out, money_model.split_cents([r["cost_usd"] for r in out], total)):
        row["cost_label"] = cents
    return out


def case_page(conn: psycopg.Connection, policy_id: str, case_id: int, live: bool) -> dict:
    """The case file as one proof page: call-out, shift, who is allowed, offers, outcome, models and their cost."""
    policy = load_policy(conn, policy_id)
    preset = policy.preset
    row = conn.execute(
        "SELECT c.code, c.shift_id, c.status, c.opened_at, c.closed_at, c.callout_id, c.absent_staff_id FROM cover_case c"
        " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE c.id = %s AND v.policy_id = %s",
        (case_id, policy_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"unknown case {case_id}")
    code, shift_id, status, opened, closed, callout_id, absent = row
    p = Pipeline(conn, None, policy_id)  # type: ignore[arg-type] - read-only use
    info = p.shift_info(shift_id)
    shift = load_shift(conn, shift_id)
    tz = ZoneInfo(info["tz"])
    labels = _shift_labels(info, shift, preset, _co_teachers(conn, shift_id))
    manager = conn.execute("SELECT manager_name FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
    names = dict(conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s", (policy_id,)).fetchall())
    events = conn.execute("SELECT seq, at, kind, data FROM case_event WHERE case_id = %s ORDER BY seq", (case_id,)).fetchall()
    first = {kind: (at, data) for _, at, kind, data in reversed(events)}
    steps = []
    if "callout_received" in first:
        at, data = first["callout_received"]
        voice = first.get("voice_transcribed", (None, {}))[1]
        audio = conn.execute("SELECT 1 FROM callout_audio WHERE callout_id = %s", (callout_id,)).fetchone()
        steps.append({
            "kind": "callout", "at": _utc(at), "time_label": _local_label(at, tz),
            "title": "Voice call-out" if data.get("channel") == "voice" else "Text call-out",
            "staff": f"{names.get(absent)} · {labels['needs_label']}",
            "language": LANGUAGE_NAMES.get(data.get("language")),
            "audio_url": f"/media/callouts/{code}.ogg" if audio else None,
            "transcript_original": data.get("text"), "transcript_en": data.get("text_en"),
            "model_tag": f"{MODEL_LABELS.get(voice.get('model'), voice.get('model'))} · prepared demo answer"
            if voice.get("synthetic")
            else f"{MODEL_LABELS.get(voice.get('model'), voice.get('model'))} · {voice['duration_ms'] / 1000:.1f} s"
            if voice.get("duration_ms") else None,
        })
    if "night_check_found" in first:
        at, data = first["night_check_found"]
        steps.append({"kind": "night_check", "at": _utc(at), "time_label": _local_label(at, tz), "title": "Night check",
                      "summary": f"{data['name']}: " + "; ".join(r["text"] for r in data.get("reasons", []))})
    if "ratio_broken" in first:
        at, data = first["ratio_broken"]
        word = preset.copy.get("participants", "participants")
        steps.append({"kind": "ratio", "at": _utc(at), "time_label": _local_label(at, tz),
                      "title": f"Norm broken: {data['group_name']} — now {data['participants']} {word}, "
                               f"ratio 1:{data['max_per_instructor']}",
                      "summary": f"{data['added']} added by {data['by']}; instructors needed: "
                                 f"{data['instructors_before']} → {data['instructors_needed']}"})
    for _, at, kind, data in events:
        if kind == "flagged_for_manager":
            steps.append({"kind": "flagged", "at": _utc(at), "time_label": _local_label(at, tz),
                          "title": "Message flagged for the manager",
                          "summary": f"{data.get('name')}: “{data.get('text')}” · the rules were checked anyway"})
    if "callout_understood" in first or "shift_opened" in first:
        at, data = first.get("callout_understood") or first["shift_opened"]
        by = data.get("by")
        steps.append({
            "kind": "shift", "at": _utc(at), "time_label": _local_label(at, tz), "title": "Shift identified",
            "shift_id": shift_id,
            "summary": f"{labels['time_label']} · Needs: {labels['needs_label']}"
            + (f" · {labels['program_label']}" if labels["program_label"] else ""),
            "model_tag": MODEL_LABELS.get(by, by) if by and by != "fallback" else None,
        })
    ranked = first.get("candidates_ranked")
    if ranked:
        at, data = ranked
        grid = data.get("grid", [])
        chips: dict[str, dict] = {}
        staff_rows = []
        for person in grid:
            label = None
            if person["first"]:
                label = reason_label(person["first"]["code"], person["first"]["params"], preset, conn, tz)
                chips.setdefault(label, {"code": person["first"]["code"], "label": label, "count": 0,
                                         "tone": case_tone(person["first"]["code"])})["count"] += 1
            staff_rows.append({"id": person["staff_id"], "name": person["name"], "allowed": person["eligible"],
                               "reason_label": label})
        refusal = _first_refusal(events, grid, shift, preset, conn, tz)
        steps.append({
            "kind": "allowed", "at": _utc(at), "time_label": _local_label(at, tz), "title": "Who is allowed",
            "total": data["total"], "allowed": data["eligible"],
            "blocked": {"staff": refusal["staff"], "label": refusal["label"]} if refusal else None,
            "reasons": sorted(chips.values(), key=lambda c: (-c["count"], c["label"])),
            "staff": staff_rows,
        })
    offers = conn.execute(
        "SELECT id, staff_id, status, sent_at, expires_at, responded_at, place->>'line' FROM offer WHERE case_id = %s ORDER BY id",
        (case_id,),
    ).fetchall()
    if offers:
        rows = []
        stood = _stood_down(conn, case_id)
        family = _family_offers(conn, case_id)
        for oid, sid, ostatus, sent, expires, responded, line in offers:
            result, when, _, result_label = _offer_result(ostatus, sid, sent, expires, responded, stood.get(oid), names,
                                                          preset.offer_collect_seconds)
            if oid in family:  # a private lesson: the coach said yes, the family decided
                result, _, result_label = FAMILY_OFFER[family[oid][0]]
                when = responded or when
            rows.append({"id": f"O-{oid}", "time_label": _clock(when, tz), "staff": names[sid], "result": result,
                         "result_label": result_label, "why_first": line})
        steps.append({"kind": "offers", "at": _utc(offers[0][3]), "time_label": _local_label(offers[0][3], tz),
                      "title": "Offers in order", "offers": rows})
    steps.extend(_family_case_steps(events, tz))
    for _, at, kind, data in events:
        if kind == "approval_refused":
            steps.append({"kind": "plan_b", "at": _utc(at), "time_label": _local_label(at, tz),
                          "title": "Approval refused: plan changed",
                          "summary": f"{data.get('plan_id')} was shown with hash {str(data.get('shown_hash'))[:8]}…; "
                                     f"the data changed, the current plan has hash {str(data.get('current_hash'))[:8]}…"
                                     + (f" · {data['why']}" if data.get("why") else "")})
    plan = conn.execute(
        "SELECT id, snapshot, snapshot_hash, status, created_at, decided_at, decided_by, chosen_option FROM plan_b"
        " WHERE case_id = %s AND status IN ('proposed', 'approved') ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    cancelled = conn.execute(
        "SELECT count(*) FROM shift WHERE status = 'cancelled' AND venue_id = %s AND starts_at::date = %s::date"
        " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = shift.id)",
        (info["venue_id"], info["start"]),
    ).fetchone()[0]
    covered = first.get("shift_covered")
    if covered:
        at, data = covered
        co = [n for n in _co_teachers(conn, shift_id) if n != data.get("name")]  # the lesson's other teacher stays
        steps.append({"kind": "covered", "at": _utc(at), "time_label": _local_label(at, tz), "title": "Schedule covered",
                      "summary": f"{labels['time_label'].split('–')[0]} covered by {data.get('name')}"
                                 + (f", teaching with {' and '.join(co)}" if co else "")
                                 + f" · {cancelled} lesson{'s' if cancelled != 1 else ''} cancelled"})
    if plan:
        pid, snap, digest, pstatus, created, decided, decided_by, chosen = plan
        lines = snap.get("text", {}).get("lines", [])
        if pstatus == "approved":
            option = snap["options"][chosen]
            text = _glued([lines[chosen]]) if chosen < len(lines) else option["kind"]
            steps.append({"kind": "plan_b", "at": _utc(decided), "time_label": _local_label(decided, tz),
                          "title": "Plan B approved by hash", "summary": f"{text} · approved by {decided_by} · P-{pid}"})
        else:
            steps.append({"kind": "plan_b", "at": _utc(created), "time_label": _local_label(created, tz),
                          "title": "Plan B waiting for approval", "summary": _glued(lines) or _plan_text(snap, preset)})
        proof = {"plan_id": f"P-{pid}", "plan_hash": digest}
    elif status in ("open", "covered"):
        proof = {"plan_id": None, "plan_hash": canonical_key(p._plan_facts(case_id, closed or opened))}
    else:
        proof = {"plan_id": None, "plan_hash": None}
    proof["policy_label"] = f"cover = {policy.autonomy['assign_cover']} · plan B = {policy.autonomy['plan_b']}"
    steps.sort(key=lambda s: s["at"])
    calls = model_calls(conn, case_id, callout_id, tz)
    bill = money_model.case_cost(conn, case_id)
    local_day = info["start"].astimezone(tz)
    private_end = next((k for _, _, k, _ in reversed(events) if k in ("private_moved", "private_credit")), None)
    return plain({
        "case_id": code,
        "status": status,
        "status_label": {"private_moved": "Moved", "private_credit": "Credit"}.get(private_end)
        or STATUS_LABELS.get(status, status),
        "covered_time_label": _clock(covered[0], tz) if covered else None,
        "covered_by": covered[1].get("name") if covered else None,
        "covered_via": covered[1].get("via") if covered else None,
        "shift_label": f"{_clock(info['start'], tz)} {labels['lanes_label'].split(' (')[0]}",
        "venue": {"id": info["venue_id"], "name": info["venue"]},
        "date_label": f"{local_day:%a}, {local_day:%b} {local_day.day}",
        "user_label": f"{manager} · Manager" if manager else None,
        "opened_at": _utc(opened),
        "closed_at": _utc(closed) if closed else None,
        "steps": steps,
        "proof": proof,
        "model_calls": calls,
        # one cost of this cover on every screen: money.case_cost (its own calls + its share of the call-out's)
        "model_cost_usd": round(bill["usd"], 6),
        "model_cost_label": money_model.cost_label(bill["usd"]),
        # the Proof table's last line: the same count and cost as the "Models:" line above it
        "model_calls_total_label": f"{len(calls)} call{'s' if len(calls) != 1 else ''} · "
                                   f"{money_model.cost_label(bill['usd'])}"
                                   + (f" ({bill['usd'] * 100:.2f}¢)" if bill["usd"] >= 0.01 else ""),
        "risk": case_risk(shift, policy),
    })
