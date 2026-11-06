"""Small builders for rule tests. Defaults describe an eligible level-3 instructor."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from coverage.model import Commitment, Context, GroupRef, Shift, Staff, StaffCert, StaffProgram, Venue
from coverage.presets import default_policy
from coverage.rules import add_months

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")

SWIM = default_policy("swim_school")  # 40 h, week starts Sunday
FIT = default_policy("fitness_club")  # 40 h, week starts Monday

VENUES = {
    "A": Venue("A", "America/New_York", 33.95, -84.40),
    "B": Venue("B", "America/New_York", 33.80, -84.25),
    "W": Venue("W", "America/Los_Angeles", 34.05, -118.25),
}

FAR_FUTURE = date(2027, 12, 31)


def local(y: int, m: int, d: int, h: int = 0, mi: int = 0, tz: ZoneInfo = NY, fold: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=tz, fold=fold)


AT = local(2026, 11, 9, 5, 12)  # Monday 05:12, the morning call-out


def card(cert_type: str, expires_on: date | None = FAR_FUTURE, issued_on: date | None = None, needs_review: bool = False) -> StaffCert:
    if issued_on is None:
        issued_on = add_months(expires_on, -24) if expires_on else date(2025, 1, 1)
    return StaffCert(cert_type, issued_on, expires_on, needs_review)


def instructor(
    sid: str = "S1",
    *,
    roles: tuple[str, ...] = ("instructor",),
    certs: tuple[StaffCert, ...] | None = None,
    levels: tuple[str, ...] = ("3",),
    renewed_at: date = date(2026, 6, 1),
    since: date = date(2025, 1, 1),
    home: str | None = "A",
    program: str = "learn_to_swim",
) -> Staff:
    if certs is None:
        certs = (card("wsi"), card("cpr_aed"))
    programs = {program: StaffProgram(program, renewed_at, {lv: since for lv in levels})} if levels else {}
    return Staff(sid, f"Person {sid}", frozenset(roles), certs, programs, home)


def lifeguard(sid: str = "L1", *, certs: tuple[StaffCert, ...] | None = None, home: str | None = "A") -> Staff:
    if certs is None:
        certs = (card("lifeguarding"), card("cpr_aed"))
    return Staff(sid, f"Person {sid}", frozenset({"lifeguard"}), certs, {}, home)


def group(gid: str = "G1", level: str = "3", size: int = 6, program: str = "learn_to_swim") -> GroupRef:
    return GroupRef(gid, program, level, size)


def lesson(
    sid: str = "SH1",
    start: datetime = local(2026, 11, 9, 6, 0),
    minutes: int = 30,
    groups: tuple[GroupRef, ...] | None = None,
    venue: str = "A",
    role: str = "instructor",
    instructors: int = 1,
) -> Shift:
    if groups is None:
        groups = (group(),)
    return Shift(sid, venue, role, start, start + timedelta(minutes=minutes), groups, instructors)


def guard_shift(sid: str = "LG1", start: datetime = local(2026, 11, 9, 6, 0), hours: float = 4, venue: str = "A") -> Shift:
    return Shift(sid, venue, "lifeguard", start, start + timedelta(hours=hours))


def booked(ref: str, start: datetime, minutes: int, kind: str = "shift") -> Commitment:
    return Commitment(ref, kind, start, start + timedelta(minutes=minutes))


def ctx(**commitments: list[Commitment]) -> Context:
    return Context(VENUES, {staff_id: tuple(items) for staff_id, items in commitments.items()})

