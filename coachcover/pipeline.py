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

import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from coachcover import messages as M
from coachcover.model import Policy, Shift
from coachcover.models import SECOND_READER_MODEL, TEXT_MODEL, ModelClient, ModelError, canonical_key, sha256_bytes
from coachcover.reasons_text import describe, describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import Code, Ranked, assess, eligible, rank

MODE_ORDER = {"auto": 0, "propose": 1, "off": 2}
OPTION_ORDER = ("merge", "lead", "overtime", "reschedule", "close")


class PipelineError(Exception):
    pass


class NotFound(PipelineError):
    pass


class Conflict(PipelineError):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def strictest(*modes: str) -> str:
    return max(modes, key=MODE_ORDER.__getitem__)


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
            dict(zip(("id", "name", "size", "languages", "level", "level_label", "program"), g))
            for g in self.conn.execute(
                "SELECT g.id, g.name, g.size, g.family_languages, l.code, l.label, p.code FROM shift_group sg"
                " JOIN lesson_group g ON g.id = sg.group_id JOIN program_level l ON l.id = g.level_id"
                " JOIN program p ON p.id = l.program_id WHERE sg.shift_id = %s ORDER BY g.id",
                (shift_id,),
            )
        ]
        tz = ZoneInfo(info["tz"])
        start, end = info["start"].astimezone(tz), info["end"].astimezone(tz)
        levels = ", ".join(dict.fromkeys(g["level_label"] for g in info["groups"]))
        info["local_start"], info["local_end"] = start, end
        info["label"] = f"{start:%a %H:%M}-{end:%H:%M} · {info['role_label']}" + (f" · {levels}" if levels else "") + f" · {info['venue']}"
        return info

    def _when(self, info: dict) -> str:
        return f"{info['local_start']:%a %d.%m %H:%M}-{info['local_end']:%H:%M}"

    def _what(self, info: dict) -> str:
        return info["role_label"] + (f", {info['groups'][0]['level_label']}" if info["groups"] else "")

    # --- candidates -----------------------------------------------------------

    def ranked(self, shift_id: str, now: datetime) -> tuple[list[Ranked], dict, Policy, int]:
        """All non-reserve staff, ranked for the shift; plus a name map and the number of reserve people."""
        policy = self._policy()
        staff = load_staff(self.conn, self.policy_id)
        shift = load_shift(self.conn, shift_id)
        context = load_context(self.conn, self.policy_id, shift)
        reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
        people = [s for s in staff if not (s.roles & reserve_roles)]
        names = {s.id: s.name for s in staff}
        return rank(assess(s, shift, now, policy, context) for s in people), names, policy, len(staff) - len(people)

    def candidates(self, shift_id: str, now: datetime) -> dict:
        ranked, names, policy, reserve = self.ranked(shift_id, now)
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
        return {
            "shift_id": shift_id,
            "total": len(rows) + reserve,
            "reserve": reserve,
            "eligible": eligible_count,
            "filtered": len(rows) - eligible_count,
            "chips": [{"code": code, "count": n} for code, n in sorted(chips.items(), key=lambda kv: (-kv[1], kv[0]))],
            "queue": rows,
        }

    # --- call-out -------------------------------------------------------------

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
                     "cost_usd": heard.record.cost_usd, "recorded": heard.record.source == "recorded"}
        today = now.astimezone(tz).date()
        todays = [
            (sid, start)
            for sid, start in self.conn.execute(
                "SELECT id, starts_at FROM shift WHERE staff_id = %s AND status = 'scheduled' AND ends_at > %s ORDER BY starts_at, id",
                (staff_id, now),
            )
            if start.astimezone(tz).date() == today
        ]
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
        result = {"callout_id": callout_id, "staff_id": staff_id, "text": text, "understood": understood, "cases": []}
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
        opened = []
        with self.conn.transaction():
            for shift_id, _ in matched:
                case_id = self._open_case(shift_id, staff_id, now, "callout", callout_id)
                info = self.shift_info(shift_id)
                self._event(case_id, now, "staff", "callout_received",
                            {"channel": "voice" if audio is not None else "text", "text": text,
                             "staff_id": staff_id, "staff_name": person["name"], "callout_id": callout_id})
                if voice:
                    self._event(case_id, now, "agent", "voice_transcribed", voice)
                self._event(case_id, now, "agent", "callout_understood",
                            {"by": by, "error": error, "understood": understood,
                             "matched_shift_ids": [sid for sid, _ in matched]})
                self._event(case_id, now, "agent", "shift_opened", {"shift_id": shift_id, "label": info["label"]})
                opened.append((case_id, shift_id))
        for case_id, shift_id in opened:
            summary = self.candidates(shift_id, now)
            with self.conn.transaction():
                self._event(case_id, now, "agent", "candidates_ranked", {
                    "total": summary["total"], "reserve": summary["reserve"], "eligible": summary["eligible"],
                    "filtered": summary["filtered"], "chips": summary["chips"],
                    "top": [q["line"] for q in summary["queue"][:5]],
                    "grid": [
                        {"staff_id": q["staff_id"], "name": q["name"], "position": q["position"], "eligible": q["eligible"],
                         "code": next((r["code"] for r in q["reasons"] if r["severity"] == "block"), None),
                         "warning": any(r["severity"] == "warn" for r in q["reasons"])}
                        for q in summary["queue"]
                    ],
                })
            self._offer_next(case_id, now)
            result["cases"].append({"case_id": case_id, "shift_id": shift_id})
        return result

    def _open_case(self, shift_id: str, absent: str | None, now: datetime, source: str, callout_id: str | None) -> int:
        open_case = self.conn.execute(
            "SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)
        ).fetchone()
        if open_case:
            raise Conflict(f"shift {shift_id} already has an open case", {"case_id": open_case[0]})
        self.conn.execute("SELECT 1 FROM shift WHERE id = %s FOR UPDATE", (shift_id,))
        self.conn.execute("UPDATE shift SET status = 'open', staff_id = NULL WHERE id = %s", (shift_id,))
        self.conn.execute("UPDATE offer SET status = 'released' WHERE shift_id = %s AND status = 'accepted'", (shift_id,))
        return self.conn.execute(
            "INSERT INTO cover_case (shift_id, absent_staff_id, source, opened_at, callout_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (shift_id, absent, source, now, callout_id),
        ).fetchone()[0]

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
            pending = self.conn.execute(
                "SELECT id FROM offer WHERE case_id = %s AND status = 'pending'", (case_id,)
            ).fetchone()
            if pending:
                return {"offer_id": pending[0]}
            ranked, names, policy, _ = self.ranked(shift_id, now)
            if policy.autonomy["assign_cover"] == "off":
                self._event(case_id, now, "agent", "offers_off", {"why": "policy: assign_cover is off"})
                return None
            offered = {r[0] for r in self.conn.execute("SELECT staff_id FROM offer WHERE case_id = %s", (case_id,))}
            queue = [r for r in ranked if r.candidate.decision.ok and r.candidate.staff_id not in offered]
            if queue:
                return self._send_offer(case_id, shift_id, queue[0], names, policy, now, len(offered) + 1)
            self._event(case_id, now, "agent", "queue_exhausted", {"offered": len(offered)})
        return self.propose_plan_b(case_id, now)

    def _send_offer(self, case_id: int, shift_id: str, r: Ranked, names: dict, policy: Policy, now: datetime,
                    pos: int, overtime: bool = False) -> dict:
        staff_id = r.candidate.staff_id
        info = self.shift_info(shift_id)
        expires = now + timedelta(minutes=policy.offer_timeout_minutes)
        place = {**r.place(), "line": describe_place(r, names[staff_id].split()[0], "en", policy.preset), "overtime": overtime}
        offer_id = self.conn.execute(
            "INSERT INTO offer (case_id, shift_id, staff_id, queue_pos, place, sent_at, expires_at, overtime)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (case_id, shift_id, staff_id, pos, Jsonb(plain(place)), now, expires, overtime),
        ).fetchone()[0]
        lang = M.lang_or_en(self._staff_row(staff_id)["language"])
        body = M.OFFER[lang].format(when=self._when(info), venue=info["venue"], what=self._what(info),
                                    minutes=policy.offer_timeout_minutes)
        if overtime:
            body += M.OFFER_OVERTIME[lang]
        self._message(now, "staff", lang, body, staff_id=staff_id, case_id=case_id, offer_id=offer_id)
        self._event(case_id, now, "agent", "offer_sent", {
            "offer_id": offer_id, "staff_id": staff_id, "name": names[staff_id], "queue_pos": pos,
            "line": place["line"], "expires_at": expires, "overtime": overtime,
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
        for case_id in dict.fromkeys(case_id for _, case_id, _ in rows):
            self._offer_next(case_id, now)
        return len(rows)

    def respond(self, offer_id: int, accept: bool, now: datetime) -> dict:
        """A staff answer. The person is checked again at the moment of "yes"."""
        self.expire_due(now)
        move_on = False
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT o.case_id, o.shift_id, o.staff_id, o.status, o.overtime FROM offer o WHERE o.id = %s FOR UPDATE",
                (offer_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"unknown offer {offer_id}")
            case_id, shift_id, staff_id, status, overtime = row
            self.conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (case_id,))
            if status != "pending":
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
                    self.conn.execute("UPDATE offer SET status = 'withdrawn', responded_at = %s WHERE id = %s", (now, offer_id))
                    why = "; ".join(describe(r, "en", policy.preset) for r in blocking)
                    self._event(case_id, now, "agent", "accept_rejected",
                                {"offer_id": offer_id, "staff_id": staff_id, "codes": [r.code.value for r in blocking], "why": why})
                    lang = M.lang_or_en(self._staff_row(staff_id)["language"])
                    self._message(now, "staff", lang, M.NO_LONGER[lang].format(when=self._when(info), why=why),
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
        if move_on:
            self._offer_next(case_id, now)
        return self.offer_state(offer_id)

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
            if approve:
                self._book(case_id, payload["shift_id"], payload["staff_id"], now,
                           via={"offer_id": payload["offer_id"], "approval_id": approval_id, "approved_by": by})
            else:
                self.conn.execute("UPDATE offer SET status = 'withdrawn' WHERE id = %s", (payload["offer_id"],))
                self._event(case_id, now, "manager", "approval_rejected", {"approval_id": approval_id, "by": by})
        if not approve:
            self._offer_next(case_id, now)
        return {"approval_id": approval_id, "status": "approved" if approve else "rejected"}

    # --- plan B ---------------------------------------------------------------

    def _plan_facts(self, case_id: int, now: datetime) -> dict:
        shift_id, absent = self.conn.execute(
            "SELECT shift_id, absent_staff_id FROM cover_case WHERE id = %s", (case_id,)
        ).fetchone()
        policy = self._policy()
        auto = policy.autonomy
        staff = {s.id: s for s in load_staff(self.conn, self.policy_id)}
        shift = load_shift(self.conn, shift_id)
        context = load_context(self.conn, self.policy_id, shift)
        info = self.shift_info(shift_id)
        tz = ZoneInfo(info["tz"])
        families = sum(g["size"] for g in info["groups"])
        group_ids = [g["id"] for g in info["groups"]]
        options: list[dict] = []
        rejected: list[dict] = []
        if shift.groups and auto["merge_groups"] != "off":
            partners = self.conn.execute(
                "SELECT s.id FROM shift s WHERE s.venue_id = %s AND s.role_id = (SELECT role_id FROM shift WHERE id = %s)"
                " AND s.starts_at = %s AND s.ends_at = %s AND s.status = 'scheduled' AND s.id <> %s"
                " AND EXISTS (SELECT 1 FROM shift_group sg WHERE sg.shift_id = s.id) ORDER BY s.id",
                (shift.venue, shift_id, shift.start, shift.end, shift_id),
            ).fetchall()
            for (partner_id,) in partners:
                partner = load_shift(self.conn, partner_id)
                teacher = staff.get(partner.assignee)
                merged = Shift(partner.id, partner.venue, partner.role, partner.start, partner.end,
                               partner.groups + shift.groups, partner.instructors, partner.zone,
                               assignee=None, merged_shift_ids=(shift_id,))
                decision = eligible(teacher, merged, now, policy, context)
                participants = sum(g.size for g in merged.groups)
                if decision.ok:
                    per = min(policy.preset.programs[g.program].levels[g.level].max_per_instructor for g in merged.groups)
                    options.append({
                        "kind": "merge", "mode": strictest(auto["plan_b"], auto["merge_groups"]),
                        "into_shift_id": partner.id, "staff_id": teacher.id, "staff_name": teacher.name,
                        "groups": [g.id for g in merged.groups], "participants": participants,
                        "capacity": per * merged.instructors,
                    })
                else:
                    rejected.append({"kind": "merge", "into_shift_id": partner.id, "participants": participants,
                                     "codes": [r.code.value for r in decision.reasons if r.blocking]})
        reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
        leads = [s for s in staff.values() if s.roles & reserve_roles and s.id != absent]
        lead_rank = rank(assess(s, shift, now, policy, context) for s in leads)
        ready = [r for r in lead_rank if r.candidate.decision.ok]
        if ready:
            lead = staff[ready[0].candidate.staff_id]
            options.append({"kind": "lead", "mode": auto["plan_b"], "staff_id": lead.id, "staff_name": lead.name})
        elif leads:
            rejected.append({"kind": "lead", "codes": sorted({r.code.value for x in lead_rank
                                                               for r in x.candidate.decision.reasons if r.blocking})})
        if auto["overtime"] != "off":
            people = [s for s in staff.values() if not (s.roles & reserve_roles)]
            for r in rank(assess(s, shift, now, policy, context) for s in people):
                blocking = [x for x in r.candidate.decision.reasons if x.blocking]
                if blocking and all(x.code == Code.HOURS_LIMIT_EXCEEDED for x in blocking):
                    over = round(sum(x.params["hours_after"] - x.params["limit_hours"] for x in blocking), 2)
                    person = staff[r.candidate.staff_id]
                    options.append({
                        "kind": "overtime", "mode": strictest("propose", auto["plan_b"]),
                        "staff_id": person.id, "staff_name": person.name, "hours_over": over,
                        "cost_usd": round(over * policy.overtime_cost_per_hour, 2),
                    })
                    break
        local_day = info["local_start"].date()
        if shift.groups:
            options.append({
                "kind": "reschedule", "mode": auto["plan_b"], "groups": group_ids, "families": families,
                "remedy": "makeup", "valid_until": local_day + timedelta(days=policy.makeup_window_days),
            })
        options.append({
            "kind": "close", "mode": auto["plan_b"], "zone_id": info["zone_id"], "groups": group_ids,
            "families": families, "remedy": policy.default_remedy,
            "valid_until": local_day + timedelta(days=policy.makeup_window_days) if policy.default_remedy == "makeup" else None,
        })
        options.sort(key=lambda o: OPTION_ORDER.index(o["kind"]))
        return plain({
            "case_id": case_id,
            "shift": {"id": shift_id, "label": info["label"], "venue": info["venue"], "zone_id": info["zone_id"],
                      "start": info["start"], "end": info["end"], "role": info["role"]},
            "options": options,
            "recommended": 0,
            "rejected": rejected,
        })

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
            raise Conflict(f"plan {plan_id} is {status}", {"status": status})
        if digest != stored:
            raise Conflict("the plan changed since it was shown", {"plan_id": plan_id, "hash": stored})
        fresh = self._plan_facts(case_id, now)
        if any(fresh[k] != snapshot[k] for k in ("shift", "options", "recommended", "rejected")):
            new_plan = self.propose_plan_b(case_id, now)
            raise Conflict("the situation changed; a new plan replaces the shown one",
                           {"plan_id": new_plan["plan_id"] if new_plan else None,
                            "hash": new_plan["hash"] if new_plan else None})
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

    def read_certificate(self, staff_id: str, image: bytes, mime: str, now: datetime) -> dict:
        """Two independent readers; a disagreement, an unknown type or another name sends the card to a person."""
        person = self._staff_row(staff_id)
        policy = self._policy()
        ref = {"staff_id": staff_id}
        readings: dict[str, dict | None] = {}
        flags: list[str] = []
        for model in (TEXT_MODEL, SECOND_READER_MODEL):
            try:
                readings[model] = self.models.read_card(image, mime, model=model, at=now, ref=ref).data
            except ModelError:
                readings[model] = None
                flags.append(f"reader_failed:{model}")
        first = readings[TEXT_MODEL] or readings[SECOND_READER_MODEL]
        if first is None:
            return {"cards": [], "flags": flags, "readings": readings}
        title = (first.get("title") or "").upper()
        types = [c.code for c in policy.preset.cert_types.values() if any(m in title for m in c.match)]
        if not types:
            flags.append("unknown_type")
        if " ".join((first.get("name") or "").split()).casefold() != " ".join(person["name"].split()).casefold():
            flags.append("name_mismatch")

        def us_date(value: str | None) -> str | None:
            try:
                return datetime.strptime(value, "%m/%d/%Y").date().isoformat() if value else None
            except ValueError:
                return None

        issued, expires = us_date(first.get("issued")), us_date(first.get("exp"))
        if issued is None:
            flags.append("unreadable_dates")
        a, b = readings[TEXT_MODEL], readings[SECOND_READER_MODEL]
        if a is not None and b is not None and (
            any(us_date(a.get(k)) != us_date(b.get(k)) for k in ("issued", "exp")) or a.get("id") != b.get("id")
        ):
            flags.append("readers_disagree")
        cards = []
        sha = sha256_bytes(image)
        with self.conn.transaction():
            for code in types if issued else []:
                def reader(model: str) -> dict | None:
                    got = readings[model]
                    if got is None:
                        return None
                    return {"model": model, "fields": {"type": code, "card_number": got.get("id"),
                                                       "issued_on": us_date(got.get("issued")),
                                                       "expires_on": us_date(got.get("exp"))},
                            "name": got.get("name"), "title": got.get("title"), "read_at": now.isoformat()}

                row = self.conn.execute(
                    "INSERT INTO staff_cert (staff_id, cert_type_id, card_number, issued_on, expires_on, reader_a, reader_b,"
                    " flags, image_sha256) VALUES (%s, (SELECT id FROM cert_type WHERE preset_code = %s AND code = %s),"
                    " %s, %s, %s, %s, %s, %s, %s) RETURNING id, needs_review",
                    (staff_id, policy.preset.code, code, first.get("id"), issued, expires,
                     Jsonb(reader(TEXT_MODEL)) if readings[TEXT_MODEL] else None,
                     Jsonb(reader(SECOND_READER_MODEL)) if readings[SECOND_READER_MODEL] else None,
                     flags, sha),
                ).fetchone()
                cards.append({"id": row[0], "type": code, "issued_on": issued, "expires_on": expires, "review": row[1]})
        return {"cards": cards, "flags": flags, "review": bool(flags) or any(c["review"] for c in cards),
                "readings": readings}
