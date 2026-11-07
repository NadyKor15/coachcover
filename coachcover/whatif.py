""""What if X is off too?" A manager's question, in text or voice, becomes a plan without actions.

1. Voice -> text (speech model, replay in the demo).
2. The text model reads who drops out and when (`whatif_parse`); without an answer, a small parser looks for
   staff first names and "today/tomorrow" words.
3. The real pipeline runs inside one database transaction that is rolled back at the end: the call-out opens
   cases, the first cleared person "says yes" (who steps in), and in a savepoint everyone "says no" to see
   plan B (where a merge would break the norm, what overtime would cost). Inside the dry run no model is
   called: the pipeline uses its own templates and fallbacks, so a plan costs nothing.

The answer carries `dry_run: true`; nothing is sent, booked or stored (row counters of Postgres identity
columns may skip numbers, because sequences are not transactional).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

from coachcover.models import MemoryLedger, ModelClient, ModelError, NoRecording
from coachcover.pipeline import Conflict, Pipeline
from coachcover.repo import load_policy, load_shift
from coachcover.rules import ratio_reason

PARSE_SYSTEM = (
    "You read a manager's what-if question about staff absences. Return JSON only: "
    '{"people": [{"name": "<first name in Latin letters, as on the staff list>", "day": "today" | "tomorrow" | '
    '"day_after_tomorrow"}], "language": "<two-letter code>"}. List only the people the question says would be '
    "absent. Do not invent people."
)
PARSE_SCHEMA = {
    "type": "object",
    "required": ["people"],
    "properties": {
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "day"],
                "properties": {
                    "name": {"type": "string"},
                    "day": {"type": "string", "enum": ["today", "tomorrow", "day_after_tomorrow"]},
                },
            },
        },
        "language": {"type": ["string", "null"]},
    },
}
DAY_OFFSET = {"today": 0, "tomorrow": 1, "day_after_tomorrow": 2}
DAY_WORDS = (
    (2, ("day after tomorrow", "послезавтра", "pasado mañana", "pasado manana")),
    (1, ("tomorrow", "завтра", "mañana", "manana")),
)
_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                 "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))
_CYR_NAMES = {"мария": "maria", "мэри": "mary", "софия": "sofia", "анна": "anna", "марина": "marina",
              "лео": "leo", "ноа": "noah", "кай": "kai", "дмитрий": "dmitri", "елена": "elena", "ирина": "irina"}
OFFER_ANSWER_STEP = timedelta(seconds=30)


class _NoModels(ModelClient):
    """The dry run's model client: every call fails at once, so the pipeline takes its own fallbacks."""

    def __init__(self) -> None:  # noqa: D107 - no recordings, no network, no prices
        self.ledger = MemoryLedger()
        self.mode = "replay"
        self.recordings = None
        self.prices = {}

    def _call(self, **kwargs):
        raise NoRecording("dry run: models are not called")


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _latin(word: str) -> str:
    w = word.casefold()
    if w in _CYR_NAMES:
        return _CYR_NAMES[w]
    return "".join(_CYR.get(ch, ch) for ch in w)


def fallback_parse(text: str, first_names: set[str]) -> dict:
    """Staff first names in the text, and one day word for all of them."""
    low = _fold(text)
    offset = 0
    for days, words in DAY_WORDS:
        if any(_fold(w) in low for w in words):
            offset = days
            break
    words = {_latin(w) for w in re.findall(r"\w+", text)} | {_fold(w) for w in re.findall(r"\w+", text)}
    day = {0: "today", 1: "tomorrow", 2: "day_after_tomorrow"}[offset]
    return {"people": [{"name": name, "day": day} for name in sorted(first_names) if name in words],
            "language": None}


def _staff(conn: psycopg.Connection, policy_id: str) -> list[tuple[str, str]]:
    return conn.execute("SELECT id, full_name FROM staff WHERE policy_id = %s AND active ORDER BY id", (policy_id,)).fetchall()


def resolve(conn: psycopg.Connection, policy_id: str, people: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Names to staff: (found, unknown, ambiguous). A name matches a first name or a full name."""
    staff = _staff(conn, policy_id)
    found, unknown, ambiguous = [], [], []
    for p in people:
        want = _fold(_latin(p["name"]) if re.search("[а-яё]", p["name"].casefold()) else p["name"]).strip()
        hits = [(sid, name) for sid, name in staff if _fold(name) == want or _fold(name).split()[0] == want]
        if len(hits) == 1:
            found.append({**p, "staff_id": hits[0][0], "staff_name": hits[0][1]})
        elif hits:
            ambiguous.append({**p, "matches": [{"staff_id": s, "name": n} for s, n in hits]})
        else:
            unknown.append(p)
    return found, unknown, ambiguous


def _fingerprint(conn: psycopg.Connection) -> str:
    """Rows that a plan could touch, hashed: equal before and after means nothing was stored."""
    parts = []
    for table, cols in (("shift", "id, status, staff_id"), ("cover_case", "id, status"), ("offer", "id, status"),
                        ("plan_b", "id, status"), ("approval", "id, status"), ("message", "id"),
                        ("case_event", "id"), ("remedy", "id"), ("closure", "id"), ("model_call", "id")):
        rows = conn.execute(f"SELECT {cols} FROM {table} ORDER BY 1").fetchall()
        parts.append(f"{table}:{rows!r}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _norm_breaks(conn: psycopg.Connection, policy_id: str, shift_id: str, rejected: list[dict]) -> list[dict]:
    policy = load_policy(conn, policy_id)
    shift = load_shift(conn, shift_id)
    out = []
    for r in rejected or []:
        if r.get("kind") != "merge" or "RATIO_EXCEEDED" not in (r.get("codes") or []):
            continue
        into = load_shift(conn, r["into_shift_id"])
        reason = ratio_reason(into.groups + shift.groups, into.instructors, policy.preset)
        if reason is None:
            continue
        p = reason.params
        tz = ZoneInfo(conn.execute("SELECT tz FROM venue WHERE id = %s", (into.venue,)).fetchone()[0])
        levels = ", ".join(dict.fromkeys(policy.preset.programs[g.program].levels[g.level].label for g in into.groups))
        into_label = f"the {levels} group at {into.start.astimezone(tz):%H:%M}" if levels else f"shift {into.id}"
        out.append({"into_shift_id": into.id, "participants": p["participants"], "capacity": p["capacity"],
                    "norm": f"1:{p['max_per_instructor']}",
                    "text": f"merging with {into_label} would put {p['participants']} participants with "
                            f"{p['instructors']} instructor(s); the norm is 1:{p['max_per_instructor']}"})
    return out


def _option_line(option: dict, lines: list[str], i: int) -> str:
    if i < len(lines):
        return lines[i]
    kind = option.get("kind")
    who = option.get("staff_name")
    return {"merge": f"merge into {option.get('into_shift_id')}", "lead": f"{who} steps in",
            "overtime": f"{who} on overtime", "reschedule": "move the lesson", "close": "close and credit families"
            }.get(kind, kind or "option")


def _plan_b_view(conn: psycopg.Connection, policy_id: str, case_id: int, shift_id: str) -> dict | None:
    row = conn.execute(
        "SELECT snapshot FROM plan_b WHERE case_id = %s AND status IN ('proposed', 'approved') ORDER BY id DESC LIMIT 1",
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    snap = row[0]
    options = snap.get("options") or []
    text = snap.get("text") or {}
    lines = text.get("lines") if isinstance(text, dict) else None
    lines = [ln if isinstance(ln, str) else ln.get("text", "") for ln in (lines or [])]
    rec = snap.get("recommended", 0) if options else None
    out = [{"kind": o.get("kind"), "line": _option_line(o, lines, i), "mode": o.get("mode"),
            "cost_usd": o.get("cost_usd"), "families": o.get("families"), "staff_name": o.get("staff_name")}
           for i, o in enumerate(options)]
    return {"options": out, "recommended": out[rec]["kind"] if out and rec is not None and rec < len(out) else None,
            "norm_breaks": _norm_breaks(conn, policy_id, shift_id, snap.get("rejected") or [])}


def _callout_time(now: datetime, day: date, first_start: datetime, tz: ZoneInfo) -> datetime:
    """When the absence would be reported: now for today; the same clock time on a later day, before the shift."""
    if day == now.astimezone(tz).date():
        return now
    at = datetime.combine(day, now.astimezone(tz).time(), tzinfo=tz)
    if at >= first_start:
        at = first_start - timedelta(minutes=30)
    return max(at, now)


def _simulate(conn: psycopg.Connection, policy_id: str, person: dict, now: datetime, tz: ZoneInfo) -> dict:
    day = now.astimezone(tz).date() + timedelta(days=DAY_OFFSET[person["day"]])
    start_day = datetime.combine(day, time(0), tzinfo=tz)
    shifts = conn.execute(
        "SELECT id, starts_at FROM shift WHERE staff_id = %s AND status = 'scheduled' AND starts_at >= %s"
        " AND starts_at < %s AND ends_at > %s ORDER BY starts_at, id",
        (person["staff_id"], start_day, start_day + timedelta(days=1), now),
    ).fetchall()
    out = {"staff_id": person["staff_id"], "staff_name": person["staff_name"], "date": day.isoformat(),
           "day": person["day"], "shifts": []}
    if not shifts:
        out["note"] = f"{person['staff_name']} has no shifts left on {day:%a %b %d}"
        return out
    at = _callout_time(now, day, shifts[0][1], tz)
    out["reported_at"] = at.isoformat()
    p = Pipeline(conn, _NoModels(), policy_id)
    try:
        result = p.callout(person["staff_id"], now=at, text=f"(what-if) {person['staff_name']} is off on {day:%Y-%m-%d}")
    except Conflict as exc:
        out["note"] = f"not simulated: {exc}"
        return out
    for case in result.get("cases", []):
        out["shifts"].append(_case_plan(conn, p, policy_id, case["case_id"], case["shift_id"], at))
    return out


def _case_plan(conn: psycopg.Connection, p: Pipeline, policy_id: str, case_id: int, shift_id: str, at: datetime) -> dict:
    info = p.shift_info(shift_id)
    ranked = conn.execute(
        "SELECT data FROM case_event WHERE case_id = %s AND kind = 'candidates_ranked' ORDER BY seq DESC LIMIT 1", (case_id,)
    ).fetchone()
    ranked = ranked[0] if ranked else {}
    plan = {"shift_id": shift_id, "label": info["label"],
            "time_label": f"{info['local_start']:%a %H:%M}–{info['local_end']:%H:%M}",
            "cleared": ranked.get("eligible"), "queue": (ranked.get("top") or [])[:3]}
    # if nobody says yes: everyone declines, in a savepoint
    with conn.transaction() as savepoint:
        t = at
        while True:
            row = conn.execute("SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)).fetchone()
            if row is None:
                break
            t += OFFER_ANSWER_STEP
            p.respond(row[0], False, t)
        plan["if_nobody_says_yes"] = _plan_b_view(conn, policy_id, case_id, shift_id)
        raise psycopg.Rollback(savepoint)
    # the first cleared person says yes
    row = conn.execute(
        "SELECT o.id, o.staff_id, s.full_name, o.place ->> 'line' FROM offer o JOIN staff s ON s.id = o.staff_id"
        " WHERE o.case_id = %s AND o.status = 'pending'",
        (case_id,),
    ).fetchone()
    if row is None:
        plan["who_steps_in"] = None
    else:
        offer_id, staff_id, name, line = row
        state = p.respond(offer_id, True, at + OFFER_ANSWER_STEP)
        plan["who_steps_in"] = {"staff_id": staff_id, "name": name, "line": line,
                                "needs_manager_approval": state.get("case_status") == "open"}
    nobody = plan["if_nobody_says_yes"] or {}
    rec = next((o for o in nobody.get("options", []) if o["kind"] == nobody.get("recommended")), None)
    plan["cost"] = {
        "if_someone_says_yes_usd": 0.0,
        "if_nobody_says_yes_usd": float(rec["cost_usd"]) if rec and rec.get("cost_usd") is not None else 0.0,
        "if_nobody_families_affected": rec.get("families") if rec else None,
        "basis": "overtime price from the policy; merges, reserve and moves add no pay; credits are counted, not priced",
    }
    return plan


def run(conn: psycopg.Connection, policy_id: str, now: datetime, *, text: str | None = None, audio: bytes | None = None,
        filename: str = "question.ogg", models: ModelClient | None = None) -> dict:
    if (text is None) == (audio is None):
        raise ValueError("give either text or audio")
    if text is not None and not text.strip():
        raise ValueError("empty question")
    models = models or ModelClient(MemoryLedger())
    calls = []
    heard_from = "text"
    if audio is not None:
        heard = models.transcribe(audio, filename, at=now, ref={"whatif": True})
        text, heard_from = heard.data["text"], "voice"
    first_names = {_fold(name).split()[0] for _, name in _staff(conn, policy_id)}
    tz = ZoneInfo(conn.execute("SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (policy_id,)).fetchone()[0])
    today = now.astimezone(tz).date()
    try:
        answer = models.chat_json(purpose="whatif_parse", system=PARSE_SYSTEM,
                                  user=f"Today is {today:%A %Y-%m-%d}. Question: «{text.strip()}»",
                                  schema=PARSE_SCHEMA, key={"question": text.strip()}, at=now, ref={"whatif": True})
        parsed, by = answer.data, answer.record.model_returned
    except ModelError:
        parsed, by = fallback_parse(text, first_names), "fallback"
    for record in getattr(models.ledger, "calls", []):
        calls.append({"purpose": record.purpose, "model": record.model_returned or record.model_requested,
                      "source": record.source, "ok": record.ok, "synthetic": bool(record.ref.get("synthetic")),
                      "duration_ms": record.duration_ms, "cost_usd": record.cost_usd})
    found, unknown, ambiguous = resolve(conn, policy_id, parsed.get("people") or [])
    before = _fingerprint(conn)
    plans = []
    with conn.transaction() as tx:
        for person in found:
            plans.append(_simulate(conn, policy_id, person, now, tz))
        raise psycopg.Rollback(tx)
    after = _fingerprint(conn)
    shifts = [s for p in plans for s in p["shifts"]]
    covered = [s for s in shifts if s.get("who_steps_in")]
    breaks = sum(len((s.get("if_nobody_says_yes") or {}).get("norm_breaks") or []) for s in shifts)
    worst = round(sum(s["cost"]["if_nobody_says_yes_usd"] for s in shifts), 2)
    names = ", ".join(f"{p['staff_name']} off {p['day'].replace('_', ' ')}" for p in plans) or "nobody recognised"
    steps_in = ", ".join(s["who_steps_in"]["name"].split()[0] for s in covered)
    headline = (f"{names}: {len(shifts)} shift(s) · {len(covered)} covered" + (f" ({steps_in})" if steps_in else "")
                + f" · {breaks} norm break(s) if nobody says yes · worst case ${worst:,.2f}")
    return {
        "dry_run": True,
        "question": {"text": text, "heard_from": heard_from, "language": parsed.get("language")},
        "understood": {"by": by, "people": found, "unknown": unknown, "ambiguous": ambiguous},
        "plan": plans,
        "summary": {"shifts": len(shifts), "covered": len(covered), "need_plan_b": len(shifts) - len(covered),
                    "norm_breaks_if_nobody": breaks, "worst_case_usd": worst, "headline": headline},
        "model_calls": calls,
        "database": {"before": before, "after": after, "unchanged": before == after},
        "note": "Plan only: the pipeline ran in a transaction that was rolled back. Nothing was sent, booked or stored.",
        "computed_at": now.astimezone(UTC).isoformat(),
    }
