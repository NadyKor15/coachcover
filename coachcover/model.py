"""Plain data types for the coverage engine. No database, no network.

Times are timezone-aware datetimes. Calendar dates (certificate expiry,
program renewal) are interpreted in the venue's own time zone by the rules.

A shift is one block of work for one role. The groups listed on a shift are
taught together for the whole shift (a merged group in plan B is one shift
with several groups). Back-to-back lessons are separate shifts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# What the engine may do by itself ("auto"), only suggest to a person ("propose") or never do ("off").
AUTONOMY_ACTIONS = ("assign_cover", "plan_b", "merge_groups", "overtime", "night_cover")
AUTONOMY_MODES = ("auto", "propose", "off")
REMEDIES = ("credit", "makeup")
MONEY_KEYS = ("lesson_price", "refund_share", "hourly_wage", "overtime_multiplier", "inspection_penalty", "manager_hour")


def check_autonomy(autonomy: Mapping[str, str]) -> None:
    if not isinstance(autonomy, Mapping) or set(autonomy) != set(AUTONOMY_ACTIONS):
        got = sorted(autonomy) if isinstance(autonomy, Mapping) else type(autonomy).__name__
        raise ValueError(f"autonomy must set exactly {AUTONOMY_ACTIONS}, got {got}")
    bad = {k: v for k, v in autonomy.items() if v not in AUTONOMY_MODES}
    if bad:
        raise ValueError(f"autonomy modes must be one of {AUTONOMY_MODES}: {bad}")
    if autonomy["overtime"] == "auto":
        raise ValueError("autonomy: overtime is never automatic; use 'propose' or 'off'")


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _ends_after(start: datetime, end: datetime) -> bool:
    # compare in UTC: datetimes sharing one tzinfo compare by wall clock, wrong across DST
    return end.astimezone(UTC) > start.astimezone(UTC)


def _require_code(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


# --- preset: what a kind of business needs ---------------------------------


@dataclass(frozen=True)
class CertType:
    code: str
    label: str
    validity_months: int
    match: tuple[str, ...] = ()  # words on a card title that name this type (upper case)
    short: str = ""  # short label for screens ("CPR/AED"); the label when empty

    def __post_init__(self) -> None:
        _require_code(self.code, "cert type code")
        if not _positive_int(self.validity_months):
            raise ValueError(f"cert type {self.code}: validity_months must be a positive int")


@dataclass(frozen=True)
class Role:
    code: str
    label: str
    required_certs: tuple[str, ...]
    reserve: bool = False  # people with this role are kept out of the offer queue (plan B "senior steps in")
    short: str = ""  # short label for screens; the label when empty

    def __post_init__(self) -> None:
        _require_code(self.code, "role code")
        if len(set(self.required_certs)) != len(self.required_certs):
            raise ValueError(f"role {self.code}: duplicate required certificate")


@dataclass(frozen=True)
class Level:
    code: str
    label: str
    max_per_instructor: int

    def __post_init__(self) -> None:
        _require_code(self.code, "level code")
        if not _positive_int(self.max_per_instructor):
            raise ValueError(f"level {self.code}: max_per_instructor must be a positive int")


@dataclass(frozen=True)
class Program:
    code: str
    label: str
    renewal_months: int | None
    levels: Mapping[str, Level]
    short: str = ""  # short code for screens ("LTS")

    def __post_init__(self) -> None:
        _require_code(self.code, "program code")
        if self.renewal_months is not None and not _positive_int(self.renewal_months):
            raise ValueError(f"program {self.code}: renewal_months must be a positive int or None")
        if not self.levels:
            raise ValueError(f"program {self.code}: needs at least one level")
        for key, level in self.levels.items():
            if key != level.code:
                raise ValueError(f"program {self.code}: level key {key!r} != code {level.code!r}")


@dataclass(frozen=True)
class Preset:
    code: str
    label: str
    roles: Mapping[str, Role]
    cert_types: Mapping[str, CertType]
    programs: Mapping[str, Program]
    venue_term: Mapping[str, str] = field(default_factory=dict)  # language -> the word for a venue, e.g. "club"
    young_age_max: int | None = None  # groups with children up to this age use young_max_per_instructor
    young_max_per_instructor: int | None = None
    copy: Mapping[str, str] = field(default_factory=dict)  # screen and prompt texts of this kind of business
    money: Mapping[str, Mapping[str, object]] = field(default_factory=dict)  # prices for the money card, see MONEY_KEYS
    min_rest_hours: float | None = None  # rest a person needs between work on different days; None: no rule
    offer_wave_size: int = 1  # offers sent at once, in queue order (product decision)
    offer_collect_seconds: int = 0  # after the first "yes" of a wave, wait this long for a "yes" from higher up
    plan_b_minutes_before_start: int | None = None  # plan B goes to the manager this long before the start

    def __post_init__(self) -> None:
        _require_code(self.code, "preset code")
        if (self.young_age_max is None) != (self.young_max_per_instructor is None):
            raise ValueError(f"preset {self.code}: young_age_max and young_max_per_instructor go together")
        if self.young_max_per_instructor is not None and not _positive_int(self.young_max_per_instructor):
            raise ValueError(f"preset {self.code}: young_max_per_instructor must be a positive int")
        if self.min_rest_hours is not None and not (
            isinstance(self.min_rest_hours, (int, float)) and not isinstance(self.min_rest_hours, bool)
            and 0 < self.min_rest_hours <= 24
        ):
            raise ValueError(f"preset {self.code}: min_rest_hours must be a number in (0, 24]")
        if not _positive_int(self.offer_wave_size):
            raise ValueError(f"preset {self.code}: offers.wave_size must be an int >= 1")
        if not isinstance(self.offer_collect_seconds, int) or isinstance(self.offer_collect_seconds, bool) \
                or not 0 <= self.offer_collect_seconds <= 3600:
            raise ValueError(f"preset {self.code}: offers.collect_seconds must be an int from 0 to 3600")
        if self.plan_b_minutes_before_start is not None and not _positive_int(self.plan_b_minutes_before_start):
            raise ValueError(f"preset {self.code}: offers.plan_b_minutes_before_start must be an int >= 1")
        for kind, items in (("role", self.roles), ("cert type", self.cert_types), ("program", self.programs)):
            for key, item in items.items():
                if key != item.code:
                    raise ValueError(f"preset {self.code}: {kind} key {key!r} != code {item.code!r}")
        for role in self.roles.values():
            for cert in role.required_certs:
                if cert not in self.cert_types:
                    raise ValueError(f"preset {self.code}: role {role.code} requires unknown certificate {cert!r}")


@dataclass(frozen=True)
class Policy:
    """Settings of one business, built on a preset."""

    preset: Preset
    max_weekly_hours: float
    week_start: int  # 0 = Monday ... 6 = Sunday
    makeup_window_days: int
    autonomy: Mapping[str, str]  # action -> mode, see AUTONOMY_ACTIONS / AUTONOMY_MODES
    offer_timeout_minutes: int  # how long one person has to answer an offer
    overtime_cost_per_hour: float  # shown on an overtime proposal in plan B
    default_remedy: str  # "credit" or "makeup" for families when a lesson is cancelled

    def __post_init__(self) -> None:
        if not _positive_int(self.offer_timeout_minutes):
            raise ValueError("offer_timeout_minutes must be a positive int")
        if not (isinstance(self.overtime_cost_per_hour, (int, float)) and self.overtime_cost_per_hour >= 0):
            raise ValueError("overtime_cost_per_hour must be a number >= 0")
        if self.default_remedy not in REMEDIES:
            raise ValueError(f"default_remedy must be one of {REMEDIES}")
        check_autonomy(self.autonomy)
        if not (0 < self.max_weekly_hours <= 168):
            raise ValueError("max_weekly_hours must be in (0, 168]")
        if not isinstance(self.week_start, int) or not (0 <= self.week_start <= 6):
            raise ValueError("week_start must be an int 0..6 (0 = Monday)")
        if not isinstance(self.makeup_window_days, int) or self.makeup_window_days < 0:
            raise ValueError("makeup_window_days must be a non-negative int")


# --- places and people ------------------------------------------------------


@dataclass(frozen=True)
class Venue:
    id: str
    tz: str
    lat: float | None = None
    lon: float | None = None

    def __post_init__(self) -> None:
        _require_code(self.id, "venue id")
        try:
            ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError(f"venue {self.id}: unknown time zone {self.tz!r}") from exc
        if (self.lat is None) != (self.lon is None):
            raise ValueError(f"venue {self.id}: lat and lon go together")
        if self.lat is not None and not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
            raise ValueError(f"venue {self.id}: coordinates out of range")


@dataclass(frozen=True)
class StaffCert:
    cert_type: str
    issued_on: date
    expires_on: date | None = None  # None: issued_on + validity months of the type
    needs_review: bool = False  # the readings disagree and no person has confirmed
    revoked_on: date | None = None  # the issuer's registry says the card was revoked on this day

    def __post_init__(self) -> None:
        _require_code(self.cert_type, "certificate type")
        if self.expires_on is not None and self.expires_on < self.issued_on:
            raise ValueError(f"certificate {self.cert_type}: expires_on before issued_on")


@dataclass(frozen=True)
class StaffProgram:
    program: str
    renewed_at: date
    levels: Mapping[str, date]  # level code -> qualified since

    def __post_init__(self) -> None:
        _require_code(self.program, "program code")


@dataclass(frozen=True)
class Staff:
    id: str
    name: str
    roles: frozenset[str]
    certs: tuple[StaffCert, ...] = ()
    programs: Mapping[str, StaffProgram] = field(default_factory=dict)
    home_venue: str | None = None
    languages: frozenset[str] = frozenset()  # languages the person speaks with families (ISO 639-1)

    def __post_init__(self) -> None:
        _require_code(self.id, "staff id")
        for key, prog in self.programs.items():
            if key != prog.program:
                raise ValueError(f"staff {self.id}: program key {key!r} != {prog.program!r}")


# --- schedule ---------------------------------------------------------------


@dataclass(frozen=True)
class GroupRef:
    id: str
    program: str
    level: str
    size: int
    age_min: int | None = None  # youngest participant, when known
    families: tuple[tuple[str, int], ...] = ()  # (language, number of families speaking it), when known

    def __post_init__(self) -> None:
        _require_code(self.id, "group id")
        for lang, n in self.families:
            if not (isinstance(lang, str) and lang) or not isinstance(n, int) or n < 0:
                raise ValueError(f"group {self.id}: families must be (language, count >= 0) pairs")
        if not isinstance(self.size, int) or self.size < 0:
            raise ValueError(f"group {self.id}: size must be a non-negative int")
        if self.age_min is not None and (not isinstance(self.age_min, int) or self.age_min < 0):
            raise ValueError(f"group {self.id}: age_min must be a non-negative int")


@dataclass(frozen=True)
class Shift:
    id: str
    venue: str
    role: str
    start: datetime
    end: datetime
    groups: tuple[GroupRef, ...] = ()
    instructors: int = 1  # people teaching these groups together, the candidate included
    zone: str | None = None
    assignee: str | None = None  # who holds (or held, before the call-out) this shift
    merged_shift_ids: tuple[str, ...] = ()  # plan B: the shifts whose groups this shift takes over
    extra_certs: tuple[str, ...] = ()  # certificates this shift needs on top of its role's

    def __post_init__(self) -> None:
        _require_code(self.id, "shift id")
        if self.id in self.merged_shift_ids or len(set(self.merged_shift_ids)) != len(self.merged_shift_ids):
            raise ValueError(f"shift {self.id}: merged_shift_ids must be distinct and not the shift itself")
        _require_aware(self.start, f"shift {self.id} start")
        _require_aware(self.end, f"shift {self.id} end")
        if not _ends_after(self.start, self.end):
            raise ValueError(f"shift {self.id}: end must be after start")
        if not _positive_int(self.instructors):
            raise ValueError(f"shift {self.id}: instructors must be an int >= 1")
        ids = [g.id for g in self.groups]
        if len(set(ids)) != len(ids):
            raise ValueError(f"shift {self.id}: the same group is listed twice")


@dataclass(frozen=True)
class Commitment:
    """Time a person is already booked: a shift or a group they teach."""

    ref: str
    kind: str  # "shift" | "group"
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.kind not in ("shift", "group"):
            raise ValueError(f"commitment {self.ref}: kind must be 'shift' or 'group'")
        _require_aware(self.start, f"commitment {self.ref} start")
        _require_aware(self.end, f"commitment {self.ref} end")
        if not _ends_after(self.start, self.end):
            raise ValueError(f"commitment {self.ref}: end must be after start")


@dataclass(frozen=True)
class Context:
    venues: Mapping[str, Venue]
    commitments: Mapping[str, tuple[Commitment, ...]] = field(default_factory=dict)  # by staff id
    # people who called out: (from, to) windows in which they are offered nothing (at least their whole day)
    absences: Mapping[str, tuple[tuple[datetime, datetime], ...]] = field(default_factory=dict)
