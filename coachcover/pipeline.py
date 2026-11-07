"""The cover pipeline on top of the rules.

call-out (voice or text) -> which shifts -> a case per shift -> ranked candidates
-> offers one at a time with a timer -> the first eligible "yes" is booked,
or, when nobody is left, plan B (merge, reserve steps in, overtime, move, close)
approved against the hash of the plan that was shown. Also: closures with
family messages and remedies, and reading certificate cards with two readers.

Every step that changes the schedule goes through the policy's autonomy setting:
"auto" does it, "propose" leaves a card for a person, "off" does nothing.
Every step writes an event to the case file. `now` is always passed in, so the
API and the worker can run on the demo clock.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coachcover import messages as M
from coachcover.model import Policy
from coachcover.models import SECOND_READER_MODEL, TEXT_MODEL, ModelClient, ModelError, canonical_key, sha256_bytes
from coachcover.planb import (is_private, own_coach_slot, plan_b_options, private_plan_options,  # noqa: F401
                              ratio_plan_options, strictest)  # strictest kept importable
from coachcover.reasons_text import describe, describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, Ranked, assess, eligible, group_norm, rank, ratio_reason



class PipelineError(Exception):
    pass


class NotFound(PipelineError):
    pass


class Conflict(PipelineError):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    raise TypeError(f"not JSON: {type(value).__name__}")


def plain(value: Any) -> Any:
    """The value as it will come back from a jsonb column."""
    return json.loads(json.dumps(value, default=_default, ensure_ascii=False))


class Pipeline:
    def __init__(self, conn: psycopg.Connection, models: ModelClient, policy_id: str):
        self.conn = conn
        self.models = models
        self.policy_id = policy_id

    # --- small helpers ------------------------------------------------------

    def _policy(self) -> Policy:
        return load_policy(self.conn, self.policy_id)

    def _event(self, case_id: int, at: datetime, actor: str, kind: str, data: dict | None = None) -> None:
        seq = self.conn.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM case_event WHERE case_id = %s", (case_id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO case_event (case_id, seq, at, actor, kind, data) VALUES (%s, %s, %s, %s, %s, %s)",
            (case_id, seq, at, actor, kind, Jsonb(plain(data or {}))),
        )

    def _message(self, at: datetime, audience: str, language: str, body: str, **links: Any) -> int:
        cols = ["created_at", "audience", "language", "body", *links]
        return self.conn.execute(
            f"INSERT INTO message ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
            (at, audience, language, body, *links.values()),
        ).fetchone()[0]

    def _staff_row(self, staff_id: str) -> dict:
        row = self.conn.execute(
            "SELECT s.id, s.full_name, s.language, s.home_venue_id, v.tz FROM staff s"
            " LEFT JOIN venue v ON v.id = s.home_venue_id WHERE s.id = %s AND s.policy_id = %s",
            (staff_id, self.policy_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown staff {staff_id!r}")
        return dict(zip(("id", "name", "language", "home_venue_id", "tz"), row))

    def shift_info(self, shift_id: str) -> dict:
        row = self.conn.execute(
            "SELECT s.id, s.venue_id, v.name, v.tz, s.zone_id, z.name, r.code, r.label, s.starts_at, s.ends_at,"
            " s.staff_id, s.status FROM shift s JOIN venue v ON v.id = s.venue_id JOIN role r ON r.id = s.role_id"
            " LEFT JOIN zone z ON z.id = s.zone_id WHERE s.id = %s AND v.policy_id = %s",
            (shift_id, self.policy_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown shift {shift_id!r}")
        keys = ("id", "venue_id", "venue", "tz", "zone_id", "zone", "role", "role_label", "start", "end", "staff_id", "status")
        info = dict(zip(keys, row))
        info["groups"] = [
            dict(zip(("id", "name", "size", "languages", "level", "level_label", "program", "kind", "participant"), g))
            for g in self.conn.execute(
                "SELECT g.id, g.name, g.size, g.family_languages, l.code, l.label, p.code, g.kind, g.participant"
                " FROM shift_group sg"
                " JOIN lesson_group g ON g.id = sg.group_id JOIN program_level l ON l.id = g.level_id"
                " JOIN program p ON p.id = l.program_id WHERE sg.shift_id = %s ORDER BY g.id",
                (shift_id,),
            )
        ]
        tz = ZoneInfo(info["tz"])
        start, end = info["start"].astimezone(tz), info["end"].astimezone(tz)
        levels = ", ".join(dict.fromkeys(g["level_label"] for g in info["groups"]))
        info["local_start"], info["local_end"] = start, end
        info["private"] = bool(info["groups"]) and all(g["kind"] == "private" for g in info["groups"])
        info["label"] = f"{start:%a %H:%M}-{end:%H:%M} · {info['role_label']}" + (f" · {levels}" if levels else "") + f" · {info['venue']}"
        return info

    def _when(self, info: dict) -> str:
        return f"{info['local_start']:%a %d.%m %H:%M}-{info['local_end']:%H:%M}"

    def _what(self, info: dict) -> str:
        return (info["role_label"] + (f", {info['groups'][0]['level_label']}" if info["groups"] else "")
                + (", private lesson" if info.get("private") else ""))

    # --- candidates -----------------------------------------------------------

    def ranked(self, shift_id: str, now: datetime) -> tuple[list[Ranked], dict, Policy, list[str]]:
        """All non-reserve staff, ranked for the shift; plus a name map and the reserve people's ids."""
        policy = self._policy()
        staff = load_staff(self.conn, self.policy_id)
        shift = load_shift(self.conn, shift_id)
        context = load_context(self.conn, self.policy_id, shift)
        reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
        people = [s for s in staff if not (s.roles & reserve_roles)]
        names = {s.id: s.name for s in staff}
        reserve = [s.id for s in staff if s.roles & reserve_roles]
        return rank(assess(s, shift, now, policy, context) for s in people), names, policy, reserve

    def candidates(self, shift_id: str, now: datetime) -> dict:
        ranked, names, policy, reserve_ids = self.ranked(shift_id, now)
        reserve = len(reserve_ids)
        rows = []
        chips: Counter[str] = Counter()  # each filtered person counted once, by the first blocking reason
        for r in ranked:
            c = r.candidate
            blocking = [x for x in c.decision.reasons if x.blocking]
            if blocking:
                chips[blocking[0].code.value] += 1
            rows.append(
                {
                    "position": r.position,
                    "staff_id": c.staff_id,
                    "name": names[c.staff_id],
                    "eligible": c.decision.ok,
                    "line": describe_place(r, names[c.staff_id].split()[0], "en", policy.preset),
                    "place": r.place(),
                    "reasons": [
                        {"code": x.code.value, "severity": x.severity, "params": dict(x.params), "text": describe(x, "en", policy.preset)}
                        for x in c.decision.reasons
                    ],
                }
            )
        eligible_count = sum(1 for r in rows if r["eligible"])
        grid = []
        for q in rows:
            blocking = [x for x in q["reasons"] if x["severity"] == "block"]
            grid.append({
                "staff_id": q["staff_id"], "name": q["name"], "position": q["position"], "eligible": q["eligible"],
                "reserve": False, "week_hours": q["place"]["week_hours"],
                "first": {"code": blocking[0]["code"], "params": blocking[0]["params"]} if blocking else None,
                "blocking_codes": [x["code"] for x in blocking],
                "warnings": [{"code": x["code"], "params": x["params"]} for x in q["reasons"] if x["severity"] == "warn"],
            })
        for sid in reserve_ids:
            grid.append({"staff_id": sid, "name": names[sid], "position": None, "eligible": False, "reserve": True,
                         "week_hours": None, "first": {"code": "RESERVE", "params": {}}, "blocking_codes": ["RESERVE"],
                         "warnings": []})
        if reserve:
            chips["RESERVE"] = reserve
        return {
            "shift_id": shift_id,
            "total": len(rows) + reserve,
            "reserve": reserve,
            "eligible": eligible_count,
            "filtered": len(rows) - eligible_count,
            "chips": [{"code": code, "count": n} for code, n in sorted(chips.items(), key=lambda kv: (-kv[1], kv[0]))],
            "queue": rows,
            "grid": grid,
        }

    # --- call-out -------------------------------------------------------------

    def _screen(self, text: str, now: datetime, ref: dict, case_id: int | None = None) -> dict:
        """Content-safety verdict for a staff message. It never decides eligibility: it only warns the manager."""
        policy_text = self._policy().preset.copy.get("safety_policy", M.SAFETY_FALLBACK)
        try:
            answer = self.models.screen_message(text, policy_text, at=now, case_id=case_id, ref=ref)
            return {**answer.data, "model": answer.record.model_returned}
        except ModelError as exc:
            return {"verdict": "unknown", "error": str(exc), "model": None}

    def _translate(self, text: str, language: str | None, now: datetime, ref: dict) -> tuple[str | None, str]:
        if language == "en":
            return text, "original"
        try:
            answer = self.models.chat_json(purpose="translate", system=M.TRANSLATE_SYSTEM, user=text,
                                           schema=M.TRANSLATE_SCHEMA, key={"text": text, "to": "en"}, at=now, ref=ref)
            return answer.data["text"], "model"
        except ModelError:
            return None, "none"

    def callout(self, staff_id: str, *, now: datetime, text: str | None = None, audio: bytes | None = None,
                filename: str = "voice.ogg") -> dict:
        if (text is None) == (audio is None):
            raise ValueError("give either text or audio")
        if text is not None and not text.strip():
            raise ValueError("empty message")
        person = self._staff_row(staff_id)
        tz = ZoneInfo(person["tz"] or self.conn.execute(
            "SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (self.policy_id,)).fetchone()[0])
        callout_id = str(uuid.uuid4())
        ref = {"callout_id": callout_id, "staff_id": staff_id}
        voice = None
        if audio is not None:
            heard = self.models.transcribe(audio, filename, at=now, ref=ref)
            text = heard.data["text"]
            voice = {"model": heard.record.model_returned, "duration_ms": heard.record.duration_ms,
                     "cost_usd": heard.record.cost_usd, "recorded": heard.record.source == "recorded",
                     "synthetic": heard.synthetic, "seconds": heard.data.get("duration")}
        today = now.astimezone(tz).date()
        todays = [
            (sid, start)
            for sid, start in self.conn.execute(
                "SELECT id, starts_at FROM shift WHERE staff_id = %s AND status = 'scheduled' AND ends_at > %s ORDER BY starts_at, id",
                (staff_id, now),
            )
            if start.astimezone(tz).date() == today
        ]
        screened = self._screen(text, now, ref)
        understood, by, error = None, "fallback", None
        try:
            prompt = (
                f"Today is {today:%A %Y-%m-%d}. Staff member: {person['name']}. "
                f"Their shifts today start at: {', '.join(f'{s.astimezone(tz):%H:%M}' for _, s in todays) or 'none'}.\n"
                f"Message: «{text}»"
            )
            answer = self.models.chat_json(purpose="extract_callout", system=M.CALLOUT_SYSTEM, user=prompt,
                                           schema=M.CALLOUT_SCHEMA, key={"transcript": text}, at=now, ref=ref)
            understood, by = answer.data, answer.record.model_returned
        except ModelError as exc:
            error = str(exc)
        language = understood["language"] if understood and understood["language"] != "other" else None
        text_en, translated_by = self._translate(text, language, now, ref)
        result = {"callout_id": callout_id, "staff_id": staff_id, "text": text, "text_en": text_en,
                  "understood": understood, "screened": screened, "cases": []}
        if understood is not None and not understood["absent"]:
            result["note"] = "the message is not a call-out"
            return result
        matched = todays
        if understood and understood["start_times"]:
            wanted = set(understood["start_times"])
            matched = [(sid, s) for sid, s in todays if f"{s.astimezone(tz):%H:%M}" in wanted] or todays
        if not matched:
            result["note"] = "no shifts left today"
            return result
        # a person who calls out is out for the whole day (the rules offer them nothing that day): the shifts the
        # message names come first, then the rest of the day's shifts, so none of them is left without a person
        named = {sid for sid, _ in matched}
        rest_of_day = [(sid, s) for sid, s in todays if sid not in named]
        matched = [*matched, *rest_of_day]
        opened = []
        with self.conn.transaction():
            if audio is not None:
                self.conn.execute(
                    "INSERT INTO callout_audio (callout_id, filename, mime, seconds, data) VALUES (%s, %s, %s, %s, %s)",
                    (callout_id, filename, _audio_mime(filename), voice.get("seconds"), audio),
                )
            for shift_id, _ in matched:
                case_id = self._open_case(shift_id, staff_id, now, "callout", callout_id)
                info = self.shift_info(shift_id)
                self._event(case_id, now, "staff", "callout_received",
                            {"channel": "voice" if audio is not None else "text", "text": text, "text_en": text_en,
                             "translated_by": translated_by, "language": language,
                             "staff_id": staff_id, "staff_name": person["name"], "callout_id": callout_id})
                if voice:
                    self._event(case_id, now, "agent", "voice_transcribed", voice)
                self._screen_events(case_id, now, staff_id, person["name"], text, screened)
                self._event(case_id, now, "agent", "callout_understood",
                            {"by": by, "error": error, "understood": understood,
                             "matched_shift_ids": [sid for sid, _ in matched if sid in named],
                             "rest_of_day_shift_ids": [sid for sid, _ in rest_of_day]})
                self._event(case_id, now, "agent", "shift_opened", {
                    "shift_id": shift_id, "label": info["label"],
                    **({"why": "out for the whole day"} if shift_id not in named else {}),
                    **({"private": True} if info["private"] else {}),
                })
                opened.append((case_id, shift_id))
        for case_id, shift_id in opened:
            self._rank_event(case_id, shift_id, now)
            self._offer_next(case_id, now)
            result["cases"].append({"case_id": case_id, "code": self.case_code(case_id), "shift_id": shift_id})
        return result

    def _screen_events(self, case_id: int, now: datetime, staff_id: str, name: str, text: str, screened: dict) -> None:
        self._event(case_id, now, "agent", "message_screened", {"staff_id": staff_id, **screened})
        if screened["verdict"] == "unsafe":
            self._event(case_id, now, "agent", "flagged_for_manager", {
                "staff_id": staff_id, "name": name, "text": text,
                "why": "the message tries to change the rules; eligibility is still checked by code",
            })

    def _rank_event(self, case_id: int, shift_id: str, now: datetime) -> dict:
        """Snapshot of who may take the shift at this moment: the evidence for the screens."""
        summary = self.candidates(shift_id, now)
        with self.conn.transaction():
            self._event(case_id, now, "agent", "candidates_ranked", {
                "total": summary["total"], "reserve": summary["reserve"], "eligible": summary["eligible"],
                "filtered": summary["filtered"], "chips": summary["chips"],
                "top": [q["line"] for q in summary["queue"][:5]],
                "grid": summary["grid"],
            })
        return summary

    def case_code(self, case_id: int) -> str:
        return self.conn.execute("SELECT code FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]

    def _insert_case(self, shift_id: str, absent: str | None, now: datetime, source: str, callout_id: str | None) -> int:
        open_case = self.conn.execute(
            "SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)
        ).fetchone()
        if open_case:
            raise Conflict(f"shift {shift_id} already has an open case", {"case_id": open_case[0]})
        tz = ZoneInfo(self.shift_info(shift_id)["tz"])
        prefix = f"C-{now.astimezone(tz):%m%d}-"
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (prefix,))
        n = self.conn.execute("SELECT count(*) + 1 FROM cover_case WHERE code LIKE %s", (prefix + "%",)).fetchone()[0]
        return self.conn.execute(
            "INSERT INTO cover_case (shift_id, absent_staff_id, source, opened_at, callout_id, code)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (shift_id, absent, source, now, callout_id, f"{prefix}{n:02d}"),
        ).fetchone()[0]

    def _open_shift(self, shift_id: str) -> None:
        self.conn.execute("SELECT 1 FROM shift WHERE id = %s FOR UPDATE", (shift_id,))
        self.conn.execute("UPDATE shift SET status = 'open', staff_id = NULL WHERE id = %s", (shift_id,))
        self.conn.execute("UPDATE offer SET status = 'released' WHERE shift_id = %s AND status = 'accepted'", (shift_id,))

    def _open_case(self, shift_id: str, absent: str | None, now: datetime, source: str, callout_id: str | None) -> int:
        case_id = self._insert_case(shift_id, absent, now, source, callout_id)
        self._open_shift(shift_id)
        return case_id

    def find_case(self, ref: str | int) -> int:
        """A case by its code (C-1109-01) or its number."""
        text = str(ref)
        row = self.conn.execute(
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
            " WHERE v.policy_id = %s AND (c.code = %s OR c.id::text = %s)",
            (self.policy_id, text, text.removeprefix("C-")),
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown case {ref}")
        return row[0]

    # --- volunteers and the night check ------------------------------------------

    def volunteer(self, case_id: int, staff_id: str, text: str, now: datetime) -> dict:
        """Someone offers to take an open shift. The message is screened; the rules decide, as always."""
        row = self.conn.execute("SELECT shift_id, status FROM cover_case WHERE id = %s", (case_id,)).fetchone()
        if row is None:
            raise NotFound(f"unknown case {case_id}")
        shift_id, status = row
        if status != "open":
            raise Conflict(f"case {case_id} is {status}")
        person = self._staff_row(staff_id)
        screened = self._screen(text, now, {"case_id": case_id, "staff_id": staff_id}, case_id=case_id)
        policy = self._policy()
        staff = next(s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id)
        shift = load_shift(self.conn, shift_id)
        decision = eligible(staff, shift, now, policy, load_context(self.conn, self.policy_id, shift))
        reasons = [{"code": r.code.value, "params": dict(r.params), "text": describe(r, "en", policy.preset)}
                   for r in decision.reasons if r.blocking]
        booked = False
        with self.conn.transaction():
            self.conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (case_id,))
            self._event(case_id, now, "staff", "volunteered", {"staff_id": staff_id, "name": person["name"], "text": text})
            self._screen_events(case_id, now, staff_id, person["name"], text, screened)
            if reasons:
                self._event(case_id, now, "agent", "volunteer_blocked",
                            {"staff_id": staff_id, "name": person["name"], "reasons": reasons})
                lang = M.lang_or_en(person["language"])
                info = self.shift_info(shift_id)
                self._message(now, "staff", lang, M.NO_LONGER[lang].format(
                    when=self._when(info), why="; ".join(r["text"] for r in reasons)), staff_id=staff_id, case_id=case_id)
            elif policy.autonomy["assign_cover"] == "auto":
                self.conn.execute("UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status = 'pending'", (case_id,))
                self._book(case_id, shift_id, staff_id, now, via={"volunteer": True})
                booked = True
        return {"case_id": case_id, "staff_id": staff_id, "eligible": not reasons, "reasons": reasons,
                "screened": screened, "booked": booked}

    def night_cover(self, report: dict, now: datetime) -> list[dict]:
        """Open a cover case for each problem of the night check, as the policy says (night_cover)."""
        mode = self._policy().autonomy["night_cover"]
        opened = []
        if mode == "off":
            return opened
        for problem in sorted(report["problems"], key=lambda p: (p["start"], p["shift_id"])):
            shift_id, staff_id = problem["shift_id"], problem["staff_id"]
            if self.conn.execute("SELECT 1 FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)).fetchone():
                continue
            info = self.shift_info(shift_id)
            with self.conn.transaction():
                if mode == "auto":
                    case_id = self._open_case(shift_id, staff_id, now, "night_check", None)
                else:
                    case_id = self._insert_case(shift_id, staff_id, now, "night_check", None)
                self._event(case_id, now, "agent", "night_check_found", {
                    "staff_id": staff_id, "name": problem["name"], "shift_id": shift_id, "label": info["label"],
                    "reasons": problem["reasons"], "for_date": report["for_date"],
                })
                if mode == "propose":
                    approval = self.conn.execute(
                        "INSERT INTO approval (case_id, action, payload, created_at) VALUES (%s, 'night_cover', %s, %s) RETURNING id",
                        (case_id, Jsonb({"shift_id": shift_id, "staff_id": staff_id, "reasons": problem["reasons"]}), now),
                    ).fetchone()[0]
                    self._event(case_id, now, "agent", "awaiting_approval",
                                {"approval_id": approval, "action": "night_cover", "staff_id": staff_id})
                    self._message(now, "manager", "en", f"Night check: {problem['name']} may not work {info['label']}: "
                                  + "; ".join(r["text"] for r in problem["reasons"]) + ". Find a replacement?", case_id=case_id)
            self._rank_event(case_id, shift_id, now)
            if mode == "auto":
                self._offer_next(case_id, now)
            opened.append({"case_id": case_id, "code": self.case_code(case_id), "shift_id": shift_id,
                           "staff_id": staff_id, "mode": mode})
        return opened

    # --- standby for a fragile shift -------------------------------------------------

    def propose_standby(self, shift_id: str, staff_id: str, now: datetime, by: str = "manager") -> dict:
        """Propose a person as standby for a shift. Only a proposal (autonomy "propose"): nothing is booked. The
        person must be cleared for the shift by the rules right now; the holder cannot be their own standby."""
        info = self.shift_info(shift_id)
        person = self._staff_row(staff_id)
        policy = self._policy()
        staff = next((s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id), None)
        if staff is None:
            raise NotFound(f"unknown person {staff_id}")
        shift = load_shift(self.conn, shift_id)
        decision = eligible(staff, shift, now, policy, load_context(self.conn, self.policy_id, shift))
        blocking = [r for r in decision.reasons if r.blocking]
        if blocking:
            raise Conflict(f"{person['name']} is not cleared for {info['label']}: "
                           + "; ".join(describe(r, "en", policy.preset) for r in blocking),
                           {"codes": [r.code.value for r in blocking]})
        text = f"Standby proposed: {person['name']} for {info['label']}. Nothing is booked until the manager approves."
        with self.conn.transaction():
            try:
                with self.conn.transaction():
                    row = self.conn.execute(
                        "INSERT INTO standby_proposal (shift_id, staff_id, text, created_by, created_at)"
                        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (shift_id, staff_id, text, by, now),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                raise Conflict(f"{person['name']} is already proposed as standby for {info['label']}") from None
            case = self.conn.execute("SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)).fetchone()
            if case:
                self._event(case[0], now, "manager", "standby_proposed",
                            {"standby_id": f"SB-{row[0]}", "staff_id": staff_id, "name": person["name"], "by": by})
        return {"id": f"SB-{row[0]}", "shift_id": shift_id, "staff_id": staff_id, "name": person["name"],
                "status": "proposed", "mode": "propose", "text": text}

    # --- enrolment: the children-per-instructor norm, live --------------------------

    RATIO_HORIZON = timedelta(days=7)

    def enroll(self, group_id: str, added: int, now: datetime, *, language: str | None = None,
               by: str = "front desk") -> dict:
        """Children added to a group. The norm is checked again on the group's shifts of the coming week; a shift
        over the norm gets one more instructor seat (a shift that teaches the same groups at the same time) and a
        cover case for it, filled from the same queue by the same rules. Nobody presses anything."""
        if not isinstance(added, int) or isinstance(added, bool) or not 1 <= added <= 100:
            raise ValueError("add must be a whole number of children from 1 to 100")
        if language is not None and not re.fullmatch(r"[a-z]{2}", language):
            raise ValueError("language must be a two-letter code, e.g. es")
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT g.size, g.family_counts, g.name FROM lesson_group g JOIN venue v ON v.id = g.venue_id"
                " WHERE g.id = %s AND v.policy_id = %s FOR UPDATE OF g",
                (group_id, self.policy_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown group {group_id}")
            size, counts, name = row
            if self.conn.execute("SELECT kind FROM lesson_group WHERE id = %s", (group_id,)).fetchone()[0] == "private":
                raise ValueError("a private lesson has one child; add a new private lesson instead")
            counts = {k: int(v) for k, v in counts.items()}
            if language is not None:
                counts[language] = counts.get(language, 0) + added
            self.conn.execute("UPDATE lesson_group SET size = size + %s, family_counts = %s WHERE id = %s",
                              (added, Jsonb(counts), group_id))
            self.conn.execute(
                "INSERT INTO group_enrollment (group_id, at, added, language, size_before, size_after, by_whom)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (group_id, now, added, language, size, size + added, by),
            )
        shift_ids = [r[0] for r in self.conn.execute(
            "SELECT s.id FROM shift s JOIN shift_group sg ON sg.shift_id = s.id WHERE sg.group_id = %s"
            " AND s.extra_for IS NULL AND s.status IN ('scheduled', 'open') AND s.starts_at > %s AND s.starts_at < %s"
            " ORDER BY s.starts_at, s.id",
            (group_id, now, now + self.RATIO_HORIZON),
        )]
        alerts = [a for a in (self._ratio_check(sid, group_id, name, size, added, now, by) for sid in shift_ids) if a]
        return {"group_id": group_id, "name": name, "size_before": size, "size_after": size + added,
                "checked_shifts": shift_ids, "alerts": alerts}

    def _ratio_check(self, shift_id: str, group_id: str, name: str, size_before: int, added: int, now: datetime,
                     by: str) -> dict | None:
        policy = self._policy()
        shift = load_shift(self.conn, shift_id)
        reason = ratio_reason(shift.groups, shift.instructors, policy.preset)
        if reason is None:
            return None
        per, participants = reason.params["max_per_instructor"], reason.params["participants"]
        needed = -(-participants // per)
        info = self.shift_info(shift_id)
        opened = []
        with self.conn.transaction():
            self.conn.execute("SELECT 1 FROM shift WHERE id = %s FOR UPDATE", (shift_id,))
            self.conn.execute("UPDATE shift SET instructors = %s WHERE id = %s OR extra_for = %s", (needed, shift_id, shift_id))
            for seat in range(shift.instructors + 1, needed + 1):
                extra = f"{shift_id}-I{seat}"
                self.conn.execute(
                    "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id, instructors,"
                    " extra_certs, status, extra_for) SELECT %s, venue_id, zone_id, role_id, starts_at, ends_at, NULL, %s,"
                    " extra_certs, 'open', id FROM shift WHERE id = %s",
                    (extra, needed, shift_id),
                )
                self.conn.execute("INSERT INTO shift_group (shift_id, group_id) SELECT %s, group_id FROM shift_group"
                                  " WHERE shift_id = %s", (extra, shift_id))
                case_id = self._insert_case(extra, None, now, "ratio", None)
                self._event(case_id, now, "agent", "ratio_broken", {
                    "group_id": group_id, "group_name": name, "size_before": size_before, "added": added, "by": by,
                    "shift_id": shift_id, "extra_shift_id": extra, "label": info["label"],
                    "participants": participants, "max_per_instructor": per, "instructors_before": shift.instructors,
                    "instructors_needed": needed, "reason": plain(dict(reason.params)),
                })
                self._event(case_id, now, "agent", "shift_opened", {"shift_id": extra, "label": info["label"]})
                self._message(now, "manager", "en", f"{name}: now {participants} children for {shift.instructors}"
                              f" instructor(s), the norm is 1:{per}. Looking for instructor no. {seat}.", case_id=case_id)
                opened.append((case_id, extra))
        for case_id, extra in opened:
            self._rank_event(case_id, extra, now)
            self._offer_next(case_id, now)
        return {"shift_id": shift_id, "label": info["label"], "participants": participants, "max_per_instructor": per,
                "instructors_before": shift.instructors, "instructors_needed": needed,
                "cases": [{"case_id": self.case_code(c), "extra_shift_id": e} for c, e in opened]}

    # --- offers ---------------------------------------------------------------

    def _offer_next(self, case_id: int, now: datetime) -> dict | None:
        """Send the next offer, or go to plan B when nobody eligible is left."""
        with self.conn.transaction():
            case = self.conn.execute(
                "SELECT shift_id, status FROM cover_case WHERE id = %s FOR UPDATE", (case_id,)
            ).fetchone()
            if case is None:
                raise NotFound(f"unknown case {case_id}")
            shift_id, status = case
            if status != "open":
                return None
            active = self.conn.execute(
                "SELECT id FROM offer WHERE case_id = %s AND status IN ('pending', 'held') ORDER BY queue_pos", (case_id,)
            ).fetchone()
            if active:
                return {"offer_id": active[0]}
            ranked, names, policy, _ = self.ranked(shift_id, now)
            if policy.autonomy["assign_cover"] == "off":
                self._event(case_id, now, "agent", "offers_off", {"why": "policy: assign_cover is off"})
                return None
            offered = {r[0] for r in self.conn.execute("SELECT staff_id FROM offer WHERE case_id = %s", (case_id,))}
            queue = [r for r in ranked if r.candidate.decision.ok and r.candidate.staff_id not in offered]
            if queue:
                # a wave: the next people in queue order, all at once (product decision)
                wave = self.conn.execute("SELECT coalesce(max(wave), 0) + 1 FROM offer WHERE case_id = %s",
                                         (case_id,)).fetchone()[0]
                members = queue[: policy.preset.offer_wave_size]
                sent = [self._send_offer(case_id, shift_id, r, names, policy, now, len(offered) + i + 1, wave=wave,
                                         wave_place=i + 1) for i, r in enumerate(members)]
                if len(members) > 1:
                    self._event(case_id, now, "agent", "wave_sent", {
                        "wave": wave, "staff_ids": [x["staff_id"] for x in sent],
                        "collect_seconds": policy.preset.offer_collect_seconds,
                    })
                return {**sent[0], "offers": sent, "wave": wave}
            self._event(case_id, now, "agent", "queue_exhausted", {"offered": len(offered)})
            # an approved option that left the case open did not work out (the overtime person said no)
            failed = self.conn.execute(
                "UPDATE plan_b SET status = 'failed' WHERE case_id = %s AND status = 'approved' RETURNING id", (case_id,)
            ).fetchall()
            for (plan_id,) in failed:
                self._event(case_id, now, "agent", "plan_b_failed", {"plan_id": f"P-{plan_id}"})
        return self.propose_plan_b(case_id, now)

    def _send_offer(self, case_id: int, shift_id: str, r: Ranked, names: dict, policy: Policy, now: datetime,
                    pos: int, overtime: bool = False, wave: int = 1, wave_place: int = 1) -> dict:
        staff_id = r.candidate.staff_id
        info = self.shift_info(shift_id)
        expires = now + timedelta(minutes=policy.offer_timeout_minutes)
        here = dataclasses.replace(r, position=wave_place)  # the place among those still in the queue
        place = {**r.place(), "line": describe_place(here, names[staff_id].split()[0], "en", policy.preset),
                 "overtime": overtime, "wave": wave, "wave_place": wave_place}
        offer_id = self.conn.execute(
            "INSERT INTO offer (case_id, shift_id, staff_id, queue_pos, place, sent_at, expires_at, overtime, wave)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (case_id, shift_id, staff_id, pos, Jsonb(plain(place)), now, expires, overtime, wave),
        ).fetchone()[0]
        lang = M.lang_or_en(self._staff_row(staff_id)["language"])
        body = M.OFFER[lang].format(when=self._when(info), venue=info["venue"], what=self._what(info),
                                    minutes=policy.offer_timeout_minutes)
        if overtime:
            body += M.OFFER_OVERTIME[lang]
        self._message(now, "staff", lang, body, staff_id=staff_id, case_id=case_id, offer_id=offer_id)
        self._event(case_id, now, "agent", "offer_sent", {
            "offer_id": offer_id, "staff_id": staff_id, "name": names[staff_id], "queue_pos": pos,
            "line": place["line"], "expires_at": expires, "overtime": overtime, "wave": wave,
        })
        return {"offer_id": offer_id, "staff_id": staff_id}

    def expire_due(self, now: datetime) -> int:
        """Expire offers whose timer ran out and move each case on. Safe to run from several workers."""
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT id, case_id, staff_id FROM offer WHERE status = 'pending' AND expires_at <= %s"
                " ORDER BY expires_at, id FOR UPDATE SKIP LOCKED",
                (now,),
            ).fetchall()
            for offer_id, case_id, staff_id in rows:
                self.conn.execute("UPDATE offer SET status = 'expired' WHERE id = %s", (offer_id,))
                self._event(case_id, now, "agent", "offer_expired", {"offer_id": offer_id, "staff_id": staff_id})
        collect = timedelta(seconds=self._policy().preset.offer_collect_seconds)
        windows = [r[0] for r in self.conn.execute(
            "SELECT case_id FROM offer o WHERE status = 'held' AND NOT EXISTS (SELECT 1 FROM client_ask a"
            " WHERE a.offer_id = o.id AND a.status = 'asked') GROUP BY case_id HAVING min(responded_at) + %s <= %s",
            (collect, now),
        )]
        for case_id in dict.fromkeys([*(case_id for _, case_id, _ in rows), *windows]):
            self._settle_or_next(case_id, now)
        self._client_due(now)
        self._plan_b_by_deadline(now)
        return len(rows)

    def _plan_b_by_deadline(self, now: datetime) -> list[int]:
        """Plan B goes to the manager `plan_b_minutes_before_start` before the shift, even while offers are out."""
        minutes = self._policy().preset.plan_b_minutes_before_start
        if minutes is None:
            return []
        due = [r[0] for r in self.conn.execute(
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
            " WHERE v.policy_id = %s AND c.status = 'open' AND s.status = 'open' AND s.starts_at - %s <= %s"
            " AND NOT EXISTS (SELECT 1 FROM plan_b p WHERE p.case_id = c.id) ORDER BY s.starts_at, c.id",
            (self.policy_id, timedelta(minutes=minutes), now),
        )]
        for case_id in due:
            with self.conn.transaction():
                self._event(case_id, now, "agent", "plan_b_deadline", {"minutes_before_start": minutes})
            self.propose_plan_b(case_id, now)
        return due

    def _settle_or_next(self, case_id: int, now: datetime) -> dict | None:
        """After an answer or a timer: give the shift to the highest "yes" once nobody higher can still answer or
        the collection window is over; tell the others; with nothing left open, send the next wave."""
        policy = self._policy()
        with self.conn.transaction():
            status = self.conn.execute("SELECT status FROM cover_case WHERE id = %s FOR UPDATE", (case_id,)).fetchone()[0]
            if status != "open":
                return None
            if self.conn.execute("SELECT 1 FROM client_ask WHERE case_id = %s AND status = 'asked'", (case_id,)).fetchone():
                return {"asking_family": True}  # the family has the question; their answer moves the case on
            rows = self.conn.execute(
                "SELECT id, staff_id, status, queue_pos, responded_at, shift_id FROM offer"
                " WHERE case_id = %s AND status IN ('pending', 'held') AND NOT overtime ORDER BY queue_pos",
                (case_id,),
            ).fetchall()
            held = [r for r in rows if r[2] == "held"]
            if held:
                window_end = min(r[4] for r in held) + timedelta(seconds=policy.preset.offer_collect_seconds)
                higher_open = any(r[2] == "pending" and r[3] < held[0][3] for r in rows)
                if higher_open and now < window_end:
                    return {"collecting_until": window_end}
                for offer_id, staff_id, _, _, _, shift_id in held:
                    person = next(s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id)
                    shift = load_shift(self.conn, shift_id)
                    decision = eligible(person, shift, now, policy, load_context(self.conn, self.policy_id, shift))
                    blocking = [r for r in decision.reasons if r.blocking]
                    if blocking:
                        self._refuse_yes(case_id, offer_id, staff_id, shift_id, blocking, policy, now)
                        continue
                    if is_private(shift):
                        return self._ask_family(case_id, offer_id, staff_id, shift_id, policy, now)
                    return self._take(case_id, offer_id, staff_id, shift_id, policy, now)
            elif rows:
                return None
        return self._offer_next(case_id, now)

    def _refuse_yes(self, case_id: int, offer_id: int, staff_id: str, shift_id: str, blocking: list, policy: Policy,
                    now: datetime) -> str:
        self.conn.execute("UPDATE offer SET status = 'withdrawn', responded_at = coalesce(responded_at, %s) WHERE id = %s",
                          (now, offer_id))
        why = "; ".join(describe(r, "en", policy.preset) for r in blocking)
        self._event(case_id, now, "agent", "accept_rejected",
                    {"offer_id": offer_id, "staff_id": staff_id, "codes": [r.code.value for r in blocking], "why": why})
        lang = M.lang_or_en(self._staff_row(staff_id)["language"])
        self._message(now, "staff", lang, M.NO_LONGER[lang].format(when=self._when(self.shift_info(shift_id)), why=why),
                      staff_id=staff_id, case_id=case_id, offer_id=offer_id)
        return why

    def _take(self, case_id: int, offer_id: int, staff_id: str, shift_id: str, policy: Policy, now: datetime) -> dict:
        """This "yes" gets the shift (or, in propose mode, the manager's card); everyone else still open stands down."""
        self.conn.execute("UPDATE offer SET status = 'accepted', responded_at = coalesce(responded_at, %s) WHERE id = %s",
                          (now, offer_id))
        if policy.autonomy["assign_cover"] == "propose":
            approval = self.conn.execute(
                "INSERT INTO approval (case_id, action, payload, created_at) VALUES (%s, 'assign_cover', %s, %s) RETURNING id",
                (case_id, Jsonb({"offer_id": offer_id, "staff_id": staff_id, "shift_id": shift_id}), now),
            ).fetchone()[0]
            self._event(case_id, now, "agent", "awaiting_approval",
                        {"approval_id": approval, "action": "assign_cover", "staff_id": staff_id})
        else:
            self._book(case_id, shift_id, staff_id, now, via={"offer_id": offer_id, "overtime": False})
        self._stand_down(case_id, staff_id, shift_id, now)
        return {"offer_id": offer_id, "staff_id": staff_id, "taken": True}

    def _stand_down(self, case_id: int, winner: str, shift_id: str, now: datetime) -> None:
        name = self._staff_row(winner)["name"].split()[0]
        when = self._when(self.shift_info(shift_id))
        for offer_id, staff_id in self.conn.execute(
            "UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status IN ('pending', 'held') AND NOT overtime"
            " RETURNING id, staff_id", (case_id,),
        ).fetchall():
            self._event(case_id, now, "agent", "offer_stood_down", {"offer_id": offer_id, "staff_id": staff_id,
                                                                    "covered_by": winner})
            lang = M.lang_or_en(self._staff_row(staff_id)["language"])
            self._message(now, "staff", lang, M.STOOD_DOWN[lang].format(name=name, when=when),
                          staff_id=staff_id, case_id=case_id, offer_id=offer_id)

    def respond(self, offer_id: int, accept: bool, now: datetime) -> dict:
        """A staff answer. The person is checked again at the moment of "yes"."""
        self.expire_due(now)
        move_on = False
        refused = None
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT o.case_id, o.shift_id, o.staff_id, o.status, o.overtime FROM offer o WHERE o.id = %s FOR UPDATE",
                (offer_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown offer {offer_id}")
            case_id, shift_id, staff_id, status, overtime = row
            case_status = self.conn.execute("SELECT status FROM cover_case WHERE id = %s FOR UPDATE", (case_id,)).fetchone()[0]
            if status != "pending":
                holder = self.conn.execute("SELECT s.full_name FROM shift sh JOIN staff s ON s.id = sh.staff_id"
                                           " WHERE sh.id = %s", (shift_id,)).fetchone()
                if case_status in ("covered", "plan_b") and holder and accept:
                    raise Conflict(f"Thanks — {holder[0].split()[0]} already covered it",
                                   {"status": status, "covered_by": holder[0]})
                raise Conflict(f"offer {offer_id} is {status}", {"status": status})
            info = self.shift_info(shift_id)
            if not accept:
                self.conn.execute("UPDATE offer SET status = 'declined', responded_at = %s WHERE id = %s", (now, offer_id))
                self._event(case_id, now, "staff", "offer_declined", {"offer_id": offer_id, "staff_id": staff_id})
                move_on = True
            else:
                policy = self._policy()
                person = next(s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id)
                shift = load_shift(self.conn, shift_id)
                decision = eligible(person, shift, now, policy, load_context(self.conn, self.policy_id, shift))
                blocking = [r for r in decision.reasons if r.blocking and not (overtime and r.code == Code.HOURS_LIMIT_EXCEEDED)]
                if blocking:
                    refused = self._refuse_yes(case_id, offer_id, staff_id, shift_id, blocking, policy, now)
                    move_on = True
                elif not overtime and (policy.preset.offer_collect_seconds > 0 or info["private"]):
                    # a wave: hold the "yes"; the highest "yes" gets the shift (see _settle_or_next)
                    self.conn.execute("UPDATE offer SET status = 'held', responded_at = %s WHERE id = %s", (now, offer_id))
                    self._event(case_id, now, "staff", "offer_yes_held", {
                        "offer_id": offer_id, "staff_id": staff_id,
                        "collect_seconds": policy.preset.offer_collect_seconds,
                    })
                    lang = M.lang_or_en(self._staff_row(staff_id)["language"])
                    held = M.HELD_PRIVATE if info["private"] else M.HELD
                    self._message(now, "staff", lang, held[lang].format(when=self._when(info)),
                                  staff_id=staff_id, case_id=case_id, offer_id=offer_id)
                    move_on = True
                elif policy.autonomy["assign_cover"] == "propose" and not overtime:
                    self.conn.execute("UPDATE offer SET status = 'accepted', responded_at = %s WHERE id = %s", (now, offer_id))
                    approval = self.conn.execute(
                        "INSERT INTO approval (case_id, action, payload, created_at) VALUES (%s, 'assign_cover', %s, %s) RETURNING id",
                        (case_id, Jsonb({"offer_id": offer_id, "staff_id": staff_id, "shift_id": shift_id}), now),
                    ).fetchone()[0]
                    self._event(case_id, now, "agent", "awaiting_approval",
                                {"approval_id": approval, "action": "assign_cover", "staff_id": staff_id})
                else:
                    self.conn.execute("UPDATE offer SET status = 'accepted', responded_at = %s WHERE id = %s", (now, offer_id))
                    self._book(case_id, shift_id, staff_id, now, via={"offer_id": offer_id, "overtime": overtime})
                    if not overtime:
                        self._stand_down(case_id, staff_id, shift_id, now)
        if move_on:
            self._settle_or_next(case_id, now)
        state = self.offer_state(offer_id)
        if refused:
            state["reason"] = refused
        return state

    def offer_state(self, offer_id: int) -> dict:
        row = self.conn.execute(
            "SELECT o.id, o.case_id, o.staff_id, o.status, c.status FROM offer o JOIN cover_case c ON c.id = o.case_id WHERE o.id = %s",
            (offer_id,),
        ).fetchone()
        return dict(zip(("offer_id", "case_id", "staff_id", "status", "case_status"), row))

    def _book(self, case_id: int, shift_id: str, staff_id: str, now: datetime, via: dict) -> None:
        """Put the person on the shift and close the case as covered."""
        info = self.shift_info(shift_id)
        self.conn.execute("UPDATE shift SET staff_id = %s, status = 'scheduled' WHERE id = %s", (staff_id, shift_id))
        opened_at = self.conn.execute("SELECT opened_at FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]
        self.conn.execute("UPDATE cover_case SET status = 'covered', closed_at = %s WHERE id = %s", (now, case_id))
        self.conn.execute("UPDATE plan_b SET status = 'superseded' WHERE case_id = %s AND status = 'proposed'", (case_id,))
        person = self._staff_row(staff_id)
        self._event(case_id, now, "agent", "shift_covered", {
            **via, "staff_id": staff_id, "name": person["name"],
            "minutes_to_cover": round((now - opened_at).total_seconds() / 60, 1),
        })
        lang = M.lang_or_en(person["language"])
        self._message(now, "staff", lang, M.CONFIRMED[lang].format(when=self._when(info), venue=info["venue"]),
                      staff_id=staff_id, case_id=case_id)

    def decide_approval(self, approval_id: int, approve: bool, now: datetime, by: str) -> dict:
        """A person decides a pending card (assign_cover in "propose" mode)."""
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT case_id, action, payload, status FROM approval WHERE id = %s FOR UPDATE", (approval_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown approval {approval_id}")
            case_id, action, payload, status = row
            if status != "pending":
                raise Conflict(f"approval {approval_id} is {status}")
            self.conn.execute(
                "UPDATE approval SET status = %s, decided_by = %s, decided_at = %s WHERE id = %s",
                ("approved" if approve else "rejected", by, now, approval_id),
            )
            if action == "night_cover":
                if approve:
                    self._open_shift(payload["shift_id"])
                    self._event(case_id, now, "manager", "night_cover_approved", {"approval_id": approval_id, "by": by})
                else:
                    self._close_case(case_id, now, "cancelled")
                    self._event(case_id, now, "manager", "approval_rejected", {"approval_id": approval_id, "by": by})
            elif approve:
                self._book(case_id, payload["shift_id"], payload["staff_id"], now,
                           via={"offer_id": payload["offer_id"], "approval_id": approval_id, "approved_by": by})
            else:
                self.conn.execute("UPDATE offer SET status = 'withdrawn' WHERE id = %s", (payload["offer_id"],))
                self._event(case_id, now, "manager", "approval_rejected", {"approval_id": approval_id, "by": by})
        if (action == "night_cover" and approve) or (action == "assign_cover" and not approve):
            self._offer_next(case_id, now)
        return {"approval_id": approval_id, "status": "approved" if approve else "rejected"}

    # --- a private lesson: the family says yes first -----------------------------------------------------------

    def _family_of(self, info: dict) -> dict:
        group = info["groups"][0]
        language = M.lang_or_en(group["languages"][0] if group["languages"] else None)
        return {"group_id": group["id"], "child": group["participant"] or group["name"], "language": language}

    def _family_text(self, kind: str, facts: dict, template: dict, now: datetime, case_id: int,
                     day: date) -> tuple[str, dict]:
        """The question for the family, written by the text model in the family's language from facts that say what
        to tell them; the model's text goes out only if it passes M.family_text_problems (names, time, day, the change,
        a yes/no question, no machine date), else the template. Returns (text, how: source and any rejected draft)."""
        draft, problems = None, []
        try:
            answer = self.models.chat_json(purpose="family_question", system=M.CLIENT_SYSTEM,
                                           user=json.dumps(plain(facts), ensure_ascii=False), schema=M.CLIENT_SCHEMA,
                                           key=plain(facts), at=now, case_id=case_id)
            draft = answer.data["body"].strip()
            problems = M.family_text_problems(kind, facts, draft, day) if draft else ["empty"]
            if not problems:
                return draft, {"text_source": "model"}
        except (ModelError, AttributeError):
            pass
        how = {"text_source": "template"}
        if draft is not None:  # the model answered, but its text would have said something else: kept for the record
            how.update({"model_text": draft, "model_text_rejected": problems})
        return template[facts["language"]].format(**facts), how

    def _answer_by(self, info: dict, now: datetime, policy: Policy) -> datetime:
        return min(now + timedelta(minutes=policy.preset.client_answer_minutes),
                   info["start"] - timedelta(minutes=policy.preset.client_answer_before_start_minutes))

    def _ask_family(self, case_id: int, offer_id: int, staff_id: str, shift_id: str, policy: Policy,
                    now: datetime) -> dict:
        """A coach said yes to a private lesson and passed the rules again: hold them (no other offer for that time),
        let the others in the wave go, and ask the family whether this coach may teach. Nothing is booked yet."""
        info = self.shift_info(shift_id)
        family = self._family_of(info)
        coach = self._staff_row(staff_id)["name"]
        absent = self.conn.execute("SELECT s.full_name FROM cover_case c JOIN staff s ON s.id = c.absent_staff_id"
                                   " WHERE c.id = %s", (case_id,)).fetchone()
        answer_by = self._answer_by(info, now, policy)
        if answer_by <= now:
            # too close to the start to ask: every coach still open is let go and the manager decides (move or credit)
            self._event(case_id, now, "agent", "family_too_late", {"staff_id": staff_id, "name": coach,
                                                                    "answer_by": answer_by})
            for other_id, other in self.conn.execute(
                "SELECT id, staff_id FROM offer WHERE case_id = %s AND status IN ('pending', 'held') AND NOT overtime"
                " ORDER BY queue_pos", (case_id,)).fetchall():
                self._release(case_id, other_id, other, info, now, M.RELEASED_LATE)
            self.propose_plan_b(case_id, now)
            return {"offer_id": offer_id, "staff_id": staff_id, "asked": None}
        for other_id, other in self.conn.execute(
            "UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status IN ('pending', 'held') AND id <> %s"
            " AND NOT overtime RETURNING id, staff_id", (case_id, offer_id),
        ).fetchall():
            self._event(case_id, now, "agent", "offer_stood_down", {"offer_id": other_id, "staff_id": other,
                                                                    "for_family": staff_id})
            lang = M.lang_or_en(self._staff_row(other)["language"])
            self._message(now, "staff", lang, M.PRIVATE_STOOD_DOWN[lang].format(name=coach.split()[0], when=self._when(info)),
                          staff_id=other, case_id=case_id, offer_id=other_id)
        day = info["local_start"].date()
        facts = M.family_facts("cover", language=family["language"], child=family["child"], coach=coach.split()[0],
                               absent=absent[0].split()[0] if absent else "Your coach", day=day,
                               today=now.astimezone(ZoneInfo(info["tz"])).date(), time=f"{info['local_start']:%H:%M}",
                               venue=info["venue"])
        text, how = self._family_text("cover", facts, M.CLIENT_COVER, now, case_id, day)
        message_id = self._message(now, "family", family["language"], text, group_id=family["group_id"], case_id=case_id)
        ask_id = self.conn.execute(
            "INSERT INTO client_ask (case_id, group_id, kind, staff_id, offer_id, language, message_id, asked_at, answer_by)"
            " VALUES (%s, %s, 'cover', %s, %s, %s, %s, %s, %s) RETURNING id",
            (case_id, family["group_id"], staff_id, offer_id, family["language"], message_id, now, answer_by),
        ).fetchone()[0]
        self._event(case_id, now, "agent", "family_asked", {
            "ask_id": f"Q-{ask_id}", "kind": "cover", "staff_id": staff_id, "name": coach, "offer_id": offer_id,
            "child": family["child"], "text": text, **how, "language": family["language"],
            "answer_by": answer_by,
        })
        return {"offer_id": offer_id, "staff_id": staff_id, "asked": f"Q-{ask_id}"}

    def _release(self, case_id: int, offer_id: int, staff_id: str, info: dict, now: datetime, text: dict) -> None:
        """Let a held coach go, with thanks."""
        self.conn.execute("UPDATE offer SET status = 'withdrawn' WHERE id = %s AND status IN ('pending', 'held')",
                          (offer_id,))
        self._event(case_id, now, "agent", "coach_released", {"offer_id": offer_id, "staff_id": staff_id,
                                                              "name": self._staff_row(staff_id)["name"]})
        lang = M.lang_or_en(self._staff_row(staff_id)["language"])
        self._message(now, "staff", lang, text[lang].format(when=self._when(info)), staff_id=staff_id, case_id=case_id,
                      offer_id=offer_id)

    def find_ask(self, ref: str | int) -> int:
        text = str(ref).removeprefix("Q-")
        row = self.conn.execute(
            "SELECT a.id FROM client_ask a JOIN cover_case c ON c.id = a.case_id JOIN shift s ON s.id = c.shift_id"
            " JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s AND a.id::text = %s", (self.policy_id, text),
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown question {ref}")
        return row[0]

    def family_respond(self, ask_id: int, accept: bool, now: datetime) -> dict:
        """The family's answer. "Yes" to another coach: the rules run again for that coach, then the lesson is booked
        (if the coach no longer passes, the next coach is found and the family is asked again). "No": the coach is
        let go and the family's own coach is offered at the nearest free time, else a credit. "Yes" to the new time:
        the lesson moves; "no": a credit. Nothing is booked, moved or credited without an answer."""
        self.expire_due(now)
        policy = self._policy()
        follow = None
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT case_id, kind, staff_id, offer_id, proposed_start, proposed_end, status FROM client_ask"
                " WHERE id = %s FOR UPDATE", (ask_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown question Q-{ask_id}")
            case_id, kind, staff_id, offer_id, start, end, status = row
            self.conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (case_id,))
            if status != "asked":
                raise Conflict(f"Thanks — this question was already closed ({status})", {"status": status})
            self.conn.execute("UPDATE client_ask SET status = %s, answered_at = %s WHERE id = %s",
                              ("yes" if accept else "no", now, ask_id))
            shift_id = self.conn.execute("SELECT shift_id FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]
            info = self.shift_info(shift_id)
            family = self._family_of(info)
            name = self._staff_row(staff_id)["name"]
            self._event(case_id, now, "family", "family_yes" if accept else "family_no",
                        {"ask_id": f"Q-{ask_id}", "kind": kind, "staff_id": staff_id, "name": name,
                         **({"start": start} if start else {})})
            if kind == "cover" and accept:
                person = next(s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id)
                shift = load_shift(self.conn, shift_id)
                decision = eligible(person, shift, now, policy, load_context(self.conn, self.policy_id, shift))
                blocking = [r for r in decision.reasons if r.blocking]
                if blocking:
                    why = self._refuse_yes(case_id, offer_id, staff_id, shift_id, blocking, policy, now)
                    self._event(case_id, now, "agent", "family_yes_recheck_failed", {"staff_id": staff_id, "why": why})
                    self._message(now, "family", family["language"], M.CLIENT_RECHECK_FAILED[family["language"]].format(
                        coach=name.split()[0]), group_id=family["group_id"], case_id=case_id)
                    follow = "next_coach"
                else:
                    self._take(case_id, offer_id, staff_id, shift_id, policy, now)
                    self._message(now, "family", family["language"], M.CLIENT_COVER_CONFIRMED[family["language"]].format(
                        coach=name.split()[0], child=family["child"], time=f"{info['local_start']:%H:%M}"),
                        group_id=family["group_id"], case_id=case_id)
            elif kind == "cover":
                self._release(case_id, offer_id, staff_id, info, now, M.RELEASED)
                follow = "own_coach"
            elif accept:
                coach = next(s for s in load_staff(self.conn, self.policy_id) if s.id == staff_id)
                shift = load_shift(self.conn, shift_id)
                moved = dataclasses.replace(shift, id=f"{shift_id}-M", start=start, end=end, assignee=None)
                decision = eligible(coach, moved, now, policy, load_context(self.conn, self.policy_id, moved))
                if decision.ok:
                    self._move_private(case_id, staff_id, start, end, now, by="family")
                else:
                    self._credit_private(case_id, now, why="time_taken")
            else:
                self._credit_private(case_id, now, why="family_no")
        if follow == "next_coach":
            self._offer_next(case_id, now)
        elif follow == "own_coach":
            self._offer_own_coach(case_id, now)
        return self.ask_state(ask_id)

    def ask_state(self, ask_id: int) -> dict:
        row = self.conn.execute(
            "SELECT a.id, a.case_id, a.kind, a.status, c.status FROM client_ask a JOIN cover_case c ON c.id = a.case_id"
            " WHERE a.id = %s", (ask_id,),
        ).fetchone()
        return {"ask_id": f"Q-{row[0]}", "case_id": row[1], "kind": row[2], "status": row[3], "case_status": row[4]}

    def _own_slot(self, shift_id: str, coach_id: str | None, now: datetime) -> tuple:
        """(coach, the moved lesson or None, the days tried): the family's own coach at the nearest free time."""
        if coach_id is None:
            return None, None, []
        coach = next((s for s in load_staff(self.conn, self.policy_id) if s.id == coach_id), None)
        if coach is None:
            return None, None, []
        shift = load_shift(self.conn, shift_id)
        tz = ZoneInfo(self.shift_info(shift_id)["tz"])
        slot, tried = own_coach_slot(shift=shift, coach=coach, policy=self._policy(),
                                     context_for=lambda moved: load_context(self.conn, self.policy_id, moved), tz=tz,
                                     now=now)
        return coach, slot, tried

    def _offer_own_coach(self, case_id: int, now: datetime) -> dict:
        """After "no" to another coach: ask the family about their own coach at the nearest free time; with none, a
        credit; too late to ask again: plan B for the manager (move or credit)."""
        policy = self._policy()
        with self.conn.transaction():
            shift_id, absent, status = self.conn.execute(
                "SELECT shift_id, absent_staff_id, status FROM cover_case WHERE id = %s FOR UPDATE", (case_id,)).fetchone()
            if status != "open":
                return {"status": status}
            info = self.shift_info(shift_id)
            coach, slot, tried = self._own_slot(shift_id, absent, now)
            self._event(case_id, now, "agent", "own_coach_checked", {
                "staff_id": absent, "name": coach.name if coach else None,
                "found": slot.start if slot else None,
                "tried": [{"start": t["start"], "codes": t["codes"]} for t in tried],
            })
            if slot is None:
                return self._credit_private(case_id, now, why="no_time")
            answer_by = self._answer_by(info, now, policy)
            if answer_by <= now:
                self._event(case_id, now, "agent", "family_too_late", {"staff_id": absent, "answer_by": answer_by})
                too_late = True
            else:
                too_late = False
        if too_late:  # no time left to ask the family about a new time: the manager decides (move or credit)
            return self.propose_plan_b(case_id, now)
        with self.conn.transaction():
            family = self._family_of(info)
            local = slot.start.astimezone(ZoneInfo(info["tz"]))
            when = f"{local:%a, %b} {local.day} at {local:%H:%M}"
            facts = M.family_facts("reschedule", language=family["language"], child=family["child"],
                                   coach=coach.name.split()[0], day=local.date(),
                                   today=now.astimezone(ZoneInfo(info["tz"])).date(), time=f"{local:%H:%M}",
                                   venue=info["venue"])
            text, how = self._family_text("reschedule", facts, M.CLIENT_RESCHEDULE, now, case_id, local.date())
            message_id = self._message(now, "family", family["language"], text, group_id=family["group_id"],
                                       case_id=case_id)
            ask_id = self.conn.execute(
                "INSERT INTO client_ask (case_id, group_id, kind, staff_id, proposed_start, proposed_end, language,"
                " message_id, asked_at, answer_by) VALUES (%s, %s, 'reschedule', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (case_id, family["group_id"], coach.id, slot.start, slot.end, family["language"], message_id, now,
                 answer_by),
            ).fetchone()[0]
            self._event(case_id, now, "agent", "family_asked", {
                "ask_id": f"Q-{ask_id}", "kind": "reschedule", "staff_id": coach.id, "name": coach.name,
                "child": family["child"], "start": slot.start, "when": f"{local:%a %H:%M}", "text": text,
                **how, "language": family["language"], "answer_by": answer_by,
            })
        return {"asked": f"Q-{ask_id}"}

    def _move_private(self, case_id: int, staff_id: str, start: datetime, end: datetime, now: datetime,
                      by: str) -> dict:
        """The lesson moves to the family's own coach at the agreed time: a new booking; the old one is cancelled."""
        shift_id = self.conn.execute("SELECT shift_id FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]
        info = self.shift_info(shift_id)
        tz = ZoneInfo(info["tz"])
        family = self._family_of(info)
        local = start.astimezone(tz)
        new_id = f"L-{local:%m%d}-{family['group_id']}"
        if self.conn.execute("SELECT 1 FROM shift WHERE id = %s", (new_id,)).fetchone():
            new_id = f"{new_id}-C{case_id}"
        self.conn.execute(
            "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id, instructors, extra_certs,"
            " status) SELECT %s, venue_id, zone_id, role_id, %s, %s, %s, instructors, extra_certs, 'scheduled'"
            " FROM shift WHERE id = %s", (new_id, start, end, staff_id, shift_id),
        )
        self.conn.execute("INSERT INTO shift_group (shift_id, group_id) SELECT %s, group_id FROM shift_group"
                          " WHERE shift_id = %s", (new_id, shift_id))
        self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
        self._close_case(case_id, now, "plan_b")
        coach = self._staff_row(staff_id)
        label = f"{local:%a %H:%M}"
        when = f"{local:%a, %b} {local.day} at {local:%H:%M}"
        self._event(case_id, now, "agent", "private_moved", {
            "to_shift_id": new_id, "staff_id": staff_id, "name": coach["name"], "start": start, "when": label, "by": by,
            "line": f"Moved to {coach['name'].split()[0]}, {label}",
        })
        self._message(now, "family", family["language"], M.CLIENT_MOVED[family["language"]].format(
            child=family["child"], coach=coach["name"].split()[0], when=when), group_id=family["group_id"], case_id=case_id)
        lang = M.lang_or_en(coach["language"])
        self._message(now, "staff", lang, M.PRIVATE_MOVED_COACH[lang].format(child=family["child"], when=when,
                                                                            venue=info["venue"]),
                      staff_id=staff_id, case_id=case_id)
        return {"moved_to": new_id, "staff_id": staff_id, "start": start}

    def _credit_private(self, case_id: int, now: datetime, why: str) -> dict:
        """The lesson does not run: the family gets a credit for one lesson, at the private lesson's price."""
        from coachcover.money import money_params

        shift_id = self.conn.execute("SELECT shift_id FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]
        info = self.shift_info(shift_id)
        family = self._family_of(info)
        policy = self._policy()
        amount = money_params(policy.preset.money, policy.overtime_cost_per_hour).private_price.value
        self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
        self.conn.execute(
            "INSERT INTO remedy (shift_id, group_id, kind, families, valid_until, case_id, created_at, amount_usd)"
            " VALUES (%s, %s, 'credit', 1, NULL, %s, %s, %s)", (shift_id, family["group_id"], case_id, now, amount),
        )
        self._close_case(case_id, now, "plan_b")
        self._event(case_id, now, "agent", "private_credit", {"group_id": family["group_id"], "amount_usd": amount,
                                                              "why": why, "line": "Credit for one lesson"})
        when = f"{info['local_start']:%a %H:%M}"
        self._message(now, "family", family["language"], M.CLIENT_CREDIT[family["language"]].format(
            child=family["child"], when=when, amount=f"${amount:g}"), group_id=family["group_id"], case_id=case_id)
        return {"credit_usd": amount}

    def _client_due(self, now: datetime) -> list[int]:
        """Questions the family did not answer in time: the held coach is let go and the manager gets plan B for the
        lesson (the family's own coach at the nearest free time, or a credit). Nothing moves without a person."""
        due = self.conn.execute(
            "SELECT a.id, a.case_id, a.kind, a.staff_id, a.offer_id FROM client_ask a JOIN cover_case c ON c.id = a.case_id"
            " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE v.policy_id = %s"
            " AND a.status = 'asked' AND a.answer_by <= %s ORDER BY a.answer_by, a.id",
            (self.policy_id, now),
        ).fetchall()
        for ask_id, case_id, kind, staff_id, offer_id in due:
            with self.conn.transaction():
                if self.conn.execute("UPDATE client_ask SET status = 'silent' WHERE id = %s AND status = 'asked'"
                                     " RETURNING id", (ask_id,)).fetchone() is None:
                    continue
                shift_id = self.conn.execute("SELECT shift_id FROM cover_case WHERE id = %s", (case_id,)).fetchone()[0]
                info = self.shift_info(shift_id)
                self._event(case_id, now, "agent", "family_silent", {"ask_id": f"Q-{ask_id}", "kind": kind,
                                                                     "staff_id": staff_id})
                if kind == "cover":
                    self._release(case_id, offer_id, staff_id, info, now, M.RELEASED_SILENT)
            self.propose_plan_b(case_id, now)
        return [r[1] for r in due]

    def _private_facts(self, case_id: int, shift_id: str, absent: str | None, now: datetime) -> dict:
        """Plan B for a private lesson: never another coach without the family's yes (planb.private_plan_options)."""
        from coachcover.money import money_params

        policy = self._policy()
        info = self.shift_info(shift_id)
        coach, slot, tried = self._own_slot(shift_id, absent, now)
        price = money_params(policy.preset.money, policy.overtime_cost_per_hour).private_price.value
        options = private_plan_options(shift=load_shift(self.conn, shift_id), coach=coach, slot=slot, policy=policy,
                                       tz=ZoneInfo(info["tz"]), price_usd=price)
        return plain({
            "case_id": case_id,
            "shift": {"id": shift_id, "label": info["label"], "venue": info["venue"], "zone_id": info["zone_id"],
                      "start": info["start"], "end": info["end"], "role": info["role"]},
            "options": options,
            "recommended": 0,
            "rejected": [{"kind": "reschedule_own", "start": t["start"], "codes": t["codes"]} for t in tried if not t["ok"]],
            "private": True,
        })


    # --- plan B ---------------------------------------------------------------

    def _plan_facts(self, case_id: int, now: datetime) -> dict:
        shift_id, absent, source = self.conn.execute(
            "SELECT shift_id, absent_staff_id, source FROM cover_case WHERE id = %s", (case_id,)
        ).fetchone()
        if source == "ratio":
            return self._ratio_facts(case_id, shift_id, now)
        if self.shift_info(shift_id)["private"]:
            return self._private_facts(case_id, shift_id, absent, now)
        policy = self._policy()
        staff = {s.id: s for s in load_staff(self.conn, self.policy_id)}
        shift = load_shift(self.conn, shift_id)
        context = load_context(self.conn, self.policy_id, shift)
        info = self.shift_info(shift_id)
        partner_ids = [r[0] for r in self.conn.execute(
            "SELECT s.id FROM shift s WHERE s.venue_id = %s AND s.role_id = (SELECT role_id FROM shift WHERE id = %s)"
            " AND s.starts_at = %s AND s.ends_at = %s AND s.status = 'scheduled' AND s.id <> %s"
            " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id)"
            # a private lesson is never a group next door: its family said yes to one coach, not to a group
            " AND NOT EXISTS (SELECT 1 FROM shift_group pg JOIN lesson_group pl ON pl.id = pg.group_id WHERE pg.shift_id = s.id AND pl.kind = 'private')"
            # a shift teaching the same groups (an instructor seat of a broken norm) is not a group next door
            " AND NOT EXISTS (SELECT 1 FROM shift_group a JOIN shift_group b ON b.group_id = a.group_id"
            "  WHERE a.shift_id = s.id AND b.shift_id = %s) ORDER BY s.id",
            (shift.venue, shift_id, shift.start, shift.end, shift_id, shift_id),
        )]
        asked = {r[0] for r in self.conn.execute("SELECT staff_id FROM offer WHERE case_id = %s", (case_id,))}
        options, rejected = plan_b_options(
            shift=shift, absent=absent, partners=[load_shift(self.conn, pid) for pid in partner_ids], staff=staff,
            policy=policy, context=context, now=now, exclude=asked,
        )
        return plain({
            "case_id": case_id,
            "shift": {"id": shift_id, "label": info["label"], "venue": info["venue"], "zone_id": info["zone_id"],
                      "start": info["start"], "end": info["end"], "role": info["role"]},
            "options": options,
            "recommended": 0,
            "rejected": rejected,
        })

    def _lesson_seats(self, lesson_id: str) -> tuple[int, int, list[str]]:
        """(instructors the lesson really has, its open seats' shift ids, its instructor count field): an extra seat
        counts once it is staffed; base = the field minus every seat that is not cancelled."""
        instructors = self.conn.execute("SELECT instructors FROM shift WHERE id = %s", (lesson_id,)).fetchone()[0]
        seats = self.conn.execute("SELECT id, status FROM shift WHERE extra_for = %s AND status <> 'cancelled' ORDER BY id",
                                  (lesson_id,)).fetchall()
        base = instructors - len(seats)
        staffed = sum(1 for _, st in seats if st == "scheduled")
        return base + staffed, base, [sid for sid, st in seats if st == "open"]

    def _ratio_facts(self, case_id: int, seat_id: str, now: datetime) -> dict:
        """Plan B for a broken norm: only options that keep every lesson within its norm (planb.ratio_plan_options)."""
        policy = self._policy()
        staff = {s.id: s for s in load_staff(self.conn, self.policy_id)}
        seat = load_shift(self.conn, seat_id)
        lesson_id = self.conn.execute("SELECT extra_for FROM shift WHERE id = %s", (seat_id,)).fetchone()[0]
        lesson = load_shift(self.conn, lesson_id)
        group_id = self.conn.execute(
            "SELECT data->>'group_id' FROM case_event WHERE case_id = %s AND kind = 'ratio_broken' ORDER BY seq LIMIT 1",
            (case_id,),
        ).fetchone()[0]
        effective, _, _ = self._lesson_seats(lesson_id)
        per = min(group_norm(g, policy.preset) for g in lesson.groups)
        move = max(0, sum(g.size for g in lesson.groups) - per * effective)
        moved = self._newest_families(group_id, move)
        context = load_context(self.conn, self.policy_id, lesson)
        info = self.shift_info(seat_id)
        partner_ids = [r[0] for r in self.conn.execute(
            "SELECT s.id FROM shift s WHERE s.venue_id = %s AND s.starts_at = %s AND s.ends_at = %s AND s.status = 'scheduled'"
            " AND s.extra_for IS NULL AND s.id <> %s AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id)"
            " AND NOT EXISTS (SELECT 1 FROM shift_group pg JOIN lesson_group pl ON pl.id = pg.group_id WHERE pg.shift_id = s.id AND pl.kind = 'private')"
            " AND NOT EXISTS (SELECT 1 FROM shift_group a JOIN shift_group b ON b.group_id = a.group_id"
            "  WHERE a.shift_id = s.id AND b.shift_id = %s) ORDER BY s.id",
            (lesson.venue, lesson.start, lesson.end, lesson_id, lesson_id),
        )]
        later_ids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT s.id, s.starts_at FROM shift s JOIN shift_group sg ON sg.shift_id = s.id"
            " JOIN lesson_group g ON g.id = sg.group_id JOIN venue v ON v.id = s.venue_id"
            " WHERE v.policy_id = %s AND s.status = 'scheduled' AND s.extra_for IS NULL AND s.starts_at > %s"
            " AND s.starts_at < %s AND g.id <> %s AND g.kind = 'group'"
            " AND g.level_id = (SELECT level_id FROM lesson_group WHERE id = %s)"
            " ORDER BY s.starts_at, s.id",
            (self.policy_id, lesson.start, lesson.start + self.RATIO_HORIZON, group_id, group_id),
        )]
        asked = {r[0] for r in self.conn.execute("SELECT staff_id FROM offer WHERE case_id = %s", (case_id,))}
        options, rejected = ratio_plan_options(
            seat=seat, lesson=lesson, group_id=group_id, move=move, moved_families=moved,
            partners=[load_shift(self.conn, pid) for pid in partner_ids],
            later=[load_shift(self.conn, sid) for sid in later_ids],
            staff=staff, policy=policy, context=context, now=now, exclude=asked,
        )
        return plain({
            "case_id": case_id,
            "shift": {"id": seat_id, "label": info["label"], "venue": info["venue"], "zone_id": info["zone_id"],
                      "start": info["start"], "end": info["end"], "role": info["role"]},
            "options": options,
            "recommended": 0,
            "rejected": rejected,
            "ratio": {"lesson_id": lesson_id, "group_id": group_id, "move": move, "max_per_instructor": per,
                      "instructors": effective},
        })

    def _newest_families(self, group_id: str, move: int) -> list[tuple[str, int]]:
        """Languages of the `move` newest children of a group, from its enrolments (unknown: the group's first language)."""
        first = self.conn.execute("SELECT family_languages[1] FROM lesson_group WHERE id = %s", (group_id,)).fetchone()[0]
        counts: dict[str, int] = {}
        left = move
        for added, language in self.conn.execute(
            "SELECT added, language FROM group_enrollment WHERE group_id = %s ORDER BY at DESC, id DESC", (group_id,)
        ):
            if left <= 0:
                break
            take = min(left, added)
            counts[language or first] = counts.get(language or first, 0) + take
            left -= take
        if left > 0:
            counts[first] = counts.get(first, 0) + left
        return sorted(counts.items())

    def _plan_text(self, facts: dict, now: datetime, case_id: int) -> tuple[dict, str]:
        key = {"shift": facts["shift"], "options": facts["options"]}
        try:
            answer = self.models.chat_json(purpose="plan_b_text", system=M.PLAN_B_SYSTEM, user=json.dumps(key, ensure_ascii=False),
                                           schema=M.PLAN_B_SCHEMA, key=key, at=now, case_id=case_id)
            if len(answer.data["lines"]) == len(facts["options"]):
                return answer.data, "model"
        except ModelError:
            pass
        return M.plan_b_fallback(facts), "template"

    def propose_plan_b(self, case_id: int, now: datetime) -> dict | None:
        policy = self._policy()
        if policy.autonomy["plan_b"] == "off":
            with self.conn.transaction():
                self._event(case_id, now, "agent", "plan_b_off", {"why": "policy: plan_b is off"})
            return None
        facts = self._plan_facts(case_id, now)
        text, source = self._plan_text(facts, now, case_id)
        snapshot = plain({**facts, "text": text, "text_source": source, "computed_at": now})
        digest = canonical_key(snapshot)
        with self.conn.transaction():
            self.conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (case_id,))
            self.conn.execute("UPDATE plan_b SET status = 'superseded' WHERE case_id = %s AND status = 'proposed'", (case_id,))
            plan_id = self.conn.execute(
                "INSERT INTO plan_b (case_id, snapshot, snapshot_hash, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (case_id, Jsonb(snapshot), digest, now),
            ).fetchone()[0]
            self._event(case_id, now, "agent", "plan_b_proposed", {
                "plan_id": plan_id, "hash": digest, "options": [o["kind"] for o in facts["options"]],
                "recommended": facts["options"][0]["kind"], "headline": text["headline"],
            })
            self._message(now, "manager", "en", f"Plan B ready: {text['headline']}", case_id=case_id)
        plan = {"plan_id": plan_id, "hash": digest, "snapshot": snapshot}
        if facts["options"][facts["recommended"]]["mode"] == "auto":
            return self.approve_plan_b(plan_id, digest, now, by="agent")
        return plan

    def approve_plan_b(self, plan_id: int, digest: str, now: datetime, by: str, option: int | None = None) -> dict:
        """Approve against the hash that was shown. A changed or stale plan is a conflict, never a silent change."""
        row = self.conn.execute(
            "SELECT case_id, snapshot, snapshot_hash, status FROM plan_b WHERE id = %s", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"unknown plan {plan_id}")
        case_id, snapshot, stored, status = row
        if status != "proposed":
            live = self.conn.execute(
                "SELECT id, snapshot_hash FROM plan_b WHERE case_id = %s AND status = 'proposed'", (case_id,)
            ).fetchone()
            if status == "superseded" and live:
                raise Conflict("plan changed", {"plan_id": f"P-{live[0]}", "current_hash": live[1]})
            raise Conflict(f"plan {plan_id} is {status}", {"status": status})
        if digest != stored:
            raise Conflict("plan changed", {"plan_id": f"P-{plan_id}", "current_hash": stored})
        fresh = self._plan_facts(case_id, now)
        if any(fresh[k] != snapshot[k] for k in ("shift", "options", "recommended", "rejected")):
            new_plan = self.propose_plan_b(case_id, now)
            raise Conflict("plan changed", {"plan_id": f"P-{new_plan['plan_id']}" if new_plan else None,
                                            "current_hash": new_plan["hash"] if new_plan else None})
        index = snapshot["recommended"] if option is None else option
        if not 0 <= index < len(snapshot["options"]):
            raise Conflict(f"no option {index} in plan {plan_id}")
        chosen = snapshot["options"][index]
        if chosen["mode"] == "off":
            raise Conflict(f"option {chosen['kind']} is off in the policy")
        actor = "agent" if by == "agent" else "manager"
        with self.conn.transaction():
            locked = self.conn.execute("SELECT status, snapshot_hash FROM plan_b WHERE id = %s FOR UPDATE", (plan_id,)).fetchone()
            if locked != ("proposed", stored):
                raise Conflict(f"plan {plan_id} changed while approving")
            self.conn.execute(
                "UPDATE plan_b SET status = 'approved', chosen_option = %s, decided_by = %s, decided_at = %s WHERE id = %s",
                (index, by, now, plan_id),
            )
            self._event(case_id, now, actor, "plan_b_approved", {"plan_id": plan_id, "option": chosen["kind"], "by": by})
            outcome = self._apply_option(case_id, snapshot, chosen, now)
        return {"plan_id": plan_id, "option": chosen["kind"], **outcome}

    def _apply_option(self, case_id: int, snapshot: dict, chosen: dict, now: datetime) -> dict:
        shift_id = snapshot["shift"]["id"]
        info = self.shift_info(shift_id)
        kind = chosen["kind"]
        if kind in ("split", "move", "cancel_lesson"):
            return self._apply_ratio_option(case_id, snapshot, chosen, now)
        if kind == "reschedule_own":
            start, end = datetime.fromisoformat(chosen["starts_at"]), datetime.fromisoformat(chosen["ends_at"])
            return self._move_private(case_id, chosen["staff_id"], start, end, now, by="plan_b")
        if kind == "credit":
            return self._credit_private(case_id, now, why="plan_b")
        if kind == "merge":
            for gid in [g["id"] for g in info["groups"]]:
                self.conn.execute("INSERT INTO shift_group (shift_id, group_id) VALUES (%s, %s)", (chosen["into_shift_id"], gid))
            self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
            merged = self.shift_info(chosen["into_shift_id"])
            sent = self._family_messages(merged, "merge", now, case_id=case_id)
            self._close_case(case_id, now, "plan_b")
            self._event(case_id, now, "agent", "families_notified", sent)
            return {"families": sent["families"]}
        if kind == "lead":
            self._book_plan(case_id, shift_id, chosen["staff_id"], now)
            return {"staff_id": chosen["staff_id"]}
        if kind == "overtime":
            policy = self._policy()
            ranked, names, _, _ = self.ranked(shift_id, now)
            r = next(x for x in ranked if x.candidate.staff_id == chosen["staff_id"])
            pos = self.conn.execute("SELECT count(*) + 1 FROM offer WHERE case_id = %s", (case_id,)).fetchone()[0]
            return self._send_offer(case_id, shift_id, r, names, policy, now, pos, overtime=True)
        # reschedule or close
        if info["groups"]:
            self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
            remedy = chosen["remedy"]
            until = date.fromisoformat(chosen["valid_until"]) if chosen.get("valid_until") else None
            for g in info["groups"]:
                self.conn.execute(
                    "INSERT INTO remedy (shift_id, group_id, kind, families, valid_until, case_id, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (shift_id, g["id"], remedy, g["size"], until, case_id, now),
                )
            sent = self._family_messages(info, "cancel", now, case_id=case_id, remedy=remedy, until=until)
            self._close_case(case_id, now, "plan_b")
            self._event(case_id, now, "agent", "families_notified", sent)
            return {"families": sent["families"]}
        tz = ZoneInfo(info["tz"])
        closure = self._closure(info["venue_id"], info["zone_id"], "no_cover", info["start"], info["end"], now,
                                created_by="agent:plan_b", remedy=chosen["remedy"], skip_case=case_id)
        self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
        self._close_case(case_id, now, "plan_b")
        self._event(case_id, now, "agent", "zone_closed", {**closure, "local_day": info["start"].astimezone(tz).date()})
        return closure

    def _move_children(self, group_id: str, delta: int, families: dict[str, int]) -> None:
        sign = 1 if delta > 0 else -1
        row = self.conn.execute("SELECT size, family_counts FROM lesson_group WHERE id = %s FOR UPDATE", (group_id,)).fetchone()
        counts = {k: int(v) for k, v in row[1].items()}
        for lang, n in families.items():
            counts[lang] = max(0, counts.get(lang, 0) + sign * n)
        self.conn.execute("UPDATE lesson_group SET size = size + %s, family_counts = %s WHERE id = %s",
                          (delta, Jsonb({k: v for k, v in counts.items() if v}), group_id))

    def _settle_lesson(self, lesson_id: str, case_id: int, now: datetime) -> None:
        """After children moved out: the lesson needs only the instructors it has; open seats and their cases close."""
        effective, _, open_seats = self._lesson_seats(lesson_id)
        for seat in open_seats:
            self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (seat,))
            for (other,) in self.conn.execute("SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open' AND id <> %s",
                                              (seat, case_id)).fetchall():
                self._close_case(other, now, "cancelled")
                self._event(other, now, "agent", "seat_not_needed", {"by_case": case_id})
        self.conn.execute("UPDATE shift SET instructors = %s WHERE id = %s OR (extra_for = %s AND status <> 'cancelled')",
                          (effective, lesson_id, lesson_id))

    def _apply_ratio_option(self, case_id: int, snapshot: dict, chosen: dict, now: datetime) -> dict:
        ratio = snapshot["ratio"]
        lesson_id, group_id = ratio["lesson_id"], ratio["group_id"]
        group_name = self.conn.execute("SELECT name FROM lesson_group WHERE id = %s", (group_id,)).fetchone()[0]
        kind = chosen["kind"]
        if kind == "cancel_lesson":
            lesson = self.shift_info(lesson_id)
            seats = [r[0] for r in self.conn.execute("SELECT id FROM shift WHERE extra_for = %s", (lesson_id,))]
            self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s OR extra_for = %s", (lesson_id, lesson_id))
            for (other,) in self.conn.execute("SELECT id FROM cover_case WHERE shift_id = ANY(%s) AND status = 'open' AND id <> %s",
                                              ([lesson_id, *seats], case_id)).fetchall():
                self._close_case(other, now, "cancelled")
            for g in lesson["groups"]:
                self.conn.execute(
                    "INSERT INTO remedy (shift_id, group_id, kind, families, valid_until, case_id, created_at)"
                    " VALUES (%s, %s, 'credit', %s, NULL, %s, %s)",
                    (lesson_id, g["id"], g["size"], case_id, now),
                )
            sent = self._family_messages(lesson, "cancel", now, case_id=case_id, remedy="credit", until=None)
            self._close_case(case_id, now, "plan_b")
            self._event(case_id, now, "agent", "lesson_cancelled", {"shift_id": lesson_id, **sent})
            return {"families": sent["families"]}
        families = {k: int(v) for k, v in chosen["families"].items()}
        children = chosen["children"]
        self._move_children(group_id, -children, families)
        if kind == "split":
            new_id = f"{group_id}-S{case_id}"
            self.conn.execute(
                "INSERT INTO lesson_group (id, venue_id, zone_id, level_id, name, size, age_min, age_max, family_languages,"
                " family_counts, weekday, starts_local, minutes, instructor_id)"
                " SELECT %s, p.venue_id, p.zone_id, g.level_id, g.name || ' (split)', %s, g.age_min, g.age_max, %s, %s,"
                " g.weekday, g.starts_local, g.minutes, p.staff_id FROM lesson_group g, shift p WHERE g.id = %s AND p.id = %s",
                (new_id, children, sorted(families) or ["en"], Jsonb(families), group_id, chosen["into_shift_id"]),
            )
            self.conn.execute("INSERT INTO shift_group (shift_id, group_id) VALUES (%s, %s)", (chosen["into_shift_id"], new_id))
            coach = chosen["staff_name"].split()[0]
            for lang in families:
                language = M.lang_or_en(lang)
                self._message(now, "family", language, M.SPLIT_LESSON[language].format(group=group_name, coach=coach),
                              group_id=new_id, case_id=case_id)
            event, detail = "group_split", {"into_shift_id": chosen["into_shift_id"], "new_group_id": new_id}
        else:
            self._move_children(chosen["to_group_id"], children, families)
            target = self.shift_info(chosen["to_shift_id"])
            for lang in families:
                language = M.lang_or_en(lang)
                self._message(now, "family", language, M.MOVED_LESSON[language].format(
                    group=group_name, when=self._when(target), venue=target["venue"]), group_id=chosen["to_group_id"],
                    case_id=case_id)
            event, detail = "children_moved", {"to_shift_id": chosen["to_shift_id"], "to_group_id": chosen["to_group_id"]}
        self._settle_lesson(lesson_id, case_id, now)
        self._close_case(case_id, now, "plan_b")
        self._event(case_id, now, "agent", event, {**detail, "group_id": group_id, "children": children,
                                                    "families": families})
        return {"children": children, **detail}

    def _book_plan(self, case_id: int, shift_id: str, staff_id: str, now: datetime) -> None:
        info = self.shift_info(shift_id)
        self.conn.execute("UPDATE shift SET staff_id = %s, status = 'scheduled' WHERE id = %s", (staff_id, shift_id))
        self._close_case(case_id, now, "plan_b")
        person = self._staff_row(staff_id)
        self._event(case_id, now, "agent", "shift_covered", {"staff_id": staff_id, "name": person["name"], "via": "plan_b"})
        lang = M.lang_or_en(person["language"])
        self._message(now, "staff", lang, M.CONFIRMED[lang].format(when=self._when(info), venue=info["venue"]),
                      staff_id=staff_id, case_id=case_id)

    def _close_case(self, case_id: int, now: datetime, status: str) -> None:
        self.conn.execute("UPDATE cover_case SET status = %s, closed_at = %s WHERE id = %s", (status, now, case_id))
        withdrawn = self.conn.execute(
            "UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status IN ('pending', 'held') AND NOT overtime"
            " RETURNING id", (case_id,),
        ).fetchall()
        if withdrawn:
            self._event(case_id, now, "agent", "offers_closed", {"offer_ids": [r[0] for r in withdrawn], "case": status})

    # --- families ---------------------------------------------------------------

    def _family_messages(self, info: dict, event: str, now: datetime, *, case_id: int | None = None,
                         closure_id: int | None = None, remedy: str | None = None, until: date | None = None,
                         reason: str | None = None, venue_info: dict | None = None) -> dict:
        """One text per language (model, else template), filled per group; returns who was told."""
        groups = info["groups"]
        langs = list(dict.fromkeys(M.lang_or_en(lang) for g in groups for lang in g["languages"]))
        day = info["local_start"].date()
        templates: dict[str, tuple[str, str]] = {}
        for lang in langs:
            remedy_text = M.REMEDY[remedy][lang].format(until=until) if remedy else ""
            facts = {"event": event, "language": lang, "venue": info["venue"], "date": day,
                     "reason": M.REASON[reason][lang] if reason else None, "remedy": remedy_text}
            body, source = None, "template"
            try:
                answer = self.models.chat_json(purpose="family_message", system=M.FAMILY_SYSTEM,
                                               user=json.dumps(plain(facts), ensure_ascii=False), schema=M.FAMILY_SCHEMA,
                                               key=plain(facts), at=now, case_id=case_id)
                if "{group}" in answer.data["body"] and "{time}" in answer.data["body"]:
                    body, source = answer.data["body"], "model"
            except ModelError:
                pass
            if body is None:
                body = (M.FAMILY[event][lang].replace("{venue}", info["venue"]).replace("{date}", f"{day:%d.%m}")
                        .replace("{remedy}", remedy_text).replace("{reason}", facts["reason"] or ""))
            templates[lang] = (body, source)
        count = 0
        for g in groups:
            start = info["local_start"]
            for lang in dict.fromkeys(M.lang_or_en(x) for x in g["languages"]):
                body = templates[lang][0].replace("{group}", g["name"]).replace("{time}", f"{start:%H:%M}")
                self._message(now, "family", lang, body, group_id=g["id"], case_id=case_id, closure_id=closure_id)
                count += 1
        return {
            "messages": count,
            "groups": [g["id"] for g in groups],
            "families": sum(g["size"] for g in groups),
            "languages": langs,
            "text_source": {lang: src for lang, (_, src) in templates.items()},
        }

    # --- closures ---------------------------------------------------------------

    def closure(self, venue_id: str, zone_id: str | None, reason: str, starts_at: datetime, ends_at: datetime,
                now: datetime, created_by: str, remedy: str | None = None) -> dict:
        with self.conn.transaction():
            return self._closure(venue_id, zone_id, reason, starts_at, ends_at, now, created_by, remedy)

    def _closure(self, venue_id: str, zone_id: str | None, reason: str, starts_at: datetime, ends_at: datetime,
                 now: datetime, created_by: str, remedy: str | None = None, skip_case: int | None = None) -> dict:
        if ends_at <= starts_at:
            raise ValueError("a closure ends after it starts")
        if reason not in M.REASON:
            raise ValueError(f"reason must be one of {sorted(M.REASON)}")
        policy = self._policy()
        remedy = remedy or policy.default_remedy
        if remedy not in ("credit", "makeup"):
            raise ValueError("remedy must be credit or makeup")
        venue = self.conn.execute("SELECT tz FROM venue WHERE id = %s AND policy_id = %s", (venue_id, self.policy_id)).fetchone()
        if venue is None:
            raise NotFound(f"unknown venue {venue_id!r}")
        closure_id = self.conn.execute(
            "INSERT INTO closure (venue_id, zone_id, reason, starts_at, ends_at, remedy, created_by, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (venue_id, zone_id, reason, starts_at, ends_at, remedy, created_by, now),
        ).fetchone()[0]
        affected = self.conn.execute(
            "SELECT s.id FROM shift s WHERE s.venue_id = %s AND (%s::text IS NULL OR s.zone_id = %s)"
            " AND s.status IN ('scheduled', 'open') AND s.starts_at < %s AND s.ends_at > %s"
            " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id) ORDER BY s.starts_at, s.id",
            (venue_id, zone_id, zone_id, ends_at, starts_at),
        ).fetchall()
        partial = self.conn.execute(
            "SELECT s.id, s.staff_id FROM shift s WHERE s.venue_id = %s AND (%s::text IS NULL OR s.zone_id = %s)"
            " AND s.status = 'scheduled' AND s.starts_at < %s AND s.ends_at > %s"
            " AND NOT EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id) ORDER BY s.starts_at, s.id",
            (venue_id, zone_id, zone_id, ends_at, starts_at),
        ).fetchall()
        tz = ZoneInfo(venue[0])
        until = starts_at.astimezone(tz).date() + timedelta(days=policy.makeup_window_days) if remedy == "makeup" else None
        families = messages_sent = 0
        groups: list[str] = []
        languages: set[str] = set()
        for (shift_id,) in affected:
            info = self.shift_info(shift_id)
            self.conn.execute("UPDATE shift SET status = 'cancelled' WHERE id = %s", (shift_id,))
            for case_id, in self.conn.execute(
                "SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)
            ).fetchall():
                if case_id == skip_case:
                    continue
                self.conn.execute("UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status = 'pending'", (case_id,))
                self._close_case(case_id, now, "cancelled")
                self._event(case_id, now, "agent", "cancelled_by_closure", {"closure_id": closure_id})
            for g in info["groups"]:
                self.conn.execute(
                    "INSERT INTO remedy (shift_id, group_id, kind, families, valid_until, closure_id, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (shift_id, g["id"], remedy, g["size"], until, closure_id, now),
                )
            sent = self._family_messages(info, "closure", now, closure_id=closure_id, remedy=remedy, until=until, reason=reason)
            families += sent["families"]
            messages_sent += sent["messages"]
            groups += sent["groups"]
            languages |= set(sent["languages"])
        return {
            "closure_id": closure_id,
            "cancelled_shifts": [sid for (sid,) in affected],
            "groups": groups,
            "families": families,
            "messages": messages_sent,
            "languages": sorted(languages),
            "remedy": remedy,
            "staff_on_site": [{"shift_id": sid, "staff_id": st} for sid, st in partial],
        }

    # --- certificate cards ------------------------------------------------------

    def read_certificate(self, staff_id: str, image: bytes, mime: str, now: datetime, cert_type: str | None = None) -> dict:
        """Two independent readers. Trusted only when both agree on the dates (the database checks it too);
        a different card number alone is a note ("check_id"), not a block. Another person's name, an unknown
        type or a failed reader sends the card to a person."""
        person = self._staff_row(staff_id)
        policy = self._policy()
        if cert_type is not None and cert_type not in policy.preset.cert_types:
            raise ValueError(f"unknown certificate type {cert_type!r}")
        ref = {"staff_id": staff_id}
        readings: dict[str, dict | None] = {}
        flags: list[str] = []
        notes: list[str] = []
        for model in (TEXT_MODEL, SECOND_READER_MODEL):
            try:
                readings[model] = self.models.read_card(image, mime, model=model, at=now, ref=ref).data
            except ModelError:
                readings[model] = None
                flags.append(f"reader_failed:{model}")
        a, b = readings[TEXT_MODEL], readings[SECOND_READER_MODEL]
        first = a or b
        if first is None:
            return {"cards": [], "flags": flags, "notes": notes, "review": True, "readings": readings}
        if cert_type:
            types = [cert_type]
        else:
            title = " ".join(((r or {}).get("title") or "") for r in (a, b)).upper()
            types = [c.code for c in policy.preset.cert_types.values() if any(m in title for m in c.match)]
        if not types:
            flags.append("unknown_type")
        wanted = _name_key(person["name"])
        if not any(_name_key((r or {}).get("name")) == wanted for r in (a, b) if r):
            flags.append("name_mismatch")
        dates = {m: (_card_date((r or {}).get("issued")), _card_date((r or {}).get("exp"))) for m, r in readings.items() if r}
        issued, expires = dates.get(TEXT_MODEL) or dates.get(SECOND_READER_MODEL)
        if issued is None:
            flags.append("unreadable_dates")
        if a is not None and b is not None:
            if dates[TEXT_MODEL] != dates[SECOND_READER_MODEL]:
                flags.append("dates_disagree")
            elif _id_key(a.get("id")) != _id_key(b.get("id")):
                notes.append("check_id")
        cards = []
        sha = sha256_bytes(image)
        with self.conn.transaction():
            for code in types if issued else []:
                def reader(model: str) -> dict | None:
                    got = readings[model]
                    if got is None:
                        return None
                    got_issued, got_exp = dates[model]
                    return {"model": model, "fields": {"type": code, "card_number": got.get("id"),
                                                       "issued_on": got_issued, "expires_on": got_exp},
                            "name": got.get("name"), "title": got.get("title"), "read_at": now.isoformat()}

                row = self.conn.execute(
                    "INSERT INTO staff_cert (staff_id, cert_type_id, card_number, issued_on, expires_on, reader_a, reader_b,"
                    " flags, notes, image_sha256) VALUES (%s, (SELECT id FROM cert_type WHERE preset_code = %s AND code = %s),"
                    " %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, needs_review",
                    (staff_id, policy.preset.code, code, first.get("id"), issued, expires,
                     Jsonb(reader(TEXT_MODEL)) if a else None, Jsonb(reader(SECOND_READER_MODEL)) if b else None,
                     [f for f in flags if f != "dates_disagree"], notes, sha),
                ).fetchone()
                cards.append({"id": row[0], "type": code, "issued_on": issued, "expires_on": expires, "review": row[1]})
        return {"cards": cards, "flags": flags, "notes": notes,
                "review": bool(flags) or any(c["review"] for c in cards), "readings": readings}


def _audio_mime(filename: str) -> str:
    return "audio/ogg" if filename.lower().endswith((".ogg", ".oga", ".opus")) else "application/octet-stream"


def _name_key(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def _id_key(value: str | None) -> str:
    return " ".join((value or "").split()).upper()


def _card_date(value: str | None) -> str | None:
    """ISO (YYYY-MM-DD) or US (MM/DD/YYYY) date from a reading, as ISO; None when missing or unreadable."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None
