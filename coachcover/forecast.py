"""Fragile shifts of a week: how many cleared backups each shift has, and what would fix the thin ones.

One definition for the whole product (coordinator, 07.11.2026):
  backups = people other than the holder whom the core rules clear for the shift (reserve roles not counted,
            as in the offer queue) and who belong to the shift's venue (`forecast.backups_from`: "venue",
            the default; "policy" counts every venue of the school). The other venues' cleared people are
            shown as a separate number and do not change the level;
  at_risk = 0 backups · fragile = 1..threshold backups · safe = more than threshold.
"N fragile shifts" = at_risk + fragile. The threshold is the preset's `forecast.fragile_max_backups` (default 1).
The screen's legend comes from the same thresholds (`legend`), so the screen cannot disagree with the count.

Fixes:
  - put X on standby: the best cleared person of a reserve role for that shift (else the best regular backup);
  - train N more in one certificate or program level: tried for every certificate type and level of the
    preset; N people are picked greedily; the option that makes the most fragile shifts safe wins.
    Course price per person comes from the preset (`forecast.training_cost_usd`), else a placeholder that is
    marked as an assumption.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover.model import Context, Policy, Shift, Staff, StaffCert, StaffProgram
from coachcover.reasons_text import describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, assess, eligible, rank, week_start_date

DEFAULT_THRESHOLD = 1
DEFAULT_SCOPE = "venue"
DEFAULT_COURSE_PEOPLE = 2
PLACEHOLDER_COURSE_USD = 300.0

TONES = {"at_risk": "red", "fragile": "gold", "safe": "green"}
CERT_CODES = {Code.CERT_MISSING, Code.CERT_EXPIRED, Code.CERT_EXPIRES_DURING_SHIFT, Code.CERT_NOT_YET_VALID}
PROGRAM_CODES = {Code.PROGRAM_MISSING, Code.LEVEL_MISSING, Code.PROGRAM_RENEWAL_OVERDUE}


def settings(conn: psycopg.Connection, policy_id: str) -> dict:
    """Forecast settings from the preset as installed (`forecast` block), with defaults."""
    row = conn.execute(
        "SELECT s.source -> 'forecast' FROM policy p JOIN preset s ON s.code = p.preset_code WHERE p.id = %s", (policy_id,)
    ).fetchone()
    block = (row[0] if row else None) or {}
    threshold = block.get("fragile_max_backups", DEFAULT_THRESHOLD)
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        raise ValueError("forecast.fragile_max_backups must be an int >= 0")
    costs = block.get("training_cost_usd") or {}
    scope = block.get("backups_from", DEFAULT_SCOPE)
    if scope not in ("venue", "policy"):
        raise ValueError("forecast.backups_from must be 'venue' or 'policy'")
    return {"threshold": threshold, "course_people": int(block.get("course_people", DEFAULT_COURSE_PEOPLE)),
            "training_cost_usd": costs, "backups_from": scope, "from_preset": bool(block)}


def legend(threshold: int) -> list[dict]:
    """Levels with their backup ranges (max None = no upper bound). Built from the same threshold as the count."""
    out = [{"level": "at_risk", "label": "0 backups", "min": 0, "max": 0, "tone": TONES["at_risk"]}]
    if threshold >= 1:
        label = "1 backup" if threshold == 1 else f"1–{threshold} backups"
        out.append({"level": "fragile", "label": label, "min": 1, "max": threshold, "tone": TONES["fragile"]})
    out.append({"level": "safe", "label": f"{threshold + 1}+ backups", "min": threshold + 1, "max": None,
                "tone": TONES["safe"]})
    return out


def level_of(backups: int, threshold: int) -> str:
    for item in legend(threshold):
        if backups >= item["min"] and (item["max"] is None or backups <= item["max"]):
            return item["level"]
    raise AssertionError("legend does not cover every count")


def _week_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(day, time(0), tzinfo=tz), datetime.combine(day + timedelta(days=7), time(0), tzinfo=tz)


def _cost(settings_: dict, key: str) -> tuple[float, str]:
    costs = settings_["training_cost_usd"]
    if key in costs:
        return float(costs[key]), "preset"
    return PLACEHOLDER_COURSE_USD, "assumption: placeholder, no course price list in the preset"


def _fixes(reason, option: tuple) -> bool:
    kind = option[0]
    if kind == "cert":
        return reason.code in CERT_CODES and reason.params.get("cert_type") == option[1]
    if kind == "level":
        _, program, level = option
        if reason.code in (Code.PROGRAM_MISSING, Code.PROGRAM_RENEWAL_OVERDUE):
            return reason.params.get("program") == program
        if reason.code == Code.LEVEL_MISSING:
            return reason.params.get("program") == program and str(reason.params.get("level")) == level
    return False


def _option_label(policy: Policy, option: tuple) -> str:
    preset = policy.preset
    if option[0] == "cert":
        return preset.cert_types[option[1]].label
    program = preset.programs[option[1]]
    return program.title(option[2])


def training_options(policy: Policy, cells: list[dict], blocked: dict[str, dict[str, list]], threshold: int,
                     settings_: dict) -> list[dict]:
    """For each certificate / level: pick N people greedily; count fragile shifts that become safe."""
    fragile = [c for c in cells if c["level"] in ("at_risk", "fragile")]
    need = {c["shift_id"]: threshold + 1 - c["backups"] for c in fragile}
    options = [("cert", code) for code in policy.preset.cert_types]
    options += [("level", p.code, lv) for p in policy.preset.programs.values() for lv in p.levels]
    n = settings_["course_people"]
    out = []
    for option in options:
        gains: dict[str, set[str]] = {}
        for shift_id in need:
            for staff_id, reasons in blocked.get(shift_id, {}).items():
                if reasons and all(_fixes(r, option) for r in reasons):
                    gains.setdefault(staff_id, set()).add(shift_id)
        if not gains:
            continue
        added: Counter[str] = Counter()
        picked: list[str] = []
        for _ in range(n):
            left = [sid for sid in gains if sid not in picked]
            if not left:
                break
            # most shifts brought to safe now, then most shifts helped, then staff id
            best = min(left, key=lambda sid: (-sum(1 for s in gains[sid] if added[s] + 1 == need[s]),
                                              -sum(1 for s in gains[sid] if added[s] < need[s]), sid))
            picked.append(best)
            for s in gains[best]:
                added[s] += 1
        made_safe = sorted(s for s in need if added[s] >= need[s])
        if not made_safe:
            continue
        key = f"{option[0]}:{':'.join(option[1:])}"
        price, source = _cost(settings_, key)
        out.append({
            "key": key, "kind": option[0], "label": _option_label(policy, option), "people": picked,
            "count": len(picked), "makes_safe": len(made_safe), "of": len(fragile), "shift_ids": made_safe,
            "course_usd_per_person": price, "cost_usd": round(price * len(picked), 2), "cost_source": source,
        })
    out.sort(key=lambda o: (-o["makes_safe"], o["cost_usd"], o["key"]))
    return out


def week(conn: psycopg.Connection, policy_id: str, now: datetime, venue_id: str | None = None,
         start: date | None = None) -> dict:
    """Every shift of the policy week with its backups, level and fixes. Shifts that started are 'past'."""
    policy = load_policy(conn, policy_id)
    conf = settings(conn, policy_id)
    threshold = conf["threshold"]
    venues = conn.execute("SELECT id, name, tz FROM venue WHERE policy_id = %s ORDER BY id", (policy_id,)).fetchall()
    if not venues:
        raise ValueError(f"policy {policy_id} has no venues")
    if venue_id is not None and venue_id not in {v[0] for v in venues}:
        raise ValueError(f"unknown venue {venue_id!r}")
    shown = [v for v in venues if venue_id is None or v[0] == venue_id]
    tz = ZoneInfo(shown[0][2])
    # by default the week starts today at the venue (no grey days already gone); an explicit start snaps to the
    # policy week
    first = week_start_date(start, policy.week_start) if start else now.astimezone(tz).date()
    week_from, week_to = _week_bounds(first, tz)
    ids = [sid for (sid,) in conn.execute(
        "SELECT s.id FROM shift s WHERE s.venue_id = ANY(%s) AND s.status IN ('scheduled', 'open')"
        " AND s.starts_at >= %s AND s.starts_at < %s ORDER BY s.starts_at, s.venue_id, s.id",
        ([v[0] for v in shown], week_from, week_to),
    )]
    staff = load_staff(conn, policy_id)
    names = {s.id: s.name for s in staff}
    reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
    regular = [s for s in staff if not (s.roles & reserve_roles)]
    reserve = [s for s in staff if s.roles & reserve_roles]
    span = Shift(id="week", venue=shown[0][0], role=next(iter(policy.preset.roles)), start=week_from, end=week_to)
    context = load_context(conn, policy_id, span)
    venue_names = {v[0]: v[1] for v in venues}
    cells: list[dict] = []
    blocked: dict[str, dict[str, list]] = {}
    for shift_id in ids:
        shift = load_shift(conn, shift_id)
        vtz = ZoneInfo(next(v[2] for v in venues if v[0] == shift.venue))
        start_local, end_local = shift.start.astimezone(vtz), shift.end.astimezone(vtz)
        status = conn.execute("SELECT status, staff_id FROM shift WHERE id = %s", (shift_id,)).fetchone()
        holder = status[1]
        past = shift.start <= now
        role = policy.preset.roles[shift.role]
        cell = {
            "shift_id": shift_id, "venue_id": shift.venue, "venue_name": venue_names[shift.venue],
            "day": start_local.date().isoformat(), "start": shift.start.isoformat(), "end": shift.end.isoformat(),
            "time_label": f"{start_local:%H:%M}–{end_local:%H:%M}", "role": shift.role, "role_label": role.label,
            "label": f"{start_local:%a %H:%M}–{end_local:%H:%M} · {role.label}"
                     + (f" · {', '.join(dict.fromkeys(_level_label(policy, g) for g in shift.groups))}" if shift.groups else ""),
            "holder": {"staff_id": holder, "name": names.get(holder, holder)} if holder else None,
            "uncovered": status[0] == "open", "past": past,
        }
        if past:
            cell.update({"backups": None, "level": "past", "backup_names": [], "other_venues_cleared": None,
                         "standby": None})
            cells.append(cell)
            continue
        ok_people, elsewhere, reasons_by_person = [], [], {}
        for person in regular:
            counted = conf["backups_from"] == "policy" or person.home_venue == shift.venue
            decision = eligible(person, shift, now, policy, context)
            if decision.ok:
                (ok_people if counted else elsewhere).append(person.id)
            elif counted:
                reasons_by_person[person.id] = [r for r in decision.reasons if r.blocking]
        blocked[shift_id] = reasons_by_person
        backups = len(ok_people)
        cell.update({"backups": backups, "level": level_of(backups, threshold),
                     "backup_names": [names[sid] for sid in ok_people[:3]], "other_venues_cleared": len(elsewhere),
                     "standby": None})
        if cell["level"] != "safe":
            cell["standby"] = _standby(shift, now, policy, context, reserve, regular, ok_people, names)
        cells.append(cell)
    upcoming = [c for c in cells if not c["past"]]
    fragile = [c for c in upcoming if c["level"] in ("at_risk", "fragile")]
    counts = Counter(c["level"] for c in cells)
    options = training_options(policy, upcoming, blocked, threshold, conf)
    days = []
    for i in range(7):
        day = first + timedelta(days=i)
        days.append({"date": day.isoformat(), "label": f"{day:%a} {day.day}",
                     "cells": [c for c in cells if c["day"] == day.isoformat()]})
    best = options[0] if options else None
    return {
        "venue": {"id": shown[0][0], "name": shown[0][1]} if venue_id else None,
        "venues": [{"id": v[0], "name": v[1]} for v in shown],
        "week": {"start": first.isoformat(), "end": (first + timedelta(days=6)).isoformat(),
                 "label": f"{first:%b} {first.day} – {(first + timedelta(days=6)):%b} {(first + timedelta(days=6)).day}"},
        "now": now.isoformat(),
        "threshold": threshold,
        "backups_from": conf["backups_from"],
        "legend": legend(threshold),
        "past_legend": {"level": "past", "label": "Started or over", "tone": "grey"},
        "counts": {"shifts": len(cells), "upcoming": len(upcoming), "at_risk": counts["at_risk"],
                   "fragile": counts["fragile"], "safe": counts["safe"], "past": counts["past"],
                   "fragile_total": len(fragile)},
        "headline": f"{len(fragile)} fragile shifts this week" if fragile else "No fragile shifts this week",
        "fragile": [c["shift_id"] for c in fragile],
        "days": days,
        "fixes": {
            "standby": [{"shift_id": c["shift_id"], "label": c["label"], "level": c["level"], **c["standby"],
                         "text": f"Put {c['standby']['name']} on standby for {c['label']}"}
                        for c in fragile if c["standby"]],
            "training": {
                "best": {**best, "text": f"Train {best['count']} more in {best['label']} → {best['makes_safe']} of "
                                         f"{best['of']} fragile shifts become safe · about ${best['cost_usd']:,.0f}"}
                if best else None,
                "options": options[:5],
            },
        },
        "assumptions": [
            {"name": "fragile_max_backups", "value": threshold,
             "source": "preset forecast block" if conf["from_preset"] else "default (preset has no forecast block)"},
            {"name": "backups_from", "value": conf["backups_from"],
             "source": "preset" if conf["from_preset"] else "default: people of the shift's own venue"},
            {"name": "course_people", "value": conf["course_people"], "source": "preset" if conf["from_preset"] else "default"},
            {"name": "course price per person", "value": PLACEHOLDER_COURSE_USD,
             "source": "placeholder unless the preset sets forecast.training_cost_usd"},
        ],
    }


def _level_label(policy: Policy, group) -> str:
    return policy.preset.programs[group.program].levels[group.level].label


def _standby(shift: Shift, now: datetime, policy: Policy, context: Context, reserve: list[Staff], regular: list[Staff],
             ok_people: list[str], names: dict) -> dict | None:
    """The person to put on standby: the best cleared reserve person, else the best regular backup."""
    ready = [r for r in rank(assess(s, shift, now, policy, context) for s in reserve) if r.candidate.decision.ok]
    if ready:
        r = ready[0]
        return {"staff_id": r.candidate.staff_id, "name": names[r.candidate.staff_id], "from_reserve": True,
                "line": describe_place(r, names[r.candidate.staff_id].split()[0], "en", policy.preset)}
    cleared = [s for s in regular if s.id in ok_people]
    if not cleared:
        return None
    r = rank(assess(s, shift, now, policy, context) for s in cleared)[0]
    return {"staff_id": r.candidate.staff_id, "name": names[r.candidate.staff_id], "from_reserve": False,
            "line": describe_place(r, names[r.candidate.staff_id].split()[0], "en", policy.preset)}


def fragile_summary(report: dict) -> dict:
    """A short form for assistants (MCP): the fragile shifts and the best fixes."""
    cells = {c["shift_id"]: c for d in report["days"] for c in d["cells"]}
    return {
        "week": report["week"], "threshold": report["threshold"], "backups_from": report["backups_from"],
        "legend": report["legend"],
        "headline": report["headline"], "counts": report["counts"],
        "fragile": [{"shift_id": sid, "label": cells[sid]["label"], "venue": cells[sid]["venue_name"],
                     "backups": cells[sid]["backups"], "level": cells[sid]["level"],
                     "standby": (cells[sid]["standby"] or {}).get("name")} for sid in report["fragile"]],
        "best_training": report["fixes"]["training"]["best"],
    }


# kept for tests: a person with a fresh card or level, as a course would give
def with_course(person: Staff, option: tuple, today: date) -> Staff:
    if option[0] == "cert":
        return replace(person, certs=(*person.certs, StaffCert(option[1], today)))
    _, program, level = option
    held = person.programs.get(program)
    levels = {**(held.levels if held else {}), level: today}
    return replace(person, programs={**person.programs, program: StaffProgram(program, today, levels)})
