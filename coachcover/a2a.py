"""Borrow a person from another venue of the same network, agent to agent over A2A 1.0 (JSON-RPC binding).

Every venue of a network has its own agent, its own rules (policy) and its own manager. When the borrower
venue has nobody cleared left for an open shift, its agent asks a lender venue's agent over HTTP:

  1. borrower agent: nobody cleared is left here -> `SendMessage` {type: loan.request} to the lender's agent;
  2. lender agent: checks ITS people by ITS rules, and that lending does not leave its own site short
     (`min_guards`: people of the guard role on site or free and cleared during the shift, after the loan);
  3. lender agent: offers its best person (a task in TASK_STATE_INPUT_REQUIRED), with their cards and bookings;
  4. borrower agent: checks that person again by ITS OWN rules (`loan.recheck` accepted or not);
  5. both managers approve, each for their own venue (an agent can never approve for a manager);
  6. borrower agent: `loan.confirm` -> the lender re-checks and records the loan (TASK_STATE_COMPLETED);
     only then the borrower books the person and both venues' records get the entry.

Nothing moves without both approvals. In the demo both venues are training venues on one machine and share
one database; each agent writes only its own side's steps. Protocol: A2A specification 1.0
(https://a2a-protocol.org/latest/specification/), JSON-RPC 2.0 binding, `A2A-Version: 1.0` header.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg.types.json import Jsonb

from coachcover import ext_db, notify
from coachcover.db import insert_policy, install_preset
from coachcover.model import Commitment, Context, GroupRef, Policy, Shift, Staff, StaffCert, StaffProgram, Venue
from coachcover.presets import default_policy
from coachcover.reasons_text import describe, describe_place
from coachcover.repo import load_context, load_policy, load_shift, load_staff
from coachcover.rules import assess, eligible, rank

A2A_VERSION = "1.0"
NETWORK_FILE = Path(__file__).parent / "presets" / "network" / "demo_network.json"
MAX_OFFERS = 3  # offers the borrower checks before giving up on one lender
_EIGHT_DAYS = timedelta(days=8)
_NETWORK_LOCK = 0x65787431

STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_REJECTED = "TASK_STATE_REJECTED"
STATE_CANCELED = "TASK_STATE_CANCELED"
STATE_FAILED = "TASK_STATE_FAILED"

# JSON-RPC error codes (A2A 1.0, section 5.4 and 9.5)
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, UNSUPPORTED_OPERATION, VERSION_NOT_SUPPORTED = -32001, -32004, -32009


class LoanError(Exception):
    status = 409

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


class LoanNotFound(LoanError):
    status = 404


class NotAllowed(LoanError):
    status = 403


class AgentStarting(LoanError):
    status = 503


class RpcError(Exception):
    def __init__(self, code: int, message: str, reason: str | None = None, metadata: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.reason, self.metadata = code, message, reason, metadata or {}


# --- HTTP between agents ------------------------------------------------------------

def _default_client() -> httpx.Client:
    return httpx.Client(timeout=15.0)


HTTP_CLIENT: Callable[[], httpx.Client] = _default_client  # tests swap this for an in-process client


def agent_url(venue_id: str, base_url: str) -> str:
    """The venue agent's JSON-RPC address: COACHCOVER_A2A_PEERS (JSON {venue: url}) or this server."""
    peers = json.loads(os.environ.get("COACHCOVER_A2A_PEERS") or "{}")
    return peers.get(venue_id) or f"{base_url.rstrip('/')}/a2a/{venue_id}"


def public_agent_url(venue_id: str, base_url: str) -> str:
    """The address the Agent Card shows to the world. A peer address that only the compose network resolves (a host
    name without a dot: `api`, `agent-north`) is shown as this server's own `{base_url}/a2a/{venue}`. Calls between
    the agents keep using `agent_url`, the internal address."""
    url = agent_url(venue_id, base_url)
    return url if shown_url(url, base_url) == url else f"{base_url.rstrip('/')}/a2a/{venue_id}"


def shown_url(url: str, base_url: str) -> str:
    """An agent address as people see it: one the compose network alone resolves (a host name without a dot) is
    shown on this server's own address, with the same path."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if "." in host or ":" in host:  # a public name or an IP address (IPv6 has colons)
        return url
    return f"{base_url.rstrip('/')}{parts.path}"


def send(url: str, method: str, params: dict) -> dict:
    """One JSON-RPC call to another agent. Returns `result`; a JSON-RPC error raises RpcError."""
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    with HTTP_CLIENT() as client:
        resp = client.post(url, json=body, headers={"A2A-Version": A2A_VERSION, "Content-Type": "application/json"})
    if resp.status_code != 200:
        raise RpcError(INTERNAL_ERROR, f"agent at {url} answered HTTP {resp.status_code}")
    payload = resp.json()
    if "error" in payload:
        err = payload["error"]
        raise RpcError(err.get("code", INTERNAL_ERROR), err.get("message", "error"))
    return payload["result"]


# --- wire objects (A2A 1.0: camelCase, enum names, member name as the part discriminator) ----------------

HOP = timedelta(seconds=1)  # each message happens after the one it answers, on the shared demo clock


def later(at: datetime, stamp: str | None) -> datetime:
    """Causal order between two agents: an event after a message is at least one second after the message's time
    (the lender's clock may lag the borrower's scenario time)."""
    if not stamp:
        return at
    other = datetime.fromisoformat(stamp) + HOP
    return other if other > at else at


def wait_for_lenders(conn: psycopg.Connection, borrower_venue: str, base_url: str, timeout: float | None = None) -> None:
    """Before a network loan: every lending venue's agent answers its agent card, waiting up to `timeout` seconds
    (COACHCOVER_AGENT_WAIT_SECONDS, default 10) for an agent that is still starting."""
    ensure_network(conn)
    if timeout is None:
        timeout = float(os.environ.get("COACHCOVER_AGENT_WAIT_SECONDS", "10"))
    borrower = member(conn, borrower_venue)
    deadline = monotonic() + timeout
    for m in members(conn).values():
        if m.network_id != borrower.network_id or m.venue_id == borrower.venue_id or not m.lends:
            continue
        card = agent_url(m.venue_id, base_url).split("/a2a/")[0] + "/.well-known/agent-card.json"
        while True:
            try:
                with HTTP_CLIENT() as client:
                    if client.get(card).status_code == 200:
                        break
            except httpx.HTTPError:
                pass
            if monotonic() >= deadline:
                raise AgentStarting(f"{m.short()} agent is starting, try again in a few seconds", {"venue": m.venue_id})
            sleep(0.5)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_part(text: str) -> dict:
    return {"text": text}


def data_part(data: dict) -> dict:
    return {"data": data, "mediaType": "application/json"}


def message(role: str, parts: list[dict], context_id: str, task_id: str | None = None) -> dict:
    msg = {"messageId": str(uuid.uuid4()), "role": role, "parts": parts, "contextId": context_id}
    if task_id:
        msg["taskId"] = task_id
    return msg


def message_data(msg: dict | None) -> dict | None:
    """The first JSON data part of a message."""
    for part in (msg or {}).get("parts") or []:
        if isinstance(part, dict) and isinstance(part.get("data"), dict):
            return part["data"]
    return None


def task_json(row: tuple) -> dict:
    task_id, context_id, state, status_message, artifacts, history, updated_at = row
    status = {"state": state, "timestamp": iso_z(updated_at)}
    if status_message:
        status["message"] = status_message
    return {"id": task_id, "contextId": context_id, "status": status, "artifacts": artifacts, "history": history}


def rpc_result(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Any, error: RpcError) -> dict:
    data = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": error.reason or "ERROR",
             "domain": "coachcover", "metadata": {k: str(v) for k, v in error.metadata.items()}}]
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": error.code, "message": error.message, "data": data}}


def _make(cls, **values):
    """A dataclass from values, dropping fields this version of the type does not have."""
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in values.items() if k in names})


def shift_wire(shift: Shift, venue: Venue, venue_name: str, label: str) -> dict:
    return {
        "id": shift.id, "label": label, "role": shift.role, "start": shift.start.isoformat(), "end": shift.end.isoformat(),
        "venue": {"id": venue.id, "name": venue_name, "tz": venue.tz, "lat": venue.lat, "lon": venue.lon},
        "groups": [{"id": g.id, "program": g.program, "level": g.level, "size": g.size,
                    "age_min": getattr(g, "age_min", None)} for g in shift.groups],
        "instructors": shift.instructors, "extra_certs": list(getattr(shift, "extra_certs", ())),
    }


def shift_from_wire(data: dict) -> tuple[Shift, Venue]:
    v = data["venue"]
    venue = Venue(v["id"], v["tz"], v.get("lat"), v.get("lon"))
    groups = tuple(_make(GroupRef, id=g["id"], program=g["program"], level=str(g["level"]), size=int(g["size"]),
                         age_min=g.get("age_min")) for g in data.get("groups") or [])
    shift = _make(Shift, id=data["id"], venue=venue.id, role=data["role"], start=datetime.fromisoformat(data["start"]),
                  end=datetime.fromisoformat(data["end"]), groups=groups, instructors=int(data.get("instructors") or 1),
                  extra_certs=tuple(data.get("extra_certs") or ()))
    return shift, venue


def staff_wire(person: Staff, bookings: list[Commitment]) -> dict:
    return {
        "id": person.id, "name": person.name, "roles": sorted(person.roles), "home_venue": person.home_venue,
        "certs": [{"type": c.cert_type, "issued_on": c.issued_on.isoformat(),
                   "expires_on": c.expires_on.isoformat() if c.expires_on else None, "needs_review": c.needs_review}
                  for c in person.certs],
        "programs": {code: {"renewed_at": p.renewed_at.isoformat(),
                            "levels": {lv: since.isoformat() for lv, since in p.levels.items()}}
                     for code, p in person.programs.items()},
        "bookings": [{"ref": c.ref, "start": c.start.isoformat(), "end": c.end.isoformat()} for c in bookings],
    }


def staff_from_wire(data: dict) -> tuple[Staff, list[Commitment]]:
    certs = tuple(StaffCert(c["type"], date.fromisoformat(c["issued_on"]),
                            date.fromisoformat(c["expires_on"]) if c.get("expires_on") else None,
                            bool(c.get("needs_review"))) for c in data.get("certs") or [])
    programs = {code: StaffProgram(code, date.fromisoformat(p["renewed_at"]),
                                   {lv: date.fromisoformat(s) for lv, s in p["levels"].items()})
                for code, p in (data.get("programs") or {}).items()}
    person = Staff(data["id"], data["name"], frozenset(data.get("roles") or ()), certs, programs, data.get("home_venue"))
    bookings = [Commitment(b["ref"], "shift", datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
                for b in data.get("bookings") or []]
    return person, bookings


# --- the network --------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    venue_id: str
    venue_name: str
    tz: str
    lat: float | None
    lon: float | None
    network_id: str
    policy_id: str
    manager_name: str
    guard_role: str
    min_guards: int
    lends: bool

    @property
    def venue(self) -> Venue:
        return Venue(self.venue_id, self.tz, self.lat, self.lon)

    def short(self) -> str:
        return self.venue_name.split()[0]


@lru_cache(maxsize=1)
def network_data() -> dict:
    return json.loads(NETWORK_FILE.read_text(encoding="utf-8"))


def _local(value: str, tz: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz))


def _install_member(conn: psycopg.Connection, m: dict) -> None:
    """Create a member's own policy, venue, people and schedule from the network file."""
    p, v = m["policy"], m["venue"]
    install_preset(conn, p["preset"])
    policy = default_policy(p["preset"], **p.get("overrides", {}))
    insert_policy(conn, m["policy_id"], p["name"], policy)
    conn.execute("INSERT INTO venue (id, policy_id, name, tz, lat, lon) VALUES (%s, %s, %s, %s, %s, %s)",
                 (m["venue_id"], m["policy_id"], v["name"], v["tz"], v.get("lat"), v.get("lon")))
    for zone_id, zone_name in v.get("zones", []):
        conn.execute("INSERT INTO zone (id, venue_id, name) VALUES (%s, %s, %s)", (zone_id, m["venue_id"], zone_name))
    preset = p["preset"]
    role_ids = dict(conn.execute("SELECT code, id FROM role WHERE preset_code = %s", (preset,)).fetchall())
    cert_ids = dict(conn.execute("SELECT code, id FROM cert_type WHERE preset_code = %s", (preset,)).fetchall())
    for s in m["staff"]:
        conn.execute("INSERT INTO staff (id, policy_id, full_name, language, home_venue_id) VALUES (%s, %s, %s, %s, %s)",
                     (s["id"], m["policy_id"], s["name"], s.get("language", "en"), m["venue_id"]))
        for role in s["roles"]:
            conn.execute("INSERT INTO staff_role (staff_id, role_id) VALUES (%s, %s)", (s["id"], role_ids[role]))
        for ctype, issued, expires in s["certs"]:
            conn.execute(
                "INSERT INTO staff_cert (staff_id, cert_type_id, card_number, issued_on, expires_on, verified_by_human,"
                " verified_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (s["id"], cert_ids[ctype], f"{ctype.upper()}-{s['id']}", date.fromisoformat(issued),
                 date.fromisoformat(expires), f"manager:{m['venue_id']}",
                 datetime.combine(date.fromisoformat(issued), time(12, 0), tzinfo=UTC)),
            )
        for prog in [*([s["program"]] if s.get("program") else []), *s.get("programs", [])]:
            program_id = conn.execute("SELECT id FROM program WHERE preset_code = %s AND code = %s",
                                      (preset, prog["code"])).fetchone()[0]
            sp_id = conn.execute(
                "INSERT INTO staff_program (staff_id, program_id, renewed_at) VALUES (%s, %s, %s) RETURNING id",
                (s["id"], program_id, date.fromisoformat(prog["renewed_at"])),
            ).fetchone()[0]
            for lv, since in prog["levels"].items():
                conn.execute(
                    "INSERT INTO staff_program_level (staff_program_id, program_id, level_id, since)"
                    " VALUES (%s, %s, (SELECT id FROM program_level WHERE program_id = %s AND code = %s), %s)",
                    (sp_id, program_id, program_id, lv, date.fromisoformat(since)),
                )
    for sh in m.get("shifts", []):
        conn.execute(
            "INSERT INTO shift (id, venue_id, zone_id, role_id, starts_at, ends_at, staff_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (sh["id"], m["venue_id"], sh.get("zone"), role_ids[sh["role"]], _local(sh["start"], v["tz"]),
             _local(sh["end"], v["tz"]), sh.get("staff_id")),
        )


def ensure_network(conn: psycopg.Connection) -> bool:
    """Install the training network once (after a demo reset too). Returns True when it was installed now."""
    ext_db.ensure_schema(conn)
    data = network_data()
    net = data["network"]
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_NETWORK_LOCK,))
        if conn.execute("SELECT 1 FROM ext_network WHERE id = %s", (net["id"],)).fetchone():
            return False
        conn.execute("INSERT INTO ext_network (id, name) VALUES (%s, %s)", (net["id"], net["name"]))
        for m in data["members"]:
            if "policy" in m and not conn.execute("SELECT 1 FROM policy WHERE id = %s", (m["policy_id"],)).fetchone():
                _install_member(conn, m)
            if not conn.execute("SELECT 1 FROM venue WHERE id = %s AND policy_id = %s",
                                (m["venue_id"], m["policy_id"])).fetchone():
                continue  # the venue is not loaded in this database
            conn.execute(
                "INSERT INTO ext_network_member (venue_id, network_id, policy_id, manager_name, guard_role, min_guards)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (m["venue_id"], net["id"], m["policy_id"], m["manager_name"], data["guard_role"], m.get("min_guards", 2)),
            )
    return True


def members(conn: psycopg.Connection) -> dict[str, Member]:
    rows = conn.execute(
        "SELECT m.venue_id, v.name, v.tz, v.lat, v.lon, m.network_id, m.policy_id, m.manager_name, m.guard_role,"
        " m.min_guards, m.lends FROM ext_network_member m JOIN venue v ON v.id = m.venue_id ORDER BY m.venue_id"
    ).fetchall()
    return {r[0]: Member(*r) for r in rows}


def member(conn: psycopg.Connection, venue_id: str) -> Member:
    found = members(conn).get(venue_id)
    if found is None:
        raise LoanNotFound(f"venue {venue_id!r} is not in a network")
    return found


def network_summary(conn: psycopg.Connection, base_url: str) -> dict:
    ms = members(conn)
    nets = dict(conn.execute("SELECT id, name FROM ext_network").fetchall())
    return {
        "networks": [{"id": nid, "name": name} for nid, name in nets.items()],
        "members": [{"venue_id": m.venue_id, "venue_name": m.venue_name, "network_id": m.network_id,
                     "manager": m.manager_name, "min_guards": m.min_guards, "lends": m.lends,
                     "agent_url": public_agent_url(m.venue_id, base_url)} for m in ms.values()],
        "agent_card": f"{base_url.rstrip('/')}/.well-known/agent-card.json",
        "protocol_version": A2A_VERSION,
    }


# --- the Agent Card (one for the network, a skill per venue) ---------------------------

def agent_card(conn: psycopg.Connection, base_url: str) -> dict:
    ms = members(conn)
    net = conn.execute("SELECT name FROM ext_network ORDER BY id LIMIT 1").fetchone()
    name = net[0] if net else "CoachCover network"
    skills = []
    for m in ms.values():
        if not m.lends:
            continue
        example = {"type": "loan.request", "loan": "LN-1109-01", "shift": {"id": "...", "role": "...", "start": "...",
                                                                            "end": "...", "venue": {"id": "..."}}}
        skills.append({
            "id": f"staff-loan.{m.venue_id}",
            "name": f"{m.venue_name}: lend a cleared person",
            "description": (
                f"The agent of {m.venue_name} checks its own people by its own rules for another venue's shift, "
                f"keeps at least {m.min_guards} people of its guard role on site or free and cleared, and offers one "
                f"person. Both managers approve in CoachCover; an agent cannot approve for a manager. "
                f"JSON-RPC endpoint: {public_agent_url(m.venue_id, base_url)}"
            ),
            "tags": ["staffing", "staff-loan", "eligibility", m.venue_id],
            "examples": [json.dumps(example)],
            "inputModes": ["application/json"],
            "outputModes": ["application/json", "text/plain"],
        })
    return {
        "name": f"CoachCover agents · {name}",
        "description": ("One agent per venue of the network. Each venue keeps its own rules and its own manager. "
                        "Training venues on one machine; no real staff data."),
        "supportedInterfaces": [
            {"url": public_agent_url(m.venue_id, base_url), "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}
            for m in ms.values()
        ],
        "provider": {"organization": "CoachCover (training network)", "url": base_url.rstrip("/")},
        "version": "0.1.0",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
    }


# --- shared checks --------------------------------------------------------------------

def _bookings(conn: psycopg.Connection, staff_ids: list[str], around: Shift) -> dict[str, list[Commitment]]:
    """Scheduled shifts of these people at ANY venue near the shift (a loaned person works elsewhere too)."""
    out: dict[str, list[Commitment]] = {sid: [] for sid in staff_ids}
    if not staff_ids:
        return out
    for sid, ref, start, end in conn.execute(
        "SELECT staff_id, id, starts_at, ends_at FROM shift WHERE staff_id = ANY(%s) AND status = 'scheduled'"
        " AND starts_at < %s AND ends_at > %s ORDER BY staff_id, starts_at, id",
        (staff_ids, around.end.astimezone(UTC) + _EIGHT_DAYS, around.start.astimezone(UTC) - _EIGHT_DAYS),
    ):
        out[sid].append(Commitment(ref, "shift", start, end))
    return out



def _venues_of(conn: psycopg.Connection, policy_id: str) -> dict[str, Venue]:
    return {vid: Venue(vid, tz, lat, lon) for vid, tz, lat, lon in
            conn.execute("SELECT id, tz, lat, lon FROM venue WHERE policy_id = %s", (policy_id,))}


def _reserve_roles(policy: Policy) -> set[str]:
    return {r.code for r in policy.preset.roles.values() if r.reserve}


def guard_check(conn: psycopg.Connection, m: Member, policy: Policy, people: list[Staff], context: Context,
                start: datetime, end: datetime, candidate_id: str | None, at: datetime) -> dict:
    """People of the guard role on site at the member's venue, plus free and cleared ones, before and after a loan."""
    tz = ZoneInfo(m.tz)
    window = f"{start.astimezone(tz):%H:%M}–{end.astimezone(tz):%H:%M}"
    if m.guard_role not in policy.preset.roles:
        return {"ok": True, "skipped": f"role {m.guard_role!r} is not in the rules", "minimum": m.min_guards}
    on_site = sorted({sid for (sid,) in conn.execute(
        "SELECT DISTINCT s.staff_id FROM shift s JOIN role r ON r.id = s.role_id WHERE s.venue_id = %s AND r.code = %s"
        " AND s.status = 'scheduled' AND s.starts_at < %s AND s.ends_at > %s",
        (m.venue_id, m.guard_role, end, start),
    )})
    probe = Shift(id=f"probe-{m.venue_id}", venue=m.venue_id, role=m.guard_role, start=start, end=end)
    free = [s.id for s in people
            if s.id not in on_site and m.guard_role in s.roles and eligible(s, probe, at, policy, context).ok]
    total = len(on_site) + len(free)
    after = total - (1 if candidate_id and (candidate_id in on_site or candidate_id in free) else 0)
    return {"ok": after >= m.min_guards, "window": window, "on_site": on_site, "free_cleared": free,
            "before": total, "after": after, "minimum": m.min_guards}


# --- the lender's agent -----------------------------------------------------------------

def _loan_row(conn: psycopg.Connection, ref: str | int, lock: bool = False) -> tuple:
    text = str(ref)
    row = conn.execute(
        "SELECT id, code, network_id, borrower_venue, lender_venue, shift_id, case_id, staff_id, status,"
        " a2a_context_id, a2a_task_id, borrower_approved_by, borrower_approved_at, lender_approved_by,"
        " lender_approved_at, created_at, closed_at, offer FROM ext_loan WHERE code = %s OR id::text = %s"
        + (" FOR UPDATE" if lock else ""),
        (text, text),
    ).fetchone()
    if row is None:
        raise LoanNotFound(f"unknown loan {ref}")
    return row


_LOAN_KEYS = ("id", "code", "network_id", "borrower_venue", "lender_venue", "shift_id", "case_id", "staff_id", "status",
              "context_id", "task_id", "borrower_approved_by", "borrower_approved_at", "lender_approved_by",
              "lender_approved_at", "created_at", "closed_at", "offer")


def loan_dict(conn: psycopg.Connection, ref: str | int, lock: bool = False) -> dict:
    return dict(zip(_LOAN_KEYS, _loan_row(conn, ref, lock)))


def step(conn: psycopg.Connection, loan_id: int, venue_id: str, actor: str, kind: str, data: dict, at: datetime) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ext_loan:{loan_id}",))
        seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM ext_loan_step WHERE loan_id = %s", (loan_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO ext_loan_step (loan_id, seq, at, venue_id, actor, kind, data) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (loan_id, seq, at, venue_id, actor, kind, Jsonb(_plain(data))),
        )


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, (date, datetime)) else str(v)))


def _save_task(conn: psycopg.Connection, task_id: str, context_id: str, venue_id: str, loan_id: int | None, state: str,
               status_message: dict, history: list, artifacts: list, at: datetime) -> dict:
    conn.execute(
        "INSERT INTO ext_a2a_task (id, context_id, venue_id, loan_id, state, status_message, artifacts, history, updated_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET state = excluded.state,"
        " status_message = excluded.status_message, artifacts = excluded.artifacts, history = excluded.history,"
        " updated_at = excluded.updated_at",
        (task_id, context_id, venue_id, loan_id, state, Jsonb(status_message), Jsonb(artifacts), Jsonb(history), at),
    )
    return get_task(conn, venue_id, task_id)


def get_task(conn: psycopg.Connection, venue_id: str, task_id: str) -> dict:
    row = conn.execute(
        "SELECT id, context_id, state, status_message, artifacts, history, updated_at FROM ext_a2a_task"
        " WHERE id = %s AND venue_id = %s",
        (task_id, venue_id),
    ).fetchone()
    if row is None:
        raise RpcError(TASK_NOT_FOUND, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})
    return task_json(row)


def _lender_view(conn: psycopg.Connection, m: Member, shift: Shift, venue: Venue, at: datetime):
    """The lender's own people, ranked for the borrower's shift by the lender's own rules."""
    policy = load_policy(conn, m.policy_id)
    staff = load_staff(conn, m.policy_id)
    venues = {**_venues_of(conn, m.policy_id), venue.id: venue}
    for v in members(conn).values():
        venues.setdefault(v.venue_id, v.venue)
    bookings = _bookings(conn, [s.id for s in staff], shift)
    context = Context(venues, {sid: tuple(b) for sid, b in bookings.items()})
    people = [s for s in staff if not (s.roles & _reserve_roles(policy))]
    ranked = rank(assess(s, shift, at, policy, context) for s in people)
    return policy, staff, people, context, bookings, ranked


def lender_request(conn: psycopg.Connection, m: Member, data: dict, context_id: str, request_msg: dict, at: datetime) -> dict:
    loan = loan_dict(conn, data["loan"])
    shift, venue = shift_from_wire(data["shift"])
    exclude = set(data.get("exclude") or [])
    policy, staff, people, context, bookings, ranked = _lender_view(conn, m, shift, venue, at)
    names = {s.id: s.name for s in staff}
    cleared = [r for r in ranked if r.candidate.decision.ok and r.candidate.staff_id not in exclude]
    chips: dict[str, int] = {}
    for r in ranked:
        blocking = [x for x in r.candidate.decision.reasons if x.blocking]
        if blocking:
            chips[blocking[0].code.value] = chips.get(blocking[0].code.value, 0) + 1
    best = cleared[0] if cleared else None
    guards = guard_check(conn, m, policy, staff, Context(context.venues, context.commitments), shift.start, shift.end,
                         best.candidate.staff_id if best else None, at)
    step(conn, loan["id"], m.venue_id, "agent", "checked_own_people", {
        "people": len(people), "cleared": len(cleared), "excluded": sorted(exclude), "blocked_by": chips,
        "rules": {"max_weekly_hours": policy.max_weekly_hours, "policy_id": m.policy_id}, "guards": guards,
    }, at)
    task_id = str(uuid.uuid4())
    if best is None or not guards["ok"]:
        if best is None:
            reason, why = "NO_CLEARED_PERSON", f"{m.venue_name} has nobody cleared by its own rules for this shift"
        else:
            reason = "WOULD_LEAVE_SITE_SHORT"
            why = (f"lending {names[best.candidate.staff_id]} would leave {m.venue_name} with {guards['after']} "
                   f"people of its guard role on site or free {guards['window']}; its minimum is {guards['minimum']}")
        step(conn, loan["id"], m.venue_id, "agent", "declined", {"reason": reason, "why": why, "guards": guards}, at)
        reply = message("ROLE_AGENT", [text_part(f"Declined: {why}."),
                                       data_part({"type": "loan.decline", "reason": reason, "why": why, "guards": guards})],
                        context_id, task_id)
        return _save_task(conn, task_id, context_id, m.venue_id, loan["id"], STATE_REJECTED, reply,
                          [request_msg, reply], [], at)
    person = next(s for s in staff if s.id == best.candidate.staff_id)
    line = describe_place(best, person.name.split()[0], "en", policy.preset)
    offer = {
        "type": "loan.offer", "loan": loan["code"], "from_venue": {"id": m.venue_id, "name": m.venue_name},
        "staff": staff_wire(person, bookings.get(person.id, [])), "line": line,
        "week_hours": best.candidate.week_hours, "guards": guards,
        "checked_by": {"venue": m.venue_id, "rules": m.policy_id, "max_weekly_hours": policy.max_weekly_hours},
    }
    step(conn, loan["id"], m.venue_id, "agent", "offered", {
        "staff_id": person.id, "name": person.name, "line": line, "week_hours": best.candidate.week_hours,
        "guards": guards, "cleared": len(cleared),
    }, at)
    reply = message("ROLE_AGENT", [text_part(f"Offer: {person.name}, cleared by {m.venue_name} rules. {line}."),
                                   data_part(offer)], context_id, task_id)
    return _save_task(conn, task_id, context_id, m.venue_id, loan["id"], STATE_INPUT_REQUIRED, reply,
                      [request_msg, reply], [], at)


def _task_row(conn: psycopg.Connection, venue_id: str, task_id: str) -> tuple:
    row = conn.execute(
        "SELECT state, loan_id, history, status_message FROM ext_a2a_task WHERE id = %s AND venue_id = %s FOR UPDATE",
        (task_id, venue_id),
    ).fetchone()
    if row is None:
        raise RpcError(TASK_NOT_FOUND, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})
    return row


def lender_recheck(conn: psycopg.Connection, m: Member, task_id: str, data: dict, request_msg: dict, at: datetime) -> dict:
    state, loan_id, history, status_message = _task_row(conn, m.venue_id, task_id)
    if state != STATE_INPUT_REQUIRED:
        raise RpcError(INVALID_PARAMS, f"task is {state}", "TASK_NOT_WAITING", {"taskId": task_id})
    context_id = request_msg.get("contextId") or ""
    if data.get("accepted"):
        step(conn, loan_id, m.venue_id, "agent", "borrower_accepted", {"why": data.get("why")}, at)
        reply = message("ROLE_AGENT", [text_part("Noted. Waiting for both managers; nothing moves before that.")],
                        context_id, task_id)
        return _save_task(conn, task_id, context_id, m.venue_id, loan_id, STATE_INPUT_REQUIRED, reply,
                          [*history, request_msg, reply], [], at)
    step(conn, loan_id, m.venue_id, "agent", "offer_withdrawn", {"codes": data.get("codes"), "why": data.get("why")}, at)
    reply = message("ROLE_AGENT", [text_part("Offer withdrawn: the borrower's rules do not clear this person.")],
                    context_id, task_id)
    return _save_task(conn, task_id, context_id, m.venue_id, loan_id, STATE_CANCELED, reply,
                      [*history, request_msg, reply], [], at)


def lender_confirm(conn: psycopg.Connection, m: Member, task_id: str, data: dict, request_msg: dict, at: datetime) -> dict:
    """Both managers approved: check again by the lender's rules, then record the loan on the lender's side."""
    state, loan_id, history, _ = _task_row(conn, m.venue_id, task_id)
    if state != STATE_INPUT_REQUIRED:
        raise RpcError(INVALID_PARAMS, f"task is {state}", "TASK_NOT_WAITING", {"taskId": task_id})
    context_id = request_msg.get("contextId") or ""
    loan = loan_dict(conn, loan_id)
    if not (loan["lender_approved_by"] and loan["borrower_approved_by"]):
        raise RpcError(INVALID_PARAMS, "both managers must approve first", "APPROVALS_MISSING", {"loan": loan["code"]})
    shift, venue = shift_from_wire(data["shift"])
    policy, staff, people, context, bookings, ranked = _lender_view(conn, m, shift, venue, at)
    mine = next((r for r in ranked if r.candidate.staff_id == loan["staff_id"]), None)
    guards = guard_check(conn, m, policy, staff, context, shift.start, shift.end, loan["staff_id"], at)
    if mine is None or not mine.candidate.decision.ok or not guards["ok"]:
        codes = [r.code.value for r in mine.candidate.decision.reasons if r.blocking] if mine else ["UNKNOWN_PERSON"]
        why = "; ".join(describe(r, "en", policy.preset) for r in mine.candidate.decision.reasons if r.blocking) if mine else ""
        if mine is not None and not codes and not guards["ok"]:
            why = f"the loan would leave {guards['after']} on site or free {guards['window']}, minimum {guards['minimum']}"
        step(conn, loan_id, m.venue_id, "agent", "confirm_refused", {"codes": codes, "why": why, "guards": guards}, at)
        reply = message("ROLE_AGENT", [text_part(f"Cannot lend any more: {why}."),
                                       data_part({"type": "loan.refused", "codes": codes, "why": why})], context_id, task_id)
        return _save_task(conn, task_id, context_id, m.venue_id, loan_id, STATE_REJECTED, reply,
                          [*history, request_msg, reply], [], at)
    name = next(s.name for s in staff if s.id == loan["staff_id"])
    record = {"type": "loan.recorded", "loan": loan["code"], "staff": {"id": loan["staff_id"], "name": name},
              "shift": data["shift"], "approved_by": {"borrower": loan["borrower_approved_by"],
                                                       "lender": loan["lender_approved_by"]}}
    step(conn, loan_id, m.venue_id, "agent", "lent", {"staff_id": loan["staff_id"], "name": name, "guards": guards,
                                                       "approved_by": record["approved_by"]}, at)
    tz = ZoneInfo(m.tz)
    borrower_name = data["shift"]["venue"]["name"]
    body = (f"{name} lent to {borrower_name} {shift.start.astimezone(tz):%a %H:%M}–{shift.end.astimezone(tz):%H:%M}, "
            f"loan {loan['code']}, approved by {loan['borrower_approved_by']} and {loan['lender_approved_by']}")
    notify.post_loan(conn, loan_id, m.venue_id, body, f"/api/network/loans/{loan['code']}", at)
    artifact = {"artifactId": str(uuid.uuid4()), "name": "Loan record", "parts": [data_part(record)]}
    reply = message("ROLE_AGENT", [text_part(f"Recorded: {name} is lent for this shift."), data_part(record)],
                    context_id, task_id)
    return _save_task(conn, task_id, context_id, m.venue_id, loan_id, STATE_COMPLETED, reply,
                      [*history, request_msg, reply], [artifact], at)


def handle_rpc(conn: psycopg.Connection, venue_id: str, body: Any, version: str | None, at: datetime) -> dict:
    """One JSON-RPC 2.0 request to a venue's agent. Always returns a JSON-RPC response object."""
    request_id = body.get("id") if isinstance(body, dict) else None
    try:
        if (version or "").strip() not in ("1.0", "1"):
            raise RpcError(VERSION_NOT_SUPPORTED, f"A2A version {version or '0.3 (no header)'} is not supported; send A2A-Version: 1.0",
                           "VERSION_NOT_SUPPORTED", {"supported": A2A_VERSION})
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            raise RpcError(INVALID_REQUEST, "Request payload validation error", "INVALID_REQUEST")
        m = members(conn).get(venue_id)
        if m is None:
            raise RpcError(INVALID_PARAMS, f"no agent for venue {venue_id}", "UNKNOWN_VENUE", {"venue": venue_id})
        params = body.get("params") or {}
        method = body["method"]
        if method == "GetTask":
            if not params.get("id"):
                raise RpcError(INVALID_PARAMS, "Invalid parameters: id is required", "MISSING_ID")
            return rpc_result(request_id, get_task(conn, venue_id, params["id"]))
        if method != "SendMessage":
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method} (A2A 1.0 names: SendMessage, GetTask)",
                           "METHOD_NOT_FOUND", {"method": method})
        msg = params.get("message")
        if not isinstance(msg, dict) or not msg.get("messageId") or msg.get("role") != "ROLE_USER" or not msg.get("parts"):
            raise RpcError(INVALID_PARAMS, "Invalid parameters: message needs messageId, role ROLE_USER and parts",
                           "INVALID_MESSAGE")
        data = message_data(msg)
        if data is None:
            raise RpcError(INVALID_PARAMS, "Invalid parameters: send a JSON data part with a 'type'", "NO_DATA_PART")
        kind = data.get("type")
        at = later(at, data.get("at") if isinstance(data.get("at"), str) else None)
        context_id = msg.get("contextId") or str(uuid.uuid4())
        if kind in ("loan.approve", "loan.approval"):
            raise RpcError(UNSUPPORTED_OPERATION,
                           "An agent cannot approve for a manager. Each venue's manager approves in CoachCover.",
                           "MANAGER_APPROVAL_REQUIRED", {"venue": venue_id, "manager": m.manager_name})
        if kind == "loan.request":
            task = lender_request(conn, m, data, context_id, msg, at)
        elif kind in ("loan.recheck", "loan.confirm"):
            if not msg.get("taskId"):
                raise RpcError(INVALID_PARAMS, "Invalid parameters: taskId is required", "MISSING_TASK")
            with conn.transaction():
                handler = lender_recheck if kind == "loan.recheck" else lender_confirm
                task = handler(conn, m, msg["taskId"], data, msg, at)
        else:
            raise RpcError(INVALID_PARAMS, f"Invalid parameters: unknown type {kind!r}", "UNKNOWN_TYPE")
        return rpc_result(request_id, {"task": task})
    except RpcError as exc:
        return rpc_error(request_id, exc)
    except LoanError as exc:
        return rpc_error(request_id, RpcError(INVALID_PARAMS, str(exc), "LOAN_ERROR"))


# --- the borrower's agent -------------------------------------------------------------------

def _shift_label(conn: psycopg.Connection, shift_id: str) -> tuple[str, str, Venue, str]:
    row = conn.execute(
        "SELECT v.id, v.name, v.tz, v.lat, v.lon, r.label, s.starts_at, s.ends_at FROM shift s"
        " JOIN venue v ON v.id = s.venue_id JOIN role r ON r.id = s.role_id WHERE s.id = %s",
        (shift_id,),
    ).fetchone()
    vid, vname, tz, lat, lon, role_label, start, end = row
    z = ZoneInfo(tz)
    time_label = f"{start.astimezone(z):%a %H:%M}–{end.astimezone(z):%H:%M}"
    return f"{time_label} · {role_label} · {vname}", time_label, Venue(vid, tz, lat, lon), vname


def _case_event(conn: psycopg.Connection, case_id: int | None, at: datetime, actor: str, kind: str, data: dict) -> None:
    if case_id is None:
        return
    with conn.transaction():
        conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (case_id,))
        seq = conn.execute("SELECT coalesce(max(seq), 0) + 1 FROM case_event WHERE case_id = %s", (case_id,)).fetchone()[0]
        conn.execute("INSERT INTO case_event (case_id, seq, at, actor, kind, data) VALUES (%s, %s, %s, %s, %s, %s)",
                     (case_id, seq, at, actor, kind, Jsonb(_plain(data))))


def own_queue_left(conn: psycopg.Connection, policy_id: str, shift: Shift, case_id: int | None, at: datetime) -> dict:
    """The borrower's own cleared people not asked yet on this case. A loan is asked only when none are left."""
    policy = load_policy(conn, policy_id)
    staff = load_staff(conn, policy_id)
    context = load_context(conn, policy_id, shift)
    people = [s for s in staff if not (s.roles & _reserve_roles(policy))]
    cleared = [s.id for s in people if eligible(s, shift, at, policy, context).ok]
    asked = {sid for (sid,) in conn.execute("SELECT staff_id FROM offer WHERE case_id = %s", (case_id,))} if case_id else set()
    return {"cleared": len(cleared), "asked": len(asked & set(cleared)), "left": [s for s in cleared if s not in asked]}


def recheck(conn: psycopg.Connection, policy_id: str, shift: Shift, offer: dict, lender: Member, at: datetime) -> dict:
    """The borrower checks the offered person by its OWN rules, with the cards and bookings the lender sent."""
    policy = load_policy(conn, policy_id)
    person, bookings = staff_from_wire(offer["staff"])
    venues = {**_venues_of(conn, policy_id), lender.venue_id: lender.venue}
    own = _bookings(conn, [person.id], shift)[person.id]
    seen = {b.ref for b in bookings}
    context = Context(venues, {person.id: tuple(bookings + [b for b in own if b.ref not in seen])})
    decision = eligible(person, shift, at, policy, context)
    blocking = [r for r in decision.reasons if r.blocking]
    return {"ok": decision.ok, "codes": [r.code.value for r in blocking],
            "why": "; ".join(describe(r, "en", policy.preset) for r in blocking) or "cleared by these rules",
            "warnings": [describe(r, "en", policy.preset) for r in decision.reasons if not r.blocking],
            "max_weekly_hours": policy.max_weekly_hours}


def _new_code(conn: psycopg.Connection, tz: str, at: datetime) -> str:
    prefix = f"LN-{at.astimezone(ZoneInfo(tz)):%m%d}-"
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (prefix,))
    n = conn.execute("SELECT count(*) + 1 FROM ext_loan WHERE code LIKE %s", (prefix + "%",)).fetchone()[0]
    return f"{prefix}{n:02d}"


def start_loan(conn: psycopg.Connection, policy_id: str, shift_id: str, at: datetime, base_url: str) -> dict:
    """The borrower's agent asks the network for a person for an open shift with nobody cleared left."""
    ensure_network(conn)
    shift = load_shift(conn, shift_id)
    borrower = member(conn, shift.venue)
    if borrower.policy_id != policy_id:
        raise NotAllowed(f"shift {shift_id} belongs to another venue's rules")
    status = conn.execute("SELECT status FROM shift WHERE id = %s", (shift_id,)).fetchone()[0]
    if status != "open":
        raise LoanError(f"shift {shift_id} is {status}; only an open shift is borrowed for", {"status": status})
    case = conn.execute("SELECT id FROM cover_case WHERE shift_id = %s AND status = 'open'", (shift_id,)).fetchone()
    case_id = case[0] if case else None
    queue = own_queue_left(conn, policy_id, shift, case_id, at)
    if queue["left"]:
        raise LoanError(f"{len(queue['left'])} cleared people at {borrower.venue_name} have not been asked yet",
                        {"left": queue["left"]})
    label, time_label, venue, venue_name = _shift_label(conn, shift_id)
    wire = shift_wire(shift, venue, venue_name, label)
    lenders = sorted((m for m in members(conn).values()
                      if m.network_id == borrower.network_id and m.venue_id != borrower.venue_id and m.lends),
                     key=lambda m: (_km(borrower, m), m.venue_id))
    context_id = str(uuid.uuid4())
    try:
        with conn.transaction():
            code = _new_code(conn, borrower.tz, at)
            loan_id = conn.execute(
                "INSERT INTO ext_loan (code, network_id, borrower_venue, shift_id, case_id, status, a2a_context_id, created_at)"
                " VALUES (%s, %s, %s, %s, %s, 'asking', %s, %s) RETURNING id",
                (code, borrower.network_id, borrower.venue_id, shift_id, case_id, context_id, at),
            ).fetchone()[0]
    except psycopg.errors.UniqueViolation:
        live = conn.execute("SELECT code FROM ext_loan WHERE shift_id = %s AND status IN ('asking', 'awaiting_approval')",
                            (shift_id,)).fetchone()
        raise LoanError(f"shift {shift_id} already has a loan in progress", {"loan": live[0] if live else None}) from None
    step(conn, loan_id, borrower.venue_id, "agent", "asked_network", {
        "shift_id": shift_id, "label": label, "cleared_here": queue["cleared"], "asked_here": queue["asked"],
        "lenders": [m.venue_id for m in lenders],
    }, at)
    _case_event(conn, case_id, at, "agent", "network_loan_requested", {"loan": code, "lenders": [m.venue_id for m in lenders]})
    rejected_any = False
    for lender in lenders:
        exclude: list[str] = []
        url = agent_url(lender.venue_id, base_url)
        for _ in range(MAX_OFFERS):
            request = {"type": "loan.request", "loan": code, "shift": wire, "exclude": exclude,
                       "from_venue": {"id": borrower.venue_id, "name": borrower.venue_name}, "at": iso_z(at)}
            msg = message("ROLE_USER", [text_part(f"{borrower.venue_name} needs a cleared person for {label}."),
                                        data_part(request)], context_id)
            step(conn, loan_id, borrower.venue_id, "agent", "request_sent",
                 {"to": lender.venue_id, "url": url, "method": "SendMessage", "exclude": exclude}, at)
            try:
                task = send(url, "SendMessage", {"message": msg})["task"]
            except (RpcError, httpx.HTTPError) as exc:
                step(conn, loan_id, borrower.venue_id, "agent", "lender_unreachable", {"to": lender.venue_id, "error": str(exc)}, at)
                break
            conn.execute("UPDATE ext_loan SET lender_venue = %s, a2a_task_id = %s WHERE id = %s",
                         (lender.venue_id, task["id"], loan_id))
            at = later(at, task["status"].get("timestamp"))
            state = task["status"]["state"]
            reply = message_data(task["status"].get("message")) or {}
            if state != STATE_INPUT_REQUIRED or reply.get("type") != "loan.offer":
                step(conn, loan_id, borrower.venue_id, "agent", "lender_declined",
                     {"from": lender.venue_id, "task_state": state, "reason": reply.get("reason"), "why": reply.get("why")}, at)
                break
            check = recheck(conn, policy_id, shift, reply, lender, at)
            person = reply["staff"]
            step(conn, loan_id, borrower.venue_id, "agent", "checked_offer",
                 {"staff_id": person["id"], "name": person["name"], **check}, at)
            answer = message("ROLE_USER", [data_part({"type": "loan.recheck", "accepted": check["ok"],
                                                      "codes": check["codes"], "why": check["why"], "at": iso_z(at)})],
                             context_id, task["id"])
            try:
                at = later(at, send(url, "SendMessage", {"message": answer})["task"]["status"].get("timestamp"))
            except (RpcError, httpx.HTTPError) as exc:
                step(conn, loan_id, borrower.venue_id, "agent", "lender_unreachable", {"to": lender.venue_id, "error": str(exc)}, at)
                break
            if check["ok"]:
                conn.execute(
                    "UPDATE ext_loan SET status = 'awaiting_approval', staff_id = %s, offer = %s WHERE id = %s",
                    (person["id"], Jsonb(reply), loan_id),
                )
                step(conn, loan_id, borrower.venue_id, "agent", "awaiting_approval",
                     {"staff_id": person["id"], "name": person["name"],
                      "managers": [[borrower.venue_id, borrower.manager_name], [lender.venue_id, lender.manager_name]]}, at)
                _case_event(conn, case_id, at, "agent", "network_loan_offered",
                            {"loan": code, "staff_id": person["id"], "name": person["name"], "from": lender.venue_name})
                return loan_view(conn, code, base_url)
            rejected_any = True
            exclude.append(person["id"])
    final = "rejected" if rejected_any else "declined"
    conn.execute("UPDATE ext_loan SET status = %s, closed_at = %s WHERE id = %s", (final, at, loan_id))
    step(conn, loan_id, borrower.venue_id, "agent", "no_loan", {"status": final}, at)
    _case_event(conn, case_id, at, "agent", "network_loan_closed", {"loan": code, "status": final})
    return loan_view(conn, code, base_url)


def _km(a: Member, b: Member) -> float:
    if a.lat is None or b.lat is None:
        return float("inf")
    return (a.lat - b.lat) ** 2 + (a.lon - b.lon) ** 2


def _norm(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def approve(conn: psycopg.Connection, ref: str | int, manager: str, venue_id: str, at: datetime, base_url: str) -> dict:
    """One manager approves for their own venue. The second approval books the person; one alone moves nothing."""
    with conn.transaction():
        loan = loan_dict(conn, ref, lock=True)
        if venue_id == loan["borrower_venue"]:
            side = "borrower"
        elif venue_id == loan["lender_venue"]:
            side = "lender"
        else:
            raise NotAllowed(f"venue {venue_id} is not a party to loan {loan['code']}")
        m = member(conn, venue_id)
        if _norm(manager) != _norm(m.manager_name):
            raise NotAllowed(f"only {m.manager_name}, the manager of {m.venue_name}, can approve for {m.venue_name}",
                             {"venue": venue_id})
        if loan["status"] != "awaiting_approval":
            raise LoanError(f"loan {loan['code']} is {loan['status']}", {"status": loan["status"]})
        if loan[f"{side}_approved_by"]:
            raise LoanError(f"{m.venue_name} already approved loan {loan['code']}")
        conn.execute(f"UPDATE ext_loan SET {side}_approved_by = %s, {side}_approved_at = %s WHERE id = %s",
                     (m.manager_name, at, loan["id"]))
    step(conn, loan["id"], venue_id, "manager", "approved", {"manager": m.manager_name, "side": side}, at)
    _case_event(conn, loan["case_id"], at, "manager", "network_loan_approved",
                {"loan": loan["code"], "venue": venue_id, "manager": m.manager_name})
    loan = loan_dict(conn, loan["id"])
    if loan["borrower_approved_by"] and loan["lender_approved_by"]:
        _finalize(conn, loan, at, base_url)
    return loan_view(conn, loan["code"], base_url)


def _fail(conn: psycopg.Connection, loan: dict, at: datetime, why: str, data: dict | None = None) -> None:
    conn.execute("UPDATE ext_loan SET status = 'failed', closed_at = %s WHERE id = %s", (at, loan["id"]))
    step(conn, loan["id"], loan["borrower_venue"], "agent", "failed", {"why": why, **(data or {})}, at)
    _case_event(conn, loan["case_id"], at, "agent", "network_loan_closed", {"loan": loan["code"], "status": "failed", "why": why})


def _finalize(conn: psycopg.Connection, loan: dict, at: datetime, base_url: str) -> None:
    borrower, lender = member(conn, loan["borrower_venue"]), member(conn, loan["lender_venue"])
    shift = load_shift(conn, loan["shift_id"])
    check = recheck(conn, borrower.policy_id, shift, loan["offer"], lender, at)
    step(conn, loan["id"], borrower.venue_id, "agent", "checked_again", {"staff_id": loan["staff_id"], **check}, at)
    url = agent_url(lender.venue_id, base_url)
    label, time_label, venue, venue_name = _shift_label(conn, loan["shift_id"])
    if not check["ok"]:
        cancel = message("ROLE_USER", [data_part({"type": "loan.recheck", "accepted": False, "codes": check["codes"],
                                                  "why": check["why"], "at": iso_z(at)})], loan["context_id"], loan["task_id"])
        try:
            send(url, "SendMessage", {"message": cancel})
        except (RpcError, httpx.HTTPError):
            pass  # the loan fails on this side either way; the step below records why
        _fail(conn, loan, at, check["why"], {"codes": check["codes"]})
        return
    confirm = message("ROLE_USER", [data_part({"type": "loan.confirm", "loan": loan["code"],
                                               "shift": shift_wire(shift, venue, venue_name, label), "at": iso_z(at)})],
                      loan["context_id"], loan["task_id"])
    step(conn, loan["id"], borrower.venue_id, "agent", "confirm_sent", {"to": lender.venue_id, "url": url}, at)
    try:
        task = send(url, "SendMessage", {"message": confirm})["task"]
    except (RpcError, httpx.HTTPError) as exc:
        _fail(conn, loan, at, f"lender agent did not confirm: {exc}")
        return
    at = later(at, task["status"].get("timestamp"))
    if task["status"]["state"] != STATE_COMPLETED:
        why = (message_data(task["status"].get("message")) or {}).get("why") or task["status"]["state"]
        _fail(conn, loan, at, f"{lender.venue_name} refused at confirmation: {why}")
        return
    try:
        _book(conn, loan, borrower, lender, at)
    except (LoanError, psycopg.errors.ExclusionViolation) as exc:
        _fail(conn, loan, at, f"booking failed: {exc}")


def _book(conn: psycopg.Connection, loan: dict, borrower: Member, lender: Member, at: datetime) -> None:
    name = loan["offer"]["staff"]["name"]
    with conn.transaction():
        if loan["case_id"]:
            conn.execute("SELECT 1 FROM cover_case WHERE id = %s FOR UPDATE", (loan["case_id"],))
        status = conn.execute("SELECT status FROM shift WHERE id = %s FOR UPDATE", (loan["shift_id"],)).fetchone()[0]
        if status != "open":
            raise LoanError(f"the shift is {status} now")
        if loan["case_id"]:
            case_status, opened_at = conn.execute("SELECT status, opened_at FROM cover_case WHERE id = %s",
                                                  (loan["case_id"],)).fetchone()
            if case_status != "open":
                raise LoanError(f"the case is {case_status} now")
            conn.execute("UPDATE offer SET status = 'withdrawn' WHERE case_id = %s AND status = 'pending'", (loan["case_id"],))
            conn.execute("UPDATE plan_b SET status = 'superseded' WHERE case_id = %s AND status = 'proposed'", (loan["case_id"],))
        conn.execute("UPDATE shift SET staff_id = %s, status = 'scheduled' WHERE id = %s", (loan["staff_id"], loan["shift_id"]))
        if loan["case_id"]:
            conn.execute("UPDATE cover_case SET status = 'covered', closed_at = %s WHERE id = %s", (at, loan["case_id"]))
        conn.execute("UPDATE ext_loan SET status = 'assigned', closed_at = %s WHERE id = %s", (at, loan["id"]))
        label, time_label, _, venue_name = _shift_label(conn, loan["shift_id"])
        language = conn.execute("SELECT language FROM staff WHERE id = %s", (loan["staff_id"],)).fetchone()[0]
        conn.execute(
            "INSERT INTO message (created_at, audience, staff_id, case_id, language, body) VALUES (%s, 'staff', %s, %s, %s, %s)",
            (at, loan["staff_id"], loan["case_id"], language if language in ("en", "es", "ru") else "en",
             f"CoachCover: confirmed, {time_label} at {venue_name} is yours, on loan from {lender.venue_name}. Thank you!"),
        )
    data = {"staff_id": loan["staff_id"], "name": name, "via": "network_loan", "loan_id": loan["id"],
            "loan_code": loan["code"], "lender_venue": lender.venue_id, "lender_venue_name": lender.venue_name,
            "approved_by": {"borrower": loan["borrower_approved_by"], "lender": loan["lender_approved_by"]}}
    if loan["case_id"]:
        data["minutes_to_cover"] = round((at - opened_at).total_seconds() / 60, 1)
    _case_event(conn, loan["case_id"], at, "agent", "shift_covered", data)
    step(conn, loan["id"], borrower.venue_id, "agent", "assigned",
         {"staff_id": loan["staff_id"], "name": name, "shift_id": loan["shift_id"], "label": label,
          "case_ref": notify.case_ref(conn, loan["case_id"]) if loan["case_id"] else None}, at)
    if loan["case_id"]:
        notify.post_case(conn, loan["case_id"], at, loan_id=loan["id"])


# --- the screen ---------------------------------------------------------------------------

STATUS_LABEL = {
    "asking": "Asking the network",
    "declined": "No venue could lend",
    "rejected": "Offered people did not pass our rules",
    "awaiting_approval": "Waiting for both managers",
    "assigned": "Loan approved by both managers",
    "failed": "Loan failed",
}


def _time_label(conn: psycopg.Connection, at: datetime, venue_id: str | None) -> str:
    """A step's moment for people, in that venue's own time ("08:43:40 ET"); `at` stays in UTC for machines."""
    from coachcover.reasons_text import local_stamp

    row = conn.execute("SELECT tz FROM venue WHERE id = %s", (venue_id,)).fetchone() if venue_id else None
    return local_stamp(at, row[0] if row else "UTC")


def _step_text(kind: str, data: dict, venue: str, names: dict[str, str]) -> tuple[str, str]:
    """(title, detail) of a step for the screen."""
    guards = data.get("guards") or {}
    guard_line = (f"{venue} keeps {guards.get('after')} on site or free {guards.get('window')} after the loan"
                  f" (minimum {guards.get('minimum')}; now {guards.get('before')})") if guards.get("window") else ""
    if kind == "asked_network":
        return (f"{venue} agent: no cleared staff left here · asks the network",
                f"{data.get('cleared_here')} cleared here, {data.get('asked_here')} asked · {data.get('label')}")
    if kind == "request_sent":
        target = names.get(data.get("to"), data.get("to"))
        return f"{venue} agent → {target} agent: SendMessage (A2A {A2A_VERSION})", data.get("url", "")
    if kind == "checked_own_people":
        return (f"{venue} agent checked its {data.get('people')} people by {venue} rules: {data.get('cleared')} cleared",
                guard_line)
    if kind == "offered":
        return f"{venue} agent offers {data.get('name')}", f"{data.get('line')} · {guard_line}"
    if kind == "declined":
        return f"{venue} agent declines", data.get("why", "")
    if kind == "checked_offer":
        verdict = f"cleared by {venue} rules" if data.get("ok") else f"blocked by {venue} rules: {data.get('why', '')}"
        return f"{venue} agent re-checked {data.get('name')}", verdict
    if kind == "awaiting_approval":
        return "Waiting for both managers", ", ".join(f"{names.get(v, v)}: {mgr}" for v, mgr in data.get("managers") or [])
    if kind == "borrower_accepted":
        return f"{venue} agent: offer stands, nothing moves before both managers approve", ""
    if kind == "offer_withdrawn":
        return f"{venue} agent withdrew the offer", data.get("why", "")
    if kind == "lender_declined":
        return f"{names.get(data.get('from'), data.get('from'))} declined", data.get("why") or data.get("task_state", "")
    if kind == "approved":
        return f"{data.get('manager')} ({venue} manager) approved", ""
    if kind == "checked_again":
        verdict = f"cleared by {venue} rules" if data.get("ok") else f"blocked by {venue} rules: {data.get('why', '')}"
        return f"{venue} agent checked again before booking", verdict
    if kind == "confirm_sent":
        return f"{venue} agent → {names.get(data.get('to'), data.get('to'))} agent: confirm", data.get("url", "")
    if kind == "lent":
        return f"{venue} agent recorded the loan of {data.get('name')}", guard_line
    if kind == "confirm_refused":
        return f"{venue} agent refused at confirmation", data.get("why", "")
    if kind == "assigned":
        case = f" · case {data['case_ref']}" if data.get("case_ref") else ""
        return f"{data.get('name')} is on the {venue} schedule", f"{data.get('label')}{case}"
    if kind in ("no_loan", "failed", "lender_unreachable"):
        return {"no_loan": "No loan", "failed": "Loan failed", "lender_unreachable": "Other agent unreachable"}[kind], \
            data.get("why") or data.get("error") or data.get("status", "")
    return kind.replace("_", " "), ""


def loan_view(conn: psycopg.Connection, ref: str | int, base_url: str) -> dict:
    loan = loan_dict(conn, ref)
    ms = members(conn)
    names = {v: m.short() for v, m in ms.items()}
    label, time_label, _, _ = _shift_label(conn, loan["shift_id"])

    def side(venue_id: str | None, who: str) -> dict | None:
        if venue_id is None:
            return None
        m = ms[venue_id]
        at = loan[f"{who}_approved_at"]
        return {"venue_id": venue_id, "venue_name": m.venue_name, "manager": m.manager_name,
                "agent_url": public_agent_url(venue_id, base_url),
                "approved": {"by": loan[f"{who}_approved_by"], "at": iso_z(at),
                             "at_label": _time_label(conn, at, venue_id)} if at else None}

    steps = []
    for seq, at, venue_id, actor, kind, data in conn.execute(
        "SELECT seq, at, venue_id, actor, kind, data FROM ext_loan_step WHERE loan_id = %s ORDER BY seq", (loan["id"],)
    ):
        if isinstance(data, dict) and data.get("url"):  # the step keeps the address the call used; people see the public one
            data = {**data, "url": shown_url(data["url"], base_url)}
        title, detail = _step_text(kind, data, names.get(venue_id, venue_id), names)
        steps.append({"seq": seq, "at": iso_z(at), "at_label": _time_label(conn, at, venue_id), "venue_id": venue_id,
                      "venue": names.get(venue_id, venue_id),
                      "side": "borrower" if venue_id == loan["borrower_venue"] else "lender",
                      "actor": actor, "kind": kind, "title": title, "detail": detail, "data": data})
    task = None
    if loan["task_id"] and loan["lender_venue"]:
        row = conn.execute("SELECT state FROM ext_a2a_task WHERE id = %s", (loan["task_id"],)).fetchone()
        task = row[0] if row else None
    offer = loan["offer"] or {}
    candidate = None
    if loan["staff_id"] and offer:
        candidate = {"staff_id": loan["staff_id"], "name": offer["staff"]["name"], "from_venue": offer["from_venue"]["name"],
                     "line": offer.get("line"), "week_hours": offer.get("week_hours")}
    approvals = int(bool(loan["borrower_approved_by"])) + int(bool(loan["lender_approved_by"]))
    case = None
    if loan["case_id"]:
        ref_ = notify.case_ref(conn, loan["case_id"])
        case = {"id": loan["case_id"], "ref": ref_, "link": notify.case_link(ref_)}
    return {
        "id": loan["id"], "code": loan["code"], "status": loan["status"], "status_label": STATUS_LABEL[loan["status"]],
        "network_id": loan["network_id"],
        "shift": {"id": loan["shift_id"], "label": label, "time_label": time_label},
        "case": case,
        "borrower": side(loan["borrower_venue"], "borrower"),
        "lender": side(loan["lender_venue"], "lender"),
        "candidate": candidate,
        "approvals": approvals, "approvals_needed": 2,
        "created_at": iso_z(loan["created_at"]), "closed_at": iso_z(loan["closed_at"]) if loan["closed_at"] else None,
        "a2a": {"protocol_version": A2A_VERSION, "context_id": loan["context_id"], "task_id": loan["task_id"],
                "task_state": task},
        "steps": steps,
        "honesty": "Both venues are training venues on one machine; every name is invented.",
    }
