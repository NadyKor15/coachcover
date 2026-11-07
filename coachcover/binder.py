"""The binder for an inspector or an insurer: one PDF for a venue and a period.

Every scheduled shift of the period with the person on it, checked again by the core rules at the shift's
start (cards valid on that date, programs and levels, weekly hours, overlaps, participant norm); every
cover case with a hash of its event file; every closure (incident) with a hash of its record; and the total
"N shifts · V violations". The whole binder has its own digest (sha256 of its canonical JSON), printed on
every page and sent as the X-Binder-SHA256 header, so a printed copy can be matched to the data.
"""

from __future__ import annotations

import hashlib
import re
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from fpdf import FPDF
from fpdf.fonts import FontFace

from coachcover import notify
from coachcover.model import Shift
from coachcover.reasons_text import describe
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import cert_expiry, eligible, ratio_reason

try:  # a stricter norm for young children, when the rules have one
    from coachcover.rules import group_norm  # type: ignore[attr-defined]
except ImportError:  # rules without it: every group uses its level's norm
    group_norm = None

MAX_DAYS = 62


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sha256_json(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _required(policy, shift: Shift) -> tuple[str, ...]:
    role = policy.preset.roles[shift.role]
    extra = tuple(getattr(shift, "extra_certs", ()))
    return tuple(dict.fromkeys((*role.required_certs, *extra)))


def _cards_on(policy, person, shift: Shift, tz: ZoneInfo) -> list[str]:
    day = shift.start.astimezone(tz).date()
    out = []
    for code in _required(policy, shift):
        ctype = policy.preset.cert_types[code]
        valid = [c for c in person.certs if c.cert_type == code and c.issued_on <= day <= cert_expiry(c, ctype)]
        label = getattr(ctype, "short", "") or ctype.label
        if valid:
            last = max(cert_expiry(c, ctype) for c in valid)
            out.append(f"{label} to {last:%Y-%m-%d}")
        else:
            out.append(f"{label}: none valid")
    return out


def _norm(policy, shift: Shift) -> str:
    if not shift.groups:
        return "-"
    participants = sum(g.size for g in shift.groups)
    reason = ratio_reason(shift.groups, shift.instructors, policy.preset)
    if reason is not None:
        p = reason.params
        return f"{participants}/{p['capacity']} over 1:{p['max_per_instructor']}"
    if group_norm is not None:
        per = min(group_norm(g, policy.preset) for g in shift.groups)
    else:
        per = min(policy.preset.programs[g.program].levels[g.level].max_per_instructor for g in shift.groups)
    return f"{participants}/{per * shift.instructors} ok (1:{per})"


def build(conn: psycopg.Connection, policy_id: str, venue_id: str | None, first: date | None, last: date | None,
          now: datetime) -> dict:
    venue = conn.execute(
        "SELECT id, name, tz FROM venue WHERE policy_id = %s AND (%s::text IS NULL OR id = %s) ORDER BY id LIMIT 1",
        (policy_id, venue_id, venue_id),
    ).fetchone()
    if venue is None:
        raise ValueError(f"unknown venue {venue_id!r}")
    vid, vname, tzname = venue
    tz = ZoneInfo(tzname)
    policy = load_policy(conn, policy_id)
    if first is None and last is None:
        first = _default_week(conn, vid, tz, policy.week_start, now)
    first = first or _week_of(now.astimezone(tz).date(), policy.week_start)
    last = last or (first + timedelta(days=6))
    if last < first:
        raise ValueError("the period ends before it starts")
    if (last - first).days + 1 > MAX_DAYS:
        raise ValueError(f"a binder covers at most {MAX_DAYS} days")
    start = datetime.combine(first, time(0), tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=tz)
    staff = {s.id: s for s in load_staff(conn, policy_id)}
    names = dict(conn.execute("SELECT id, full_name FROM staff").fetchall())
    rows = conn.execute(
        "SELECT s.id, s.status, s.staff_id FROM shift s WHERE s.venue_id = %s AND s.starts_at >= %s AND s.starts_at < %s"
        " AND s.status IN ('scheduled', 'open') ORDER BY s.starts_at, s.id",
        (vid, start, end),
    ).fetchall()
    context = load_context(conn, policy_id, Shift(id="binder", venue=vid, role=next(iter(policy.preset.roles)),
                                                   start=start, end=end))
    # shifts the night check caught before they started and a replacement then covered
    caught_by_night: dict[str, tuple[str, str | None]] = {}
    for case_id, shift_id, data in conn.execute(
        "SELECT c.id, c.shift_id, e.data FROM cover_case c JOIN shift s ON s.id = c.shift_id"
        " JOIN case_event e ON e.case_id = c.id AND e.kind = 'shift_covered'"
        " WHERE s.venue_id = %s AND c.source = 'night_check' AND c.status IN ('covered', 'plan_b')"
        " AND s.starts_at >= %s AND s.starts_at < %s ORDER BY c.closed_at, c.id",
        (vid, start, end),
    ).fetchall():
        caught_by_night[shift_id] = (notify.case_ref(conn, case_id), data.get("name"))
    # and those it caught whose case is still open: still a violation, with the case that is finding a replacement
    caught_open_by_night = {shift_id: notify.case_ref(conn, case_id) for case_id, shift_id in conn.execute(
        "SELECT c.id, c.shift_id FROM cover_case c JOIN shift s ON s.id = c.shift_id"
        " WHERE s.venue_id = %s AND c.source = 'night_check' AND c.closed_at IS NULL"
        " AND s.starts_at >= %s AND s.starts_at < %s ORDER BY c.opened_at, c.id",
        (vid, start, end),
    ).fetchall()}
    shifts, violations, uncovered, caught, caught_open = [], 0, 0, 0, 0
    for shift_id, status, holder in rows:
        shift = load_shift(conn, shift_id)
        local = shift.start.astimezone(tz)
        role = policy.preset.roles[shift.role]
        row = {"shift_id": shift_id, "date": local.date().isoformat(), "time": f"{local:%H:%M}-{shift.end.astimezone(tz):%H:%M}",
               "post": (getattr(role, "short", "") or role.label)
                       + (f" · {', '.join(dict.fromkeys(policy.preset.programs[g.program].levels[g.level].label for g in shift.groups))}"
                          if shift.groups else ""),
               "person": names.get(holder) if holder else None, "staff_id": holder, "norm": _norm(policy, shift)}
        if status == "open" or holder is None:
            uncovered += 1
            row.update({"cards": [], "result": "no one on the post", "ok": False, "issues": ["uncovered"]})
            shifts.append(row)
            continue
        person = staff.get(holder)
        if person is None:  # a person from another venue's rules (a network loan): checked by this venue's rules too
            person = _guest(conn, holder)
        check = eligible(person, replace(shift, assignee=None), shift.start, policy, context)
        blocking = [describe(r, "en", policy.preset) for r in check.reasons if r.blocking]
        warnings = [describe(r, "en", policy.preset) for r in check.reasons if not r.blocking]
        violations += 1 if blocking else 0
        row.update({"cards": _cards_on(policy, person, shift, tz), "ok": not blocking, "issues": blocking,
                    "warnings": warnings,
                    "result": ("VIOLATION: " + "; ".join(blocking)) if blocking
                    else "ok" + (f" (note: {'; '.join(warnings)})" if warnings else "")})
        if shift_id in caught_by_night and not blocking:
            ref, name = caught_by_night[shift_id]
            caught += 1
            row.update({"caught": True, "result": f"caught by the nightly check · case {ref} · covered by {name}"})
        elif shift_id in caught_open_by_night and blocking:
            caught_open += 1
            row.update({"caught_open": True, "result": row["result"] + " · caught by the nightly check · case "
                        f"{caught_open_by_night[shift_id]} · replacement being found"})
        shifts.append(row)
    cases = []
    for case_id, status, shift_id, absent, opened, closed in conn.execute(
        "SELECT c.id, c.status, c.shift_id, c.absent_staff_id, c.opened_at, c.closed_at FROM cover_case c"
        " JOIN shift s ON s.id = c.shift_id WHERE s.venue_id = %s AND s.starts_at >= %s AND s.starts_at < %s"
        " ORDER BY c.opened_at, c.id",
        (vid, start, end),
    ).fetchall():
        events = conn.execute("SELECT seq, at, actor, kind, data FROM case_event WHERE case_id = %s ORDER BY seq",
                              (case_id,)).fetchall()
        chain = [[seq, at.astimezone(UTC).isoformat(), actor, kind, data] for seq, at, actor, kind, data in events]
        covered = [d for _, _, _, kind, d in events if kind == "shift_covered"]
        local = conn.execute("SELECT starts_at FROM shift WHERE id = %s", (shift_id,)).fetchone()[0].astimezone(tz)
        cases.append({"case": notify.case_ref(conn, case_id), "shift_id": shift_id, "when": f"{local:%Y-%m-%d %H:%M}",
                      "absent": names.get(absent), "status": status,
                      "outcome": (f"covered by {covered[-1].get('name')}" if covered else status),
                      "opened_at": opened.astimezone(UTC).isoformat(),
                      "closed_at": closed.astimezone(UTC).isoformat() if closed else None,
                      "events": len(events), "sha256": sha256_json(chain)})
    incidents = []
    for cid, zone, reason, note, s_at, e_at, remedy, by, created in conn.execute(
        "SELECT id, zone_id, reason, note, starts_at, ends_at, remedy, created_by, created_at FROM closure"
        " WHERE venue_id = %s AND starts_at < %s AND ends_at > %s ORDER BY starts_at, id",
        (vid, end, start),
    ).fetchall():
        record = {"id": cid, "zone": zone, "reason": reason, "note": note, "from": s_at.astimezone(UTC).isoformat(),
                  "to": e_at.astimezone(UTC).isoformat(), "remedy": remedy, "by": by,
                  "created_at": created.astimezone(UTC).isoformat()}
        incidents.append({**record, "when": f"{s_at.astimezone(tz):%Y-%m-%d %H:%M}-{e_at.astimezone(tz):%H:%M}",
                          "sha256": sha256_json(record)})
    period = f"{first:%b} {first.day} - {last:%b} {last.day}, {last.year}" if first != last else f"{first:%b} {first.day}, {first.year}"
    data = {
        "venue": {"id": vid, "name": vname, "tz": tzname},
        "period": {"from": first.isoformat(), "to": last.isoformat(), "label": period},
        "generated_at": now.astimezone(UTC).isoformat(),
        "summary": {"shifts": len(shifts), "violations": violations, "uncovered": uncovered, "caught": caught,
                    "caught_open": caught_open, "cases": len(cases), "incidents": len(incidents),
                    "headline": f"{_n(len(shifts), 'shift')} · {_n(violations, 'violation')}"
                                + (f" · {caught} caught before the shift" if caught else "")
                                + (f" · {caught_open} caught by the nightly check" if caught_open else "")},
        "shifts": shifts, "cases": cases, "incidents": incidents,
        "checks": "Each person is checked again at the start of the shift: certificates valid on that date, "
                  "programs and levels, weekly hours, overlaps, participant norm.",
    }
    # the digest covers the records, not the moment of printing: the same records give the same digest
    data["sha256"] = sha256_json({k: v for k, v in data.items() if k != "generated_at"})
    return data


def _week_of(day: date, week_start: int) -> date:
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def _default_week(conn: psycopg.Connection, venue_id: str, tz: ZoneInfo, week_start: int, now: datetime) -> date:
    """The policy week that holds "now" on the demo clock; if no shift of it is still ahead (Sunday night), the week of
    the next shift, so the folder opened after the night check shows the week it checked, not an empty one."""
    first = _week_of(now.astimezone(tz).date(), week_start)
    week_end = datetime.combine(first + timedelta(days=7), time(0), tzinfo=tz)
    ahead = conn.execute(
        "SELECT min(starts_at) FROM shift WHERE venue_id = %s AND starts_at >= %s AND status IN ('scheduled', 'open')",
        (venue_id, now),
    ).fetchone()[0]
    return _week_of(ahead.astimezone(tz).date(), week_start) if ahead is not None and ahead >= week_end else first


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _guest(conn: psycopg.Connection, staff_id: str):
    policy_id = conn.execute("SELECT policy_id FROM staff WHERE id = %s", (staff_id,)).fetchone()[0]
    return next(s for s in load_staff(conn, policy_id) if s.id == staff_id)


# --- PDF ------------------------------------------------------------------------------------

# the PDF's core font is Latin-1: these become their nearest plain forms (an arrow reads "to", never "->")
_ASCII = {"–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "≤": "<=", "≥": ">="}


def _t(value) -> str:
    text = re.sub(r"\s*→\s*", " to ", "" if value is None else str(value))
    for a, b in _ASCII.items():
        text = text.replace(a, b)
    return text.encode("latin-1", "replace").decode("latin-1")


class _Binder(FPDF):
    def __init__(self, data: dict):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.data = data
        self.set_auto_page_break(True, margin=14)
        self.set_margins(12, 12, 12)

    def footer(self) -> None:
        self.set_y(-10)
        self.set_font("Helvetica", size=7)
        self.set_text_color(110)
        self.cell(0, 5, _t(f"{self.data['venue']['name']} · {self.data['period']['label']} · binder sha256 "
                           f"{self.data['sha256']} · page {self.page_no()}/{{nb}}"), align="C")
        self.set_text_color(0)


def _heading(pdf: FPDF, text: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, _t(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=8)


def _generated(data: dict) -> str:
    """When the binder was made, in the venue's time ("Nov 9, 08:45 ET")."""
    from coachcover.reasons_text import local_stamp

    at = datetime.fromisoformat(data["generated_at"])
    local = at.astimezone(ZoneInfo(data["venue"]["tz"]))
    return f"{local:%b} {local.day}, " + local_stamp(at, data["venue"]["tz"], "%H:%M")


def render(data: dict, compress: bool = True) -> bytes:
    pdf = _Binder(data)
    pdf.set_compression(compress)  # tests read the text uncompressed
    pdf.alias_nb_pages()
    pdf.add_page()
    s = data["summary"]
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 10, _t("Inspection binder"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 6, _t(f"{data['venue']['name']} · {data['period']['label']} · times in {data['venue']['tz']}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    ok = s["violations"] == 0 and s["uncovered"] == 0
    pdf.set_fill_color(*((225, 242, 230) if ok else (250, 225, 222)))
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, _t(f"  {s['headline']}"), fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_fill_color(255, 255, 255)
    pdf.set_font("Helvetica", size=9)
    pdf.cell(0, 6, _t(f"{s['uncovered']} uncovered · {_n(s['cases'], 'replacement')} · {_n(s['incidents'], 'incident')} · "
                      f"generated {_generated(data)}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=8)
    pdf.multi_cell(0, 4.5, _t(data["checks"] + " Hashes: sha256 of the canonical JSON of each case's events and each "
                                               "incident record; the binder digest covers everything in this file."),
                   new_x="LMARGIN", new_y="NEXT")
    _heading(pdf, "Who was on each post")
    head = FontFace(emphasis="BOLD", fill_color=(230, 230, 230))
    zebra = {"cell_fill_color": (246, 246, 246), "cell_fill_mode": "ROWS"}
    bad = FontFace(color=(170, 20, 20), emphasis="BOLD")
    with pdf.table(col_widths=(20, 20, 46, 40, 70, 34, 43), headings_style=head, line_height=4.5,
                   text_align="LEFT", repeat_headings=1, **zebra) as table:
        table.row([_t(x) for x in ("Date", "Time", "Post", "Person", "Cards valid on the day", "Norm", "Result")])
        for r in data["shifts"]:
            row = table.row()
            for value in (r["date"], r["time"], r["post"], r["person"] or "-", "; ".join(r["cards"]) or "-", r["norm"]):
                row.cell(_t(value))
            row.cell(_t(r["result"]), style=None if r["ok"] else bad)
    _heading(pdf, f"Replacements ({len(data['cases'])})")
    if data["cases"]:
        with pdf.table(col_widths=(22, 28, 34, 60, 22, 107), headings_style=head, line_height=4.5,
                       text_align="LEFT", repeat_headings=1, **zebra) as table:
            table.row([_t(x) for x in ("Case", "Shift time", "Absent", "Outcome", "Events", "sha256 of the event file")])
            for c in data["cases"]:
                table.row([_t(x) for x in (c["case"], c["when"], c["absent"] or "-", c["outcome"], c["events"], c["sha256"])])
    else:
        pdf.cell(0, 5, _t("None in this period."), new_x="LMARGIN", new_y="NEXT")
    _heading(pdf, f"Incidents and closures ({len(data['incidents'])})")
    if data["incidents"]:
        with pdf.table(col_widths=(40, 28, 50, 22, 133), headings_style=head, line_height=4.5,
                       text_align="LEFT", repeat_headings=1, **zebra) as table:
            table.row([_t(x) for x in ("When", "Reason", "Note", "Remedy", "sha256 of the record")])
            for i in data["incidents"]:
                table.row([_t(x) for x in (i["when"], i["reason"], i["note"] or "-", i["remedy"], i["sha256"])])
    else:
        pdf.cell(0, 5, _t("None in this period."), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4.5, _t("Prepared by CoachCover from its records. Training data: every name is invented."),
                   new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
