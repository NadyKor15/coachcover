"""Screen shapes of the API contract (docs/API-CONTRACT-2026-11-06.md): today's screen C and the phone offer P2.

Every number and name comes from the database and the case file. Unknown values are null, never 0 or "…".
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover import money as money_model
from coachcover.checks import night_report
from coachcover.model import Preset
from coachcover.models import MODEL_LABELS, SHORT_NAMES, canonical_key
from coachcover.pipeline import NotFound, Pipeline, plain
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
        return "Called out sick today"
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
    if kind == "split":
        return (f"{option['staff_name']} takes {option['children']} {participants} at the same time "
                f"({option['participants_after']} of {option['capacity']}, norm ok)")
    if kind == "move":
        return f"move the {option['children']} newest {participants} to the nearest lesson of their level with room"
    if kind == "cancel_lesson":
        return f"cancel the whole lesson: {option['remedy']} for {option['families']} families"
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
    label = f"Models: {' · '.join(names)} — {len(rows)} calls · {money_model.cost_label(usd)} · {where}" if rows else None
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
    for oid, sid, ostatus, sent, expires, responded, line in offers:
        result, when, label, _ = _offer_result(ostatus, sid, sent, expires, responded, stood.get(oid), names,
                                               preset.offer_collect_seconds)
        rows.append({"id": f"O-{oid}", "time_label": _clock(when, tz), "staff": names[sid], "result": result,
                     "why_first": line})
        if result not in ("pending", "held"):
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
        out["plan_b"] = {"id": "P-standby", "state": "standby", "text": _plan_text(facts, preset),
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
    # the event carries the name: a person lent by another venue of the network is not on this school's staff list
    out["outcome"] = {"covered_by": (covered.get("name") or names.get(covered_by)) if covered_by else None,
                      "covered_via": covered.get("via") if covered else None,
                      "covered_time": _clock(covered_at, tz) if covered_by else None,  # when it was closed
                      "lessons_cancelled": cancelled}
    out["model_log"] = _model_log(conn, case_id, callout_id, live)
    out["money"] = today_money_block(conn, policy_id, now)
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


def today_money_block(conn: psycopg.Connection, policy_id: str, now: datetime) -> dict:
    minutes, source = _manager_minutes()
    money = money_model.today_money(conn, policy_id, now, manager_minutes=minutes, manager_minutes_source=source)["money"]
    cents = money.get("case_cost_cents")
    money["case_cost_label"] = money_model.cost_label(cents / 100) if cents is not None else None
    price = load_policy(conn, policy_id).preset.money.get("lesson_price", {})
    money["source"] = "assumption" if price.get("assumption", True) else price.get("source")
    return money


def case_risk(shift, policy) -> dict:
    """The "If nobody had accepted" card of one case: refunds and overtime at the preset's prices."""
    params = money_model.money_params(policy.preset.money, policy.overtime_cost_per_hour)
    cost = money_model.uncovered_cost([shift], params)
    refunds = sum(line["usd"] for line in cost["lines"] if line["kind"] == "refunds")
    overtime = sum(line["usd"] for line in cost["lines"] if line["kind"] == "overtime")
    return {"kids": cost["participants"] or None, "refunds_usd": round(refunds, 2) if refunds else None,
            "overtime_usd": round(overtime, 2) if overtime else None, "total_usd": cost["total_usd"] or None,
            "lines": money_model.summary_lines(cost, params),
            "assumptions": params.as_dict()}


def info_absent_name(conn: psycopg.Connection, case_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.full_name FROM cover_case c JOIN staff s ON s.id = c.absent_staff_id WHERE c.id = %s", (case_id,)
    ).fetchone()
    return row[0] if row else None


def _stood_down(conn: psycopg.Connection, case_id: int) -> dict[int, tuple[datetime, str]]:
    """Offers withdrawn because someone else got the shift: offer id -> (when, winner's staff id)."""
    return {
        int(data["offer_id"]): (at, data["covered_by"])
        for at, data in conn.execute(
            "SELECT at, data FROM case_event WHERE case_id = %s AND kind = 'offer_stood_down'", (case_id,)
        ).fetchall()
    }


def _offer_result(ostatus: str, sid: str, sent, expires, responded, stood, names: dict, collect: int) -> tuple:
    """(result, when, timeline label, short label) of one offer."""
    name = names[sid]
    if ostatus == "withdrawn" and stood:
        at, winner = stood
        first = names.get(winner, winner).split()[0]
        if responded is None:
            return "no_reply", at, f"{name}: no reply, {first} took it", f"no reply, {first} took it"
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


STATUS_LABELS = {"open": "Open", "covered": "Covered", "plan_b": "Plan B", "cancelled": "Cancelled", "uncovered": "Uncovered"}


def _utc(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_label(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).strftime("%H:%M:%S UTC")


def model_calls(conn: psycopg.Connection, case_id: int, callout_id) -> list[dict]:
    rows = conn.execute(
        "SELECT at, purpose, model_requested, model_returned, duration_ms, input_tokens, output_tokens, cost_usd, source,"
        " ref->>'recording', coalesce((ref->>'synthetic')::boolean, false), ok, error FROM model_call"
        " WHERE case_id = %s OR (%s::uuid IS NOT NULL AND ref->>'callout_id' = %s::text) ORDER BY at, id",
        (case_id, callout_id, callout_id),
    ).fetchall()
    out = []
    for at, purpose, requested, returned, ms, tin, tout, cost, source, recording, synthetic, ok, error in rows:
        if source == "live":
            where = "live call on Vultr"
        else:
            where = f"recorded answer tests/fixtures/models/{recording}" + (" (synthetic)" if synthetic else "") if recording \
                else "no recorded answer"
        out.append({
            "at": _utc(at), "purpose": purpose, "model": returned or requested,
            "model_label": MODEL_LABELS.get(returned or requested, returned or requested),
            "ms": ms, "input_tokens": tin, "output_tokens": tout,
            "cost_usd": float(cost) if cost is not None else None,
            "source": where if ok else f"{where} · failed: {error}",
        })
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
    labels = _shift_labels(info, shift, preset)
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
            "kind": "callout", "at": _utc(at), "time_label": _utc_label(at),
            "title": "Voice call-out" if data.get("channel") == "voice" else "Text call-out",
            "staff": f"{names.get(absent)} · {labels['needs_label']}",
            "language": LANGUAGE_NAMES.get(data.get("language")),
            "audio_url": f"/media/callouts/{code}.ogg" if audio else None,
            "transcript_original": data.get("text"), "transcript_en": data.get("text_en"),
            "model_tag": f"{MODEL_LABELS.get(voice.get('model'), voice.get('model'))} · {voice['duration_ms'] / 1000:.1f} s"
            if voice.get("duration_ms") else None,
        })
    if "night_check_found" in first:
        at, data = first["night_check_found"]
        steps.append({"kind": "night_check", "at": _utc(at), "time_label": _utc_label(at), "title": "Night check",
                      "summary": f"{data['name']}: " + "; ".join(r["text"] for r in data.get("reasons", []))})
    if "ratio_broken" in first:
        at, data = first["ratio_broken"]
        word = preset.copy.get("participants", "participants")
        steps.append({"kind": "ratio", "at": _utc(at), "time_label": _utc_label(at),
                      "title": f"Norm broken: {data['group_name']} — now {data['participants']} {word}, "
                               f"ratio 1:{data['max_per_instructor']}",
                      "summary": f"{data['added']} added by {data['by']}; instructors needed: "
                                 f"{data['instructors_before']} → {data['instructors_needed']}"})
    for _, at, kind, data in events:
        if kind == "flagged_for_manager":
            steps.append({"kind": "flagged", "at": _utc(at), "time_label": _utc_label(at),
                          "title": "Message flagged for the manager",
                          "summary": f"{data.get('name')}: “{data.get('text')}” · the rules were checked anyway"})
    if "callout_understood" in first or "shift_opened" in first:
        at, data = first.get("callout_understood") or first["shift_opened"]
        by = data.get("by")
        steps.append({
            "kind": "shift", "at": _utc(at), "time_label": _utc_label(at), "title": "Shift identified",
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
            "kind": "allowed", "at": _utc(at), "time_label": _utc_label(at), "title": "Who is allowed",
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
        for oid, sid, ostatus, sent, expires, responded, line in offers:
            result, when, _, result_label = _offer_result(ostatus, sid, sent, expires, responded, stood.get(oid), names,
                                                          preset.offer_collect_seconds)
            rows.append({"id": f"O-{oid}", "time_label": _clock(when, tz), "staff": names[sid], "result": result,
                         "result_label": result_label, "why_first": line})
        steps.append({"kind": "offers", "at": _utc(offers[0][3]), "time_label": _utc_label(offers[0][3]),
                      "title": "Offers in order", "offers": rows})
    for _, at, kind, data in events:
        if kind == "approval_refused":
            steps.append({"kind": "plan_b", "at": _utc(at), "time_label": _utc_label(at),
                          "title": "Approval refused: plan changed",
                          "summary": f"{data.get('plan_id')} was shown with hash {str(data.get('shown_hash'))[:8]}…; "
                                     f"the data changed, the current plan has hash {str(data.get('current_hash'))[:8]}…"})
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
        steps.append({"kind": "covered", "at": _utc(at), "time_label": _utc_label(at), "title": "Schedule covered",
                      "summary": f"{labels['time_label'].split('–')[0]} covered by {data.get('name')} · "
                                 f"{cancelled} lesson{'s' if cancelled != 1 else ''} cancelled"})
    if plan:
        pid, snap, digest, pstatus, created, decided, decided_by, chosen = plan
        lines = snap.get("text", {}).get("lines", [])
        if pstatus == "approved":
            option = snap["options"][chosen]
            text = lines[chosen] if chosen < len(lines) else option["kind"]
            steps.append({"kind": "plan_b", "at": _utc(decided), "time_label": _utc_label(decided),
                          "title": "Plan B approved by hash", "summary": f"{text} · approved by {decided_by} · P-{pid}"})
        else:
            steps.append({"kind": "plan_b", "at": _utc(created), "time_label": _utc_label(created),
                          "title": "Plan B waiting for approval", "summary": " · ".join(lines) or _plan_text(snap, preset)})
        proof = {"plan_id": f"P-{pid}", "plan_hash": digest}
    elif status in ("open", "covered"):
        proof = {"plan_id": None, "plan_hash": canonical_key(p._plan_facts(case_id, closed or opened))}
    else:
        proof = {"plan_id": None, "plan_hash": None}
    proof["policy_label"] = f"cover = {policy.autonomy['assign_cover']} · plan B = {policy.autonomy['plan_b']}"
    steps.sort(key=lambda s: s["at"])
    calls = model_calls(conn, case_id, callout_id)
    local_day = info["start"].astimezone(tz)
    return plain({
        "case_id": code,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
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
        "model_cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 6),
        "model_cost_label": money_model.cost_label(sum(c["cost_usd"] or 0 for c in calls)),
        "risk": case_risk(shift, policy),
    })
