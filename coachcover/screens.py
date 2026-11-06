"""Screen shapes of the API contract (docs/API-CONTRACT-2026-11-06.md): today's screen C and the phone offer P2.

Every number and name comes from the database and the case file. Unknown values are null, never 0 or "…".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover.checks import night_report
from coachcover.model import Preset
from coachcover.models import SHORT_NAMES
from coachcover.pipeline import NotFound, Pipeline, plain
from coachcover.repo import load_policy, load_shift, load_staff
from coachcover.rules import cert_expiry, group_norm, required_certs

LANGUAGE_NAMES = {"ru": "Russian", "en": "English", "es": "Spanish"}
RED = {"CERT_EXPIRED", "CERT_EXPIRES_DURING_SHIFT", "CERT_NOT_YET_VALID", "RATIO_EXCEEDED"}
AMBER = {"CERT_NEEDS_REVIEW", "PROGRAM_RENEWAL_OVERDUE"}
CERT_CODES = {"CERT_EXPIRED", "CERT_EXPIRES_DURING_SHIFT", "CERT_NOT_YET_VALID", "CERT_MISSING", "CERT_NEEDS_REVIEW"}


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
        return f"Not a {(role.short or role.label) if role else params['role']}"
    if code == "PROGRAM_MISSING":
        return f"No {_program_short(preset, params['program'])} program"
    if code == "LEVEL_MISSING":
        return f"Not cleared for {_program_short(preset, params['program'])}-{params['level']}"
    if code == "PROGRAM_RENEWAL_OVERDUE":
        return f"{_program_short(preset, params['program'])} renewal overdue"
    if code == "HOURS_LIMIT_EXCEEDED":
        return f"Over {params['limit_hours']:g} h this week"
    if code == "OVERLAPS_COMMITMENT":
        teaching = conn.execute("SELECT EXISTS (SELECT 1 FROM shift_group WHERE shift_id = %s)", (params["ref"],)).fetchone()[0]
        return f"{'Teaching' if teaching else 'On deck'} at {_short_clock(params['start'], tz)}"
    if code == "RATIO_EXCEEDED":
        return f"Over the 1:{params['max_per_instructor']} norm"
    if code == "IS_THE_ABSENT_PERSON":
        return "Called out"
    if code == "RESERVE":
        reserve = [r for r in preset.roles.values() if r.reserve]
        return f"Reserve ({(reserve[0].short or reserve[0].label).lower()})" if reserve else "Reserve"
    return code


def _shift_labels(info: dict, shift, preset: Preset) -> dict:
    tz = ZoneInfo(info["tz"])
    role = preset.roles[shift.role]
    needs = " + ".join([role.short or role.label] + [_cert_short(preset, c) for c in shift.extra_certs])
    participants = preset.copy.get("participants", "participants")
    kids = sum(g.size for g in shift.groups)
    lanes = info["zone"] or info["venue"]
    program = None
    if shift.groups:
        g = shift.groups[0]
        prog = preset.programs[g.program]
        lanes += f" + {prog.label} {prog.levels[g.level].label} ({kids} {participants})"
        program = f"Program {_program_short(preset, g.program)}-{g.level}"
    return {
        "id": info["id"],
        "time_label": f"{_clock(info['start'], tz)}–{_clock(info['end'], tz)}",
        "lanes_label": lanes,
        "needs_label": needs,
        "program_label": program,
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
    if shift.groups:
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
    return kind


def _plan_text(facts: dict, preset: Preset) -> str:
    parts = [_plan_option_text(o, preset) for o in facts["options"]]
    for r in facts.get("rejected", []):
        if r["kind"] == "merge" and "RATIO_EXCEEDED" in r["codes"]:
            parts.insert(0, f"merge refused: {r['participants']} {preset.copy.get('participants', 'participants')} over the norm")
    return " · ".join(parts)


def _model_log(conn, case_id: int, callout_id, live: bool) -> dict:
    rows = conn.execute(
        "SELECT model_returned, model_requested, coalesce(cost_usd, 0), ok FROM model_call"
        " WHERE case_id = %s OR (%s::uuid IS NOT NULL AND ref->>'callout_id' = %s::text) ORDER BY at, id",
        (case_id, callout_id, callout_id),
    ).fetchall()
    names = list(dict.fromkeys(SHORT_NAMES.get(m or req, m or req) for m, req, _, _ in rows))
    usd = round(sum(float(c) for _, _, c, _ in rows), 4)
    where = "running on Vultr" if live else "replayed from recorded Vultr answers"
    label = f"Models: {' · '.join(names)} — {len(rows)} calls · ${usd:.3f} · {where}" if rows else None
    return {"label": label, "calls": len(rows), "usd": usd}


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
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id WHERE s.venue_id = %s AND c.source = 'callout'"
            " AND c.opened_at <= %s ORDER BY c.opened_at DESC, c.id DESC LIMIT 1",
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
    out["shift"] = _shift_labels(info, shift, preset)
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
            "staff": f"{info_absent_name(conn, case_id)} · {out['shift']['needs_label']}",
            "audio_url": f"/media/callouts/{code}.ogg" if audio else None,
            "duration_label": f"{int(round(seconds)) // 60}:{int(round(seconds)) % 60:02d}" if seconds is not None else None,
            "language": LANGUAGE_NAMES.get(data.get("language")),
            "transcript_original": data.get("text"),
            "transcript_en": data.get("text_en"),
            "asr_label": (f"whisper on Vultr · {voice['duration_ms'] / 1000:.1f} s" + ("" if live else " · recorded"))
            if voice.get("duration_ms") else None,
        }
    ranked = first_event.get("candidates_ranked", (None, {}))[1]
    covered = first_event.get("shift_covered", (None, None))[1]
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
    for oid, sid, ostatus, sent, expires, responded, line in offers:
        result, when, label = {
            "declined": ("declined", responded, f"{names[sid]} declined"),
            "expired": ("no_reply", expires, f"{names[sid]} no reply in {int((expires - sent).total_seconds() // 60)} min"),
            "accepted": ("accepted", responded, f"{names[sid]} accepted"),
            "released": ("accepted", responded, f"{names[sid]} accepted"),
            "withdrawn": ("declined", responded or expires, f"{names[sid]} no longer eligible"),
            "pending": ("pending", sent, f"{names[sid]} asked"),
        }[ostatus]
        rows.append({"id": f"O-{oid}", "time_label": _clock(when, tz), "staff": names[sid], "result": result,
                     "why_first": line})
        if result != "pending":
            timeline.append({"time_label": _clock(when, tz), "kind": result, "label": label, "_at": when})
    plans = conn.execute(
        "SELECT id, snapshot, snapshot_hash, status, created_at, decided_at FROM plan_b WHERE case_id = %s"
        " AND status IN ('proposed', 'approved') ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    autonomy = policy.autonomy["plan_b"]
    if plans:
        pid, snap, digest, pstatus, created, decided = plans
        text = " · ".join(snap.get("text", {}).get("lines", [])) or _plan_text(snap, preset)
        out["plan_b"] = {"id": f"P-{pid}", "state": pstatus, "text": text, "hash": digest, "autonomy": autonomy}
        when = decided if pstatus == "approved" and decided else created
        timeline.append({"time_label": _clock(when, tz), "kind": "plan_b",
                         "label": "Plan B approved" if pstatus == "approved" else "Plan B ready", "_at": when})
    elif status in ("open", "covered"):
        facts = p._plan_facts(case_id, now)
        from coachcover.models import canonical_key

        out["plan_b"] = {"id": None, "state": "standby", "text": _plan_text(facts, preset),
                         "hash": canonical_key(facts), "autonomy": autonomy}
    timeline.sort(key=lambda e: e.pop("_at", opened_at))
    timeline.append({"time_label": _clock(info["start"], tz), "kind": "shift_start"})
    out["timeline"] = timeline
    out["offers"] = rows
    cancelled = conn.execute(
        "SELECT count(*) FROM shift WHERE venue_id = %s AND status = 'cancelled' AND starts_at >= %s AND starts_at < %s"
        " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = shift.id)",
        (vid, local.replace(hour=0, minute=0, second=0, microsecond=0),
         local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)),
    ).fetchone()[0]
    out["outcome"] = {"covered_by": names.get(covered_by) if covered_by else None,
                      "covered_time": _clock(info["start"], tz) if covered_by else None,
                      "lessons_cancelled": cancelled}
    out["model_log"] = _model_log(conn, case_id, callout_id, live)
    out["money"]["case_cost_cents"] = round(out["model_log"]["usd"] * 100, 2) if out["model_log"]["calls"] else None
    out["flagged"] = [
        {"time_label": _clock(at, tz), "staff": data.get("name"), "text": data.get("text"),
         "label": f"Message from {data.get('name')} flagged for the manager: it tries to change the rules. "
                  "The rules were checked anyway."}
        for _, at, kind, data in events if kind == "flagged_for_manager"
    ]
    return plain(out)


def info_absent_name(conn: psycopg.Connection, case_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.full_name FROM cover_case c JOIN staff s ON s.id = c.absent_staff_id WHERE c.id = %s", (case_id,)
    ).fetchone()
    return row[0] if row else None


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
    for code in required_certs(preset.roles[shift.role], shift, preset):
        cards = [c for c in person.certs if c.cert_type == code and not c.needs_review]
        if cards:
            exp = max(cert_expiry(c, preset.cert_types[code]) for c in cards)
            why.append(f"{_cert_short(preset, code)} — valid to {exp:%b %Y}")
    for g in {(g.program, g.level) for g in shift.groups}:
        held = person.programs.get(g[0])
        if held:
            why.append(f"{_program_short(preset, g[0])}-{g[1]} program — current")
    if place and place.get("week_hours") is not None:
        why.append(f"{place['week_hours']:g} h this week")
    manager = conn.execute("SELECT manager_name FROM policy WHERE id = %s", (policy_id,)).fetchone()[0]
    labels = _shift_labels(info, shift, preset)
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
        "expires_in_s": max(0, int((expires - now).total_seconds())) if status == "pending" else 0,
        "window_s": int((expires - sent).total_seconds()),
        "manager": {"name": manager, "role": "Manager"} if manager else None,
    })
