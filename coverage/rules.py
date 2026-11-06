"""Who may cover a shift, and in what order. Pure functions: no database, no network.

`eligible` collects every reason at once (it does not stop at the first one).
Each reason is a machine code plus parameters; human text is built separately
(`coverage.reasons_text`).

All interval arithmetic runs in UTC. Python compares and subtracts datetimes
that share one tzinfo by wall clock, which is wrong across a DST change, so
every datetime is converted to UTC before it is compared or subtracted.
Calendar dates (certificate expiry, program renewal, week start) are read in
the venue's time zone.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from coverage.model import (
    CertType,
    Commitment,
    Context,
    GroupRef,
    Level,
    Policy,
    Preset,
    Role,
    Shift,
    Staff,
    StaffCert,
    Venue,
    _require_aware,
)


class Code(StrEnum):
    ROLE_MISSING = "ROLE_MISSING"
    CERT_MISSING = "CERT_MISSING"
    CERT_NOT_YET_VALID = "CERT_NOT_YET_VALID"
    CERT_EXPIRED = "CERT_EXPIRED"
    CERT_EXPIRES_DURING_SHIFT = "CERT_EXPIRES_DURING_SHIFT"
    CERT_NEEDS_REVIEW = "CERT_NEEDS_REVIEW"
    CERT_EXPIRES_ON_SHIFT_DAY = "CERT_EXPIRES_ON_SHIFT_DAY"  # warning
    PROGRAM_MISSING = "PROGRAM_MISSING"
    LEVEL_MISSING = "LEVEL_MISSING"
    PROGRAM_RENEWAL_OVERDUE = "PROGRAM_RENEWAL_OVERDUE"
    HOURS_LIMIT_EXCEEDED = "HOURS_LIMIT_EXCEEDED"
    OVERLAPS_COMMITMENT = "OVERLAPS_COMMITMENT"
    RATIO_EXCEEDED = "RATIO_EXCEEDED"


WARNINGS = frozenset({Code.CERT_EXPIRES_ON_SHIFT_DAY})


@dataclass(frozen=True)
class Reason:
    code: Code
    params: Mapping[str, object] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.code not in WARNINGS

    @property
    def severity(self) -> str:
        return "block" if self.blocking else "warn"


@dataclass(frozen=True)
class Decision:
    ok: bool
    reasons: tuple[Reason, ...]

    def __post_init__(self) -> None:
        if self.ok != (not any(r.blocking for r in self.reasons)):
            raise ValueError("Decision.ok must be True exactly when no reason is blocking")

    @classmethod
    def of(cls, reasons: Iterable[Reason]) -> Decision:
        reasons = tuple(reasons)
        return cls(ok=not any(r.blocking for r in reasons), reasons=reasons)

    @property
    def codes(self) -> list[Code]:
        return [r.code for r in self.reasons]


# --- calendar helpers -------------------------------------------------------


def add_months(day: date, months: int) -> date:
    """Same day `months` later; clamps to the month end (Jan 31 + 1 -> Feb 28/29)."""
    years, month0 = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def cert_expiry(cert: StaffCert, cert_type: CertType) -> date:
    """Last valid day of a card. The printed date wins; otherwise issue date + validity."""
    if cert.expires_on is not None:
        return cert.expires_on
    return add_months(cert.issued_on, cert_type.validity_months)


def week_start_date(day: date, week_start: int) -> date:
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _local_midnight_utc(day: date, tz: ZoneInfo) -> datetime:
    return _utc(datetime.combine(day, time(0), tzinfo=tz))


def _local_day(value: datetime, tz: ZoneInfo) -> date:
    return value.astimezone(tz).date()


def _last_local_day(end: datetime, tz: ZoneInfo) -> date:
    """The last calendar day an interval ending at `end` touches (an end at midnight belongs to the day before)."""
    return (_utc(end) - timedelta(microseconds=1)).astimezone(tz).date()


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> int:
    start, end = max(_utc(a_start), _utc(b_start)), min(_utc(a_end), _utc(b_end))
    return max(0, int((end - start).total_seconds()))


def _hours(seconds: int) -> float:
    return round(seconds / 3600, 2)


# --- context lookups --------------------------------------------------------


def _venue(context: Context, venue_id: str) -> Venue:
    venue = context.venues.get(venue_id)
    if venue is None:
        raise ValueError(f"unknown venue {venue_id!r}")
    return venue


def _role(preset: Preset, shift: Shift) -> Role:
    role = preset.roles.get(shift.role)
    if role is None:
        raise ValueError(f"shift {shift.id}: role {shift.role!r} is not in preset {preset.code}")
    return role


def _level(preset: Preset, group: GroupRef) -> Level:
    program = preset.programs.get(group.program)
    if program is None:
        raise ValueError(f"group {group.id}: program {group.program!r} is not in preset {preset.code}")
    level = program.levels.get(group.level)
    if level is None:
        raise ValueError(f"group {group.id}: level {group.level!r} is not in program {group.program}")
    return level


def _own_commitments(staff: Staff, shift: Shift, context: Context) -> list[Commitment]:
    """The person's bookings, minus this very shift and its groups (no double count, no self-overlap)."""
    skip = {shift.id, *(g.id for g in shift.groups)}
    return [c for c in context.commitments.get(staff.id, ()) if c.ref not in skip]


def _booked_seconds(commitments: Sequence[Commitment], start: datetime, end: datetime) -> int:
    return sum(_overlap_seconds(c.start, c.end, start, end) for c in commitments)


def _week_bounds(week_start: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return _local_midnight_utc(week_start, tz), _local_midnight_utc(week_start + timedelta(days=7), tz)


# --- the checks -------------------------------------------------------------


def _check_role(staff: Staff, shift: Shift) -> list[Reason]:
    if shift.role in staff.roles:
        return []
    return [Reason(Code.ROLE_MISSING, {"role": shift.role})]


def _check_certs(staff: Staff, role: Role, shift: Shift, at: datetime, tz: ZoneInfo, preset: Preset) -> list[Reason]:
    reasons: list[Reason] = []
    start, end = _utc(shift.start), _utc(shift.end)
    last_day = _last_local_day(shift.end, tz)
    today = _local_day(at, tz)
    for code in role.required_certs:
        cert_type = preset.cert_types[code]
        held = [c for c in staff.certs if c.cert_type == code]
        if not held:
            reasons.append(Reason(Code.CERT_MISSING, {"cert_type": code}))
            continue
        # newest card first: the latest expiry decides, the issue date breaks ties
        cards = sorted(((cert_expiry(c, cert_type), c) for c in held), key=lambda p: (p[0], p[1].issued_on), reverse=True)
        covering = [
            (expiry, c)
            for expiry, c in cards
            if _local_midnight_utc(c.issued_on, tz) <= start and end <= _local_midnight_utc(expiry + timedelta(days=1), tz)
        ]
        trusted = [(expiry, c) for expiry, c in covering if not c.needs_review]
        if trusted:
            expiry, _ = trusted[0]
            if expiry == last_day:
                reasons.append(
                    Reason(
                        Code.CERT_EXPIRES_ON_SHIFT_DAY,
                        {"cert_type": code, "expires_on": expiry.isoformat(), "today": expiry == today},
                    )
                )
            continue
        if covering:
            expiry, _ = covering[0]
            reasons.append(Reason(Code.CERT_NEEDS_REVIEW, {"cert_type": code, "expires_on": expiry.isoformat()}))
            continue
        expiry, card = cards[0]
        valid_from = _local_midnight_utc(card.issued_on, tz)
        valid_until = _local_midnight_utc(expiry + timedelta(days=1), tz)
        if start < valid_from:
            reasons.append(
                Reason(Code.CERT_NOT_YET_VALID, {"cert_type": code, "valid_from": card.issued_on.isoformat()})
            )
        elif start >= valid_until:
            reasons.append(Reason(Code.CERT_EXPIRED, {"cert_type": code, "expires_on": expiry.isoformat()}))
        else:
            reasons.append(
                Reason(
                    Code.CERT_EXPIRES_DURING_SHIFT,
                    {
                        "cert_type": code,
                        "expires_on": expiry.isoformat(),
                        "valid_until": valid_until.astimezone(tz).isoformat(),
                    },
                )
            )
    return reasons


def _check_programs(staff: Staff, shift: Shift, tz: ZoneInfo, preset: Preset) -> list[Reason]:
    reasons: list[Reason] = []
    day = _local_day(shift.start, tz)
    seen_levels: set[tuple[str, str]] = set()
    seen_programs: set[str] = set()
    for group in shift.groups:
        _level(preset, group)  # unknown program or level is a data error, not a staff reason
        key = (group.program, group.level)
        if key in seen_levels:
            continue
        seen_levels.add(key)
        held = staff.programs.get(group.program)
        if held is None:
            if group.program not in seen_programs:
                reasons.append(Reason(Code.PROGRAM_MISSING, {"program": group.program}))
                seen_programs.add(group.program)
            continue
        since = held.levels.get(group.level)
        if since is None or since > day:
            reasons.append(Reason(Code.LEVEL_MISSING, {"program": group.program, "level": group.level}))
        if group.program in seen_programs:
            continue
        seen_programs.add(group.program)
        months = preset.programs[group.program].renewal_months
        if months is not None:
            due = add_months(held.renewed_at, months)
            if day > due:
                reasons.append(
                    Reason(
                        Code.PROGRAM_RENEWAL_OVERDUE,
                        {"program": group.program, "renewed_at": held.renewed_at.isoformat(), "due_on": due.isoformat()},
                    )
                )
    return reasons


def _check_hours(own: Sequence[Commitment], shift: Shift, tz: ZoneInfo, policy: Policy) -> list[Reason]:
    """Weekly hours with this shift; a shift that crosses the week boundary counts in each week by its own hours."""
    reasons: list[Reason] = []
    limit = round(policy.max_weekly_hours * 3600)
    week = week_start_date(_local_day(shift.start, tz), policy.week_start)
    last_week = week_start_date(_last_local_day(shift.end, tz), policy.week_start)
    while week <= last_week:
        week_from, week_to = _week_bounds(week, tz)
        before = _booked_seconds(own, week_from, week_to)
        this = _overlap_seconds(shift.start, shift.end, week_from, week_to)
        if before + this > limit:
            reasons.append(
                Reason(
                    Code.HOURS_LIMIT_EXCEEDED,
                    {
                        "week_start": week.isoformat(),
                        "hours_before": _hours(before),
                        "shift_hours": _hours(this),
                        "hours_after": _hours(before + this),
                        "limit_hours": policy.max_weekly_hours,
                    },
                )
            )
        week += timedelta(days=7)
    return reasons


def _check_overlaps(own: Sequence[Commitment], shift: Shift, tz: ZoneInfo) -> list[Reason]:
    start, end = _utc(shift.start), _utc(shift.end)
    reasons = []
    for c in sorted(own, key=lambda c: (_utc(c.start), c.ref)):
        if _utc(c.start) < end and start < _utc(c.end):
            reasons.append(
                Reason(
                    Code.OVERLAPS_COMMITMENT,
                    {
                        "kind": c.kind,
                        "ref": c.ref,
                        "start": c.start.astimezone(tz).isoformat(),
                        "end": c.end.astimezone(tz).isoformat(),
                    },
                )
            )
    return reasons


def ratio_reason(groups: Sequence[GroupRef], instructors: int, preset: Preset) -> Reason | None:
    """Participants per instructor for groups taught together. Mixed levels take the strictest norm."""
    if not isinstance(instructors, int) or instructors < 1:
        raise ValueError("instructors must be an int >= 1")
    if not groups:
        return None
    strictest = min(groups, key=lambda g: _level(preset, g).max_per_instructor)
    per = _level(preset, strictest).max_per_instructor
    participants = sum(g.size for g in groups)
    capacity = per * instructors
    if participants <= capacity:
        return None
    return Reason(
        Code.RATIO_EXCEEDED,
        {
            "participants": participants,
            "instructors": instructors,
            "max_per_instructor": per,
            "capacity": capacity,
            "program": strictest.program,
            "level": strictest.level,
            "groups": [g.id for g in groups],
        },
    )


def eligible(staff: Staff, shift: Shift, at: datetime, policy: Policy, context: Context) -> Decision:
    """May `staff` take `shift`, judged at moment `at`? Returns every reason, blocking and warning."""
    _require_aware(at, "at")
    tz = ZoneInfo(_venue(context, shift.venue).tz)
    preset = policy.preset
    role = _role(preset, shift)
    own = _own_commitments(staff, shift, context)
    reasons = [
        *_check_role(staff, shift),
        *_check_certs(staff, role, shift, at, tz, preset),
        *_check_programs(staff, shift, tz, preset),
        *_check_hours(own, shift, tz, policy),
        *_check_overlaps(own, shift, tz),
    ]
    ratio = ratio_reason(shift.groups, shift.instructors, preset)
    if ratio is not None:
        reasons.append(ratio)
    return Decision.of(reasons)


# --- ranking ----------------------------------------------------------------
#
# Queue order (product decision, a product decision): 1) eligible only on top; 2) fewer
# hours this week; 3) closer venue, the same venue before any other; 4) program
# and level match the group; 5) longer tenure at the level. Staff id breaks
# the last tie, so the order never depends on input order.


@dataclass(frozen=True)
class Candidate:
    staff_id: str
    decision: Decision
    week_hours: float  # already booked in the week of the shift, this shift not included
    same_venue: bool  # home venue is the shift's venue
    distance_km: float | None  # home venue -> shift venue; None when unknown
    level_match: bool | None  # cleared for every group's program and level; None for a shift without groups
    level_tenure_days: int | None  # days cleared at the shift's level(s); None without groups or level
    levels: tuple[tuple[str, str], ...] = ()  # the shift's (program, level) pairs, for the text


RANK_FACTORS = (
    "eligible",
    "week_hours",
    "same_venue",
    "distance_km",
    "level_match",
    "level_tenure_days",
    "staff_id",
)


@dataclass(frozen=True)
class Ranked:
    position: int
    candidate: Candidate
    above_next: Mapping[str, object] | None  # the first factor that puts this candidate above the next one

    def place(self) -> dict[str, object]:
        """Machine reason for this place in the queue; human text is built from it."""
        c = self.candidate
        return {
            "position": self.position,
            "staff_id": c.staff_id,
            "eligible": c.decision.ok,
            "week_hours": c.week_hours,
            "same_venue": c.same_venue,
            "distance_km": c.distance_km,
            "level_match": c.level_match,
            "levels": [list(pair) for pair in c.levels],
            "level_tenure_days": c.level_tenure_days,
            "above_next": dict(self.above_next) if self.above_next else None,
        }


_EARTH_RADIUS_KM = 6371.0088


def _distance_km(home: Venue | None, venue: Venue) -> float | None:
    if home is None:
        return None
    if home.id == venue.id:
        return 0.0
    if home.lat is None or venue.lat is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (home.lat, home.lon, venue.lat, venue.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h)), 1)


def _cleared_since(staff: Staff, group: GroupRef, day: date) -> date | None:
    held = staff.programs.get(group.program)
    since = held.levels.get(group.level) if held else None
    return since if since is not None and since <= day else None


def _shift_levels(shift: Shift) -> tuple[tuple[str, str], ...]:
    return tuple(dict.fromkeys((g.program, g.level) for g in shift.groups))


def assess(staff: Staff, shift: Shift, at: datetime, policy: Policy, context: Context) -> Candidate:
    """`eligible` plus the facts `rank` sorts by."""
    decision = eligible(staff, shift, at, policy, context)
    venue = _venue(context, shift.venue)
    tz = ZoneInfo(venue.tz)
    home = _venue(context, staff.home_venue) if staff.home_venue is not None else None
    day = _local_day(shift.start, tz)
    week_from, week_to = _week_bounds(week_start_date(day, policy.week_start), tz)
    since = [_cleared_since(staff, g, day) for g in shift.groups]
    level_match = all(s is not None for s in since) if shift.groups else None
    return Candidate(
        staff_id=staff.id,
        decision=decision,
        week_hours=_hours(_booked_seconds(_own_commitments(staff, shift, context), week_from, week_to)),
        same_venue=home is not None and home.id == venue.id,
        distance_km=_distance_km(home, venue),
        level_match=level_match,
        level_tenure_days=min((day - s).days for s in since) if level_match else None,
        levels=_shift_levels(shift),
    )


def _sort_key(c: Candidate) -> tuple:
    return (
        0 if c.decision.ok else 1,
        c.week_hours,
        0 if c.same_venue else 1,
        math.inf if c.distance_km is None else c.distance_km,
        1 if c.level_match is False else 0,
        math.inf if c.level_tenure_days is None else -c.level_tenure_days,
        c.staff_id,
    )


def _factor_value(c: Candidate, factor: str) -> object:
    return c.decision.ok if factor == "eligible" else getattr(c, factor)


def rank(candidates: Iterable[Candidate]) -> list[Ranked]:
    """Order candidates for offers; see the queue order above. Every place carries its reason."""
    ordered = sorted(candidates, key=_sort_key)
    ids = [c.staff_id for c in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("rank: the same staff member appears twice")
    out = []
    for i, c in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        why = None
        if nxt is not None:
            for factor, mine, theirs in zip(RANK_FACTORS, _sort_key(c), _sort_key(nxt)):
                if mine != theirs:
                    why = {
                        "factor": factor,
                        "this": _factor_value(c, factor),
                        "next": _factor_value(nxt, factor),
                        "next_staff_id": nxt.staff_id,
                    }
                    break
        out.append(Ranked(position=i + 1, candidate=c, above_next=why))
    return out
