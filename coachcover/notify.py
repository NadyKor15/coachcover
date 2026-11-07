"""A short report in the team chat after each closed cover: "06:00 covered by Zoe, case C-1109-01".

Delivery is simulated: the post is stored in `ext_team_post` with delivery "simulated" and a log line
says where it would go ("would post to #riverside-staff"). Nothing leaves the machine.

`sweep` posts every closed cover (covered or plan B) that has no post yet; it is safe to run on every
worker tick or request. A staff loan posts on both venues' channels (`post_loan`).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg

log = logging.getLogger("coachcover.notify")

CLOSED = ("covered", "plan_b")


def channel_for(venue_name: str) -> str:
    """'#<first word of the venue name>-staff', e.g. '#riverside-staff'."""
    words = re.findall(r"[a-z0-9]+", venue_name.lower())
    return f"#{words[0] if words else 'team'}-staff"


def _has_column(conn: psycopg.Connection, table: str, column: str) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s)",
        (table, column),
    ).fetchone()[0]


def case_ref(conn: psycopg.Connection, case_id: int) -> str:
    """The case's readable code (C-1109-01) when the schema has one, else C-<number>."""
    if _has_column(conn, "cover_case", "code"):
        code = conn.execute("SELECT code FROM cover_case WHERE id = %s", (case_id,)).fetchone()
        if code and code[0]:
            return code[0]
    return f"C-{case_id}"


def case_link(ref: str) -> str:
    base = os.environ.get("COACHCOVER_PUBLIC_URL", "").rstrip("/")
    return f"{base}/case.html?id={ref}"


def _first(name: str | None) -> str:
    return (name or "someone").split()[0]


def _case_facts(conn: psycopg.Connection, case_id: int) -> dict | None:
    row = conn.execute(
        "SELECT c.status, c.shift_id, s.starts_at, s.venue_id, v.name, v.tz FROM cover_case c"
        " JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id WHERE c.id = %s",
        (case_id,),
    ).fetchone()
    if row is None:
        return None
    status, shift_id, start, venue_id, venue_name, tz = row
    events = conn.execute(
        "SELECT kind, data FROM case_event WHERE case_id = %s ORDER BY seq", (case_id,)
    ).fetchall()
    return {"status": status, "shift_id": shift_id, "start": start.astimezone(ZoneInfo(tz)), "venue_id": venue_id,
            "venue_name": venue_name, "events": events}


def compose(conn: psycopg.Connection, case_id: int) -> dict | None:
    """The post for a closed case, or None while the case is open (or was cancelled)."""
    facts = _case_facts(conn, case_id)
    if facts is None or facts["status"] not in CLOSED:
        return None
    ref = case_ref(conn, case_id)
    time = f"{facts['start']:%H:%M}"
    covered = [data for kind, data in facts["events"] if kind == "shift_covered"]
    approved = [data for kind, data in facts["events"] if kind == "plan_b_approved"]
    families = [data for kind, data in facts["events"] if kind == "families_notified"]
    if covered:
        last = covered[-1]
        what = f"covered by {_first(last.get('name'))}"
        if last.get("via") == "network_loan":
            what = f"covered by {last.get('name')} from {last.get('lender_venue_name')} (network loan {last.get('loan_code')})"
        elif last.get("via") == "plan_b":
            what += " (plan B)"
        elif last.get("overtime"):
            what += " (overtime)"
    elif approved and approved[-1].get("option") == "merge":
        what = "lesson runs together with the group next to it (plan B)"
    elif approved:
        told = sum(int(d.get("families") or 0) for d in families)
        what = "lesson cancelled (plan B)" + (f", {told} families told" if told else "")
    else:
        what = "closed"
    body = f"{time} {what}, case {ref}"
    return {"venue_id": facts["venue_id"], "channel": channel_for(facts["venue_name"]), "body": body,
            "link": case_link(ref), "case_ref": ref}


def _insert(conn: psycopg.Connection, post: dict, now: datetime, *, case_id: int | None, loan_id: int | None) -> dict | None:
    row = conn.execute(
        "INSERT INTO ext_team_post (venue_id, channel, case_id, loan_id, body, link, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
        (post["venue_id"], post["channel"], case_id, loan_id, post["body"], post["link"], now),
    ).fetchone()
    if row is None:
        return None
    log.info("would post to %s: %s %s", post["channel"], post["body"], post["link"])
    return {"id": row[0], **post, "delivery": "simulated", "log": f"would post to {post['channel']}"}


def post_case(conn: psycopg.Connection, case_id: int, now: datetime, *, loan_id: int | None = None) -> dict | None:
    """Post once for a closed case. Returns the new post, or None (still open, or already posted)."""
    post = compose(conn, case_id)
    if post is None:
        return None
    return _insert(conn, post, now, case_id=case_id, loan_id=loan_id)


def post_loan(conn: psycopg.Connection, loan_id: int, venue_id: str, body: str, link: str, now: datetime) -> dict | None:
    """A post about a staff loan on one venue's channel (the lender's side of a loan)."""
    name = conn.execute("SELECT name FROM venue WHERE id = %s", (venue_id,)).fetchone()[0]
    post = {"venue_id": venue_id, "channel": channel_for(name), "body": body, "link": link}
    return _insert(conn, post, now, case_id=None, loan_id=loan_id)


def sweep(conn: psycopg.Connection, policy_id: str, now: datetime) -> list[dict]:
    """Post every closed cover of the policy that has no post yet."""
    ids = [
        case_id
        for (case_id,) in conn.execute(
            "SELECT c.id FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
            " WHERE v.policy_id = %s AND c.status = ANY(%s)"
            " AND NOT EXISTS (SELECT 1 FROM ext_team_post p WHERE p.case_id = c.id) ORDER BY c.closed_at, c.id",
            (policy_id, list(CLOSED)),
        )
    ]
    return [post for post in (post_case(conn, case_id, now) for case_id in ids) if post]


def posts(conn: psycopg.Connection, venue_id: str | None = None, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, p.venue_id, p.channel, p.body, p.link, p.case_id, p.loan_id, p.created_at FROM ext_team_post p"
        " WHERE %s::text IS NULL OR p.venue_id = %s ORDER BY p.created_at DESC, p.id DESC LIMIT %s",
        (venue_id, venue_id, limit),
    ).fetchall()
    out = []
    for pid, venue, channel, body, link, case_id, loan_id, at in rows:
        out.append({"id": pid, "venue_id": venue, "channel": channel, "body": body, "link": link,
                    "case_id": case_id, "loan_id": loan_id, "at": at.isoformat(), "delivery": "simulated",
                    "log": f"would post to {channel}"})
    return out
