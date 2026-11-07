"""Fifty training mornings: a group chat with a phone tree against CoachCover, on the same mornings.

Synthetic, and labelled so everywhere: the fictional school of coachcover/seed.py,
changed per morning from one seed. No database, no network, no models.

A morning (`make_morning`) is one weekday of the school week with 1-3 call-outs
before 06:00 and a changed staff state: cards that expire around the date, a card
whose two readings disagree, a renewal that is overdue, weekly hours pushed up
to the limit, morning groups handed to another instructor, and for every person
whether they are awake, how fast they answer and whether they want the shift.
Both strategies run on that same morning, each on its own copy of the schedule.

- Baseline, "group chat + phone tree": the manager wakes up, posts in the staff
  chat, takes the first "yes" and books it. After a wait the manager also phones
  free people who hold the shift's role, in a random order. Nothing is checked:
  not the cards, the program, the weekly hours or the group norm. Nobody by the
  start of the shift: a lesson goes into the group next to it (norm unchecked),
  or is cancelled; a shift without groups stays empty.
- CoachCover: `rules.eligible` + `rules.rank`, one offer at a time with the
  policy's timer, the rules again at "yes"; when nobody is left, plan B in the
  core's order (merge, reserve person, overtime, move, close) under the policy's
  autonomy. Plan B comes from the core's pure function, coachcover.planb.plan_b_options.

Every placement of both strategies is audited by the same `rules.eligible` at the
moment of booking; a placement with a blocking reason is a violation. Run:

    uv run python -m coachcover.sim --mornings 50 --seed 20261109 --out docs/sim
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from coachcover import seed as school_seed
from coachcover.model import Commitment, Context, GroupRef, Policy, Shift, Staff, StaffCert, StaffProgram, Venue
from coachcover.planb import plan_b_options as core_plan_b_options
from coachcover.presets import default_policy
from coachcover.rules import Code, add_months, assess, eligible, rank

SEED = 20261109
MORNINGS = 50
SIM_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "sim"
OUT_NAME = "SIM-50-MORNINGS"

BASELINE = "baseline"
COACHCOVER = "coachcover"
STRATEGY_LABEL = {BASELINE: "Group chat + phone tree", COACHCOVER: "CoachCover"}

# --- violations: blocking codes grouped the way the report counts them -------

CATEGORY_OF = {
    Code.ROLE_MISSING: "role",
    Code.CERT_MISSING: "certificate",
    Code.CERT_NOT_YET_VALID: "certificate",
    Code.CERT_EXPIRED: "certificate",
    Code.CERT_EXPIRES_DURING_SHIFT: "certificate",
    Code.CERT_NEEDS_REVIEW: "certificate",
    Code.CERT_REVOKED: "certificate",
    Code.PROGRAM_MISSING: "program",
    Code.LEVEL_MISSING: "program",
    Code.PROGRAM_RENEWAL_OVERDUE: "program",
    Code.HOURS_LIMIT_EXCEEDED: "hours",
    Code.REST_TOO_SHORT: "hours",
    Code.RATIO_EXCEEDED: "ratio",
    Code.OVERLAPS_COMMITMENT: "other",
    Code.IS_THE_ABSENT_PERSON: "other",
    Code.CALLED_OUT: "other",
}
# a placement is counted once, under the first category in this order
PRIMARY_ORDER = ("role", "certificate", "program", "hours", "ratio", "other")
CATEGORY_LABEL = {
    "role": "no role for the shift",
    "certificate": "certificate expired, revoked, missing or unconfirmed",
    "program": "no program or level, or renewal overdue",
    "hours": "over the weekly hours limit or too little rest",
    "ratio": "group norm broken (participants per instructor)",
    "other": "other (double booking)",
}
EXPIRED_CODES = frozenset({Code.CERT_EXPIRED.value, Code.CERT_EXPIRES_DURING_SHIFT.value})


# --- parameters: every one is an assumption, written down --------------------


@dataclass(frozen=True)
class SimParams:
    # how people behave at dawn (both strategies)
    p_awake: float = 0.5
    wake_from: str = "05:15"
    wake_to: str = "07:30"
    reply_lag_min: float = 1.0
    reply_lag_max: float = 6.0
    p_willing_role: float = 0.35
    p_willing_other: float = 0.05
    # the manager in the baseline
    manager_notice_minutes: float = 10.0
    chat_post_minutes: float = 2.0
    chat_wait_minutes: float = 10.0
    call_minutes: float = 3.0
    p_pickup_awake: float = 0.8
    p_pickup_asleep: float = 0.35
    confirm_minutes: float = 1.0
    # CoachCover
    intake_minutes: float = 1.0
    decide_minutes: float = 1.0
    # the mornings
    callouts_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
    callout_from: str = "04:30"
    callout_to: str = "05:55"
    callout_lead_minutes: int = 15
    morning_until: str = "09:00"
    card_changes: tuple[int, int] = (3, 7)
    card_offset_days: tuple[int, int] = (-21, 20)
    p_card_expired: float = 0.5
    p_card_on_the_day: float = 0.15
    p_card_unconfirmed: float = 0.3
    hours_changes: tuple[int, int] = (2, 5)
    hours_target: tuple[float, float] = (34.0, 40.0)
    rota_swaps: tuple[int, int] = (0, 3)
    p_renewal_overdue: float = 0.4
    # variants for the "what if" rows; the main table uses the defaults (the core as built, the literal baseline)
    # "waves": the core as built (the preset's waves, collection window and plan B deadline; a product decision);
    # "one": one offer at a time, plan B only when the queue is empty (the core before 2026-11-07)
    offers: str = "waves"
    baseline_checks_role: bool = False


ASSUMPTIONS = {
    "p_awake": "Share of staff already awake when the first message comes (the rest wake between wake_from and wake_to).",
    "wake_from": "Earliest wake-up time of a person who was asleep (venue time).",
    "wake_to": "Latest wake-up time of a person who was asleep (venue time).",
    "reply_lag_min": "Minutes from seeing a message to answering it, lower bound (uniform).",
    "reply_lag_max": "Minutes from seeing a message to answering it, upper bound (uniform).",
    "p_willing_role": "Chance that a free person who holds the shift's role wants the shift that morning.",
    "p_willing_other": "Chance that a free person without the shift's role still answers 'I can come' in the chat.",
    "manager_notice_minutes": "Minutes from a call-out (or a plan B card) to the manager seeing it at dawn.",
    "chat_post_minutes": "Minutes the manager spends writing one post in the staff chat.",
    "chat_wait_minutes": "Minutes the manager waits for the chat before starting to phone people.",
    "call_minutes": "Minutes per phone call: dialing, ringing, voicemail or a short talk.",
    "p_pickup_awake": "Chance that an awake person picks up the manager's call.",
    "p_pickup_asleep": "Chance that a sleeping person is woken by the call and picks up.",
    "confirm_minutes": "Minutes the manager spends confirming the person or deciding the fallback.",
    "intake_minutes": "Minutes from the call-out to the first offer in CoachCover (voice to text to case).",
    "decide_minutes": "Minutes the manager spends on a plan B card after seeing it.",
    "callouts_weights": "Chance of 1, 2 or 3 call-outs on one morning.",
    "callout_from": "Earliest call-out time (venue time).",
    "callout_to": "Latest call-out time (venue time); every call-out comes before 06:00.",
    "callout_lead_minutes": "A call-out comes at least this many minutes before the person's first shift.",
    "morning_until": "Shifts of the absent person that start before this time need cover.",
    "card_changes": "How many people get one card moved to expire around the morning (min, max).",
    "card_offset_days": "Range of the new expiry date, in days from the morning (min, max).",
    "p_card_expired": "Share of moved cards that expire before the morning.",
    "p_card_on_the_day": "Share of moved cards that expire on the morning itself (allowed, with a warning).",
    "p_card_unconfirmed": "Chance that one card's two readings disagree and nobody has confirmed it yet.",
    "hours_changes": "How many people get extra hours this week (min, max).",
    "hours_target": "Their weekly total after the extra hours, before any cover (min, max); the limit is the policy's.",
    "rota_swaps": "How many of the morning's lessons are handed to another cleared instructor (min, max).",
    "p_renewal_overdue": "Chance that one instructor's program renewal is overdue that week.",
}


def _clock(value: str) -> time:
    return time.fromisoformat(value)


# --- the school as rule types ------------------------------------------------


@dataclass(frozen=True)
class World:
    policy: Policy
    venues: Mapping[str, Venue]
    tz: ZoneInfo
    staff: Mapping[str, Staff]
    shifts: Mapping[str, Shift]
    names: Mapping[str, str]
    week_start: date


def _needs_review(row: dict) -> bool:
    """Same test as the staff_cert trust trigger (migration 003)."""
    if row["verified_by_human"] is not None:
        return False
    a = (row["reader_a"] or {}).get("fields")
    b = (row["reader_b"] or {}).get("fields")
    if not isinstance(a, dict) or not isinstance(b, dict) or a != b:
        return True
    return (
        a.get("type") != row["cert_type"]
        or a.get("issued_on") != row["issued_on"].isoformat()
        or a.get("expires_on") != row["expires_on"].isoformat()
    )


@lru_cache(maxsize=1)
def load_world() -> World:
    """The training school from coachcover/seed.py as plain rule types."""
    school = school_seed.build_school()
    policy = default_policy(school_seed.PRESET)
    if len(policy.preset.programs) != 1:
        raise ValueError("the training school expects a preset with exactly one program")
    program = next(iter(policy.preset.programs))
    venues = {vid: Venue(vid, tz, lat, lon) for vid, _, tz, lat, lon in school.venues}
    roles: dict[str, set[str]] = defaultdict(set)
    for sid, role in school.staff_roles:
        roles[sid].add(role)
    revoked = {(r["cert_type"], r["card_number"]): r["revoked_on"] for r in school.registry if r["revoked_on"]}
    certs: dict[str, list[StaffCert]] = defaultdict(list)
    for row in school.certs:
        certs[row["staff_id"]].append(StaffCert(row["cert_type"], row["issued_on"], row["expires_on"], _needs_review(row),
                                                revoked.get((row["cert_type"], row["card_number"]))))
    programs: dict[str, dict[str, StaffProgram]] = defaultdict(dict)
    for p in school.programs:
        if p["program"] != program:
            raise ValueError(f"unexpected program {p['program']!r} in the training school")
        programs[p["staff_id"]][program] = StaffProgram(program, p["renewed_at"], dict(p["levels"]))
    staff = {
        sid: Staff(sid, name, frozenset(roles[sid]), tuple(certs[sid]), programs[sid], home,
                   frozenset(school_seed.staff_languages(lang)))
        for sid, name, lang, home in school.staff
    }
    groups = {
        g["id"]: GroupRef(g["id"], program, g["level"], g["size"], g["age_min"], tuple(sorted(g["family_counts"].items())))
        for g in school.groups
    }
    shifts = {
        s["id"]: Shift(
            s["id"], s["venue_id"], s["role"], s["starts_at"], s["ends_at"],
            tuple(groups[g] for g in s["groups"]), s["instructors"], s["zone_id"], assignee=s["staff_id"],
            extra_certs=tuple(s["extra_certs"]),
        )
        for s in school.shifts
    }
    return World(
        policy=policy,
        venues=venues,
        tz=school_seed.TZ,
        staff=staff,
        shifts=shifts,
        names={sid: name for sid, name, _, _ in school.staff},
        week_start=school_seed.WEEK_START,
    )


# --- one morning ---------------------------------------------------------------


@dataclass(frozen=True)
class Behavior:
    """How one person reacts that morning. The same for both strategies."""

    awake_from: datetime
    lag_minutes: float
    willing_u: float
    pickup_u: float

    def answers_at(self, sent: datetime) -> datetime:
        return max(sent, self.awake_from) + timedelta(minutes=self.lag_minutes)

    def wants(self, person: Staff, shift: Shift, params: SimParams) -> bool:
        p = params.p_willing_role if shift.role in person.roles else params.p_willing_other
        return self.willing_u < p


@dataclass(frozen=True)
class Callout:
    staff_id: str
    at: datetime
    shift_ids: tuple[str, ...]


@dataclass(frozen=True)
class Morning:
    index: int
    seed: int
    day: date
    staff: Mapping[str, Staff]
    shifts: Mapping[str, Shift]
    extra: Mapping[str, tuple[Commitment, ...]]
    callouts: tuple[Callout, ...]
    behavior: Mapping[str, Behavior]
    changes: tuple[dict, ...]

    @property
    def open_ids(self) -> frozenset[str]:
        return frozenset(sid for c in self.callouts for sid in c.shift_ids)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _local(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return _utc(a0) < _utc(b1) and _utc(b0) < _utc(a1)


def _bookings(shifts: Mapping[str, Shift], extra: Mapping[str, Iterable[Commitment]], skip: Iterable[str] = ()) -> dict:
    skip = set(skip)
    out: dict[str, list[Commitment]] = defaultdict(list)
    for sh in shifts.values():
        if sh.assignee is not None and sh.id not in skip:
            out[sh.assignee].append(Commitment(sh.id, "shift", sh.start, sh.end))
    for sid, items in extra.items():
        out[sid].extend(items)
    return out


def _week_seconds(items: Iterable[Commitment], start: datetime, end: datetime) -> int:
    s, e = _utc(start), _utc(end)
    spans = sorted((max(_utc(c.start), s), min(_utc(c.end), e)) for c in items)
    total, cur = 0, None
    for a, b in spans:
        if a >= b:
            continue
        if cur and a <= cur[1]:
            cur = (cur[0], max(cur[1], b))
        else:
            if cur:
                total += int((cur[1] - cur[0]).total_seconds())
            cur = (a, b)
    if cur:
        total += int((cur[1] - cur[0]).total_seconds())
    return total


def _pick_range(rng: random.Random, bounds: tuple[int, int]) -> int:
    return rng.randint(bounds[0], bounds[1])


def make_morning(world: World, index: int, seed: int = SEED, params: SimParams = SimParams()) -> Morning:
    """One morning; the same (world, index, seed, params) always gives the same morning."""
    rng = random.Random(f"coachcover-sim/{seed}/{index}")
    tz = world.tz
    day = school_seed.MONDAY + timedelta(days=index % 5)
    until = _local(day, _clock(params.morning_until), tz)
    day_from = _local(day, time(0), tz)
    staff = dict(world.staff)
    shifts = dict(world.shifts)
    extra: dict[str, list[Commitment]] = defaultdict(list)
    changes: list[dict] = []
    ids = sorted(staff)

    # 1. some of the morning's lessons go to another cleared, free instructor
    lessons = sorted(
        sid for sid, sh in shifts.items() if sh.groups and day_from <= sh.start.astimezone(tz) < until
    )
    for shift_id in rng.sample(lessons, min(len(lessons), _pick_range(rng, params.rota_swaps))):
        sh = shifts[shift_id]
        booked = _bookings(shifts, extra)
        cleared = [
            sid for sid in ids
            if sid != sh.assignee and sh.role in staff[sid].roles
            and all(g.level in staff[sid].programs.get(g.program, StaffProgram(g.program, day, {})).levels for g in sh.groups)
            and not any(_overlap(c.start, c.end, sh.start, sh.end) for c in booked.get(sid, ()))
        ]
        if cleared:
            new = rng.choice(cleared)
            changes.append({"kind": "rota", "shift_id": shift_id, "from": sh.assignee, "to": new})
            shifts[shift_id] = replace(sh, assignee=new)

    # 2. extra hours this week: night blocks 21:15-05:00, when the school has no shifts
    week_from = _local(world.week_start, time(0), tz)
    week_to = _local(world.week_start + timedelta(days=7), time(0), tz)
    booked = _bookings(shifts, extra)
    for sid in rng.sample(ids, _pick_range(rng, params.hours_changes)):
        target = round(rng.uniform(*params.hours_target) * 4) / 4
        have = _week_seconds(booked.get(sid, ()), week_from, week_to) / 3600
        need = target - have
        night = 0
        while need > 1e-9 and night < 6:
            start = _local(world.week_start + timedelta(days=night), time(21, 15), tz)
            hours = min(need, 7.75)
            extra[sid].append(Commitment(f"X{index:02d}-{sid}-{night}", "shift", start, start + timedelta(hours=hours)))
            need -= hours
            night += 1
        if night:
            total = _week_seconds([*booked.get(sid, ()), *extra[sid]], week_from, week_to) / 3600
            changes.append({"kind": "hours", "staff_id": sid, "week_hours": round(total, 2)})

    # 3. cards that expire around the morning
    for sid in rng.sample(ids, _pick_range(rng, params.card_changes)):
        person = staff[sid]
        k = rng.randrange(len(person.certs))
        roll = rng.random()
        if roll < params.p_card_expired:
            offset = rng.randint(params.card_offset_days[0], -1)
        elif roll < params.p_card_expired + params.p_card_on_the_day:
            offset = 0
        else:
            offset = rng.randint(1, params.card_offset_days[1])
        expires = day + timedelta(days=offset)
        old = person.certs[k]
        card = replace(old, issued_on=add_months(expires, -24), expires_on=expires)
        staff[sid] = replace(person, certs=person.certs[:k] + (card,) + person.certs[k + 1:])
        changes.append({"kind": "card", "staff_id": sid, "cert_type": old.cert_type, "expires_on": expires.isoformat()})

    # 4. one card whose two readings disagree, nobody has confirmed it
    if rng.random() < params.p_card_unconfirmed:
        sid = rng.choice(ids)
        person = staff[sid]
        k = rng.randrange(len(person.certs))
        card = replace(person.certs[k], needs_review=True)
        staff[sid] = replace(person, certs=person.certs[:k] + (card,) + person.certs[k + 1:])
        changes.append({"kind": "unconfirmed", "staff_id": sid, "cert_type": card.cert_type})

    # 5. one instructor's program renewal overdue
    if rng.random() < params.p_renewal_overdue:
        with_programs = [sid for sid in ids if staff[sid].programs]
        sid = rng.choice(with_programs)
        person = staff[sid]
        code = sorted(person.programs)[0]
        months = world.policy.preset.programs[code].renewal_months or 12
        renewed = add_months(day, -(months + 1))
        programs = {**person.programs, code: replace(person.programs[code], renewed_at=renewed)}
        staff[sid] = replace(person, programs=programs)
        changes.append({"kind": "renewal", "staff_id": sid, "program": code, "renewed_at": renewed.isoformat()})

    # 6. call-outs before 06:00, for every shift of the person that starts this morning
    count = rng.choices((1, 2, 3), weights=params.callouts_weights)[0]
    mornings: dict[str, list[Shift]] = defaultdict(list)
    for sh in shifts.values():
        if sh.assignee is not None and day_from <= sh.start.astimezone(tz) < until:
            mornings[sh.assignee].append(sh)
    absent = rng.sample(sorted(mornings), min(count, len(mornings)))
    earliest = _local(day, _clock(params.callout_from), tz)
    callouts = []
    for sid in absent:
        own = sorted(mornings[sid], key=lambda s: (_utc(s.start), s.id))
        latest = min(own[0].start - timedelta(minutes=params.callout_lead_minutes), _local(day, _clock(params.callout_to), tz))
        span = max(0, int((_utc(latest) - _utc(earliest)).total_seconds() // 60))
        at = earliest + timedelta(minutes=rng.randint(0, span))
        callouts.append(Callout(sid, at, tuple(s.id for s in own)))
    callouts.sort(key=lambda c: (_utc(c.at), c.staff_id))

    # 7. how each person reacts this morning
    wake_from, wake_to = _local(day, _clock(params.wake_from), tz), _local(day, _clock(params.wake_to), tz)
    wake_span = int((_utc(wake_to) - _utc(wake_from)).total_seconds() // 60)
    behavior = {}
    for sid in ids:
        awake = rng.random() < params.p_awake
        wake = day_from if awake else wake_from + timedelta(minutes=rng.randint(0, wake_span))
        behavior[sid] = Behavior(
            awake_from=wake,
            lag_minutes=round(rng.uniform(params.reply_lag_min, params.reply_lag_max), 1),
            willing_u=rng.random(),
            pickup_u=rng.random(),
        )
    return Morning(
        index=index,
        seed=seed,
        day=day,
        staff=staff,
        shifts=shifts,
        extra={k: tuple(v) for k, v in extra.items()},
        callouts=tuple(callouts),
        behavior=behavior,
        changes=tuple(changes),
    )


# --- one strategy's copy of the schedule ------------------------------------------


class State:
    def __init__(self, world: World, morning: Morning):
        self.world = world
        self.morning = morning
        self.shifts: dict[str, Shift] = dict(morning.shifts)
        self.booked = _bookings(self.shifts, morning.extra, skip=morning.open_ids)

    def context(self) -> Context:
        # a person who called out is offered nothing that day (as in the core: repo.load_absences)
        tz = ZoneInfo(self.world.tz) if isinstance(self.world.tz, str) else self.world.tz
        absences: dict[str, tuple] = {}
        for c in self.morning.callouts:
            first = datetime.combine(c.at.astimezone(tz).date(), time(0), tzinfo=tz)
            absences[c.staff_id] = ((first, first + timedelta(days=1)),)
        return Context(self.world.venues, {sid: tuple(items) for sid, items in self.booked.items()}, absences)

    def busy(self, staff_id: str, shift: Shift) -> bool:
        return any(c.ref != shift.id and _overlap(c.start, c.end, shift.start, shift.end) for c in self.booked.get(staff_id, ()))

    def book(self, staff_id: str, shift: Shift) -> None:
        self.booked[staff_id].append(Commitment(shift.id, "shift", shift.start, shift.end))

    def partners(self, shift: Shift) -> list[Shift]:
        """Scheduled shifts with groups next to this one: same venue, role, start and end (as the core's plan B)."""
        open_ids = self.morning.open_ids
        return sorted(
            (
                p for p in self.shifts.values()
                if p.id != shift.id and p.id not in open_ids and p.groups and p.assignee is not None
                and p.venue == shift.venue and p.role == shift.role
                and _utc(p.start) == _utc(shift.start) and _utc(p.end) == _utc(shift.end)
            ),
            key=lambda p: p.id,
        )

    def merge(self, partner: Shift, shift: Shift) -> Shift:
        """The partner's teacher takes both groups. Returned as the core judges a merge: no assignee (the rules read
        `assignee` as the absent person) and the merged shift's id, so its own booking is not a double booking."""
        merged = replace(partner, groups=partner.groups + shift.groups, assignee=None, merged_shift_ids=(shift.id,))
        self.shifts[partner.id] = replace(partner, groups=partner.groups + shift.groups)
        return merged


@dataclass
class CaseResult:
    shift_id: str
    lesson: bool
    callout_at: datetime
    start: datetime
    closed_at: datetime
    outcome: str  # covered | merged | reserve | overtime | moved | cancelled | uncovered
    channel: str  # chat | phone | offer | plan_b | fallback
    staff_id: str | None
    human: bool
    codes: tuple[str, ...] = ()  # blocking codes of the placement, at the moment of booking
    approved_overtime: bool = False
    groups_cancelled: int = 0
    participants_cancelled: int = 0
    manager_minutes: float = 0.0
    asked: int = 0

    @property
    def placed(self) -> bool:
        return self.staff_id is not None

    @property
    def violation(self) -> bool:
        return bool(self.codes)

    @property
    def minutes(self) -> float:
        return round((_utc(self.closed_at) - _utc(self.callout_at)).total_seconds() / 60, 1)

    @property
    def late(self) -> bool:
        return _utc(self.closed_at) > _utc(self.start)


def _audit(state: State, staff_id: str, shift: Shift, at: datetime, allow: frozenset[Code] = frozenset()) -> tuple[str, ...]:
    decision = eligible(state.morning.staff[staff_id], shift, at, state.world.policy, state.context())
    return tuple(r.code.value for r in decision.reasons if r.blocking and r.code not in allow)


def _cases(morning: Morning) -> list[tuple[Callout, Shift]]:
    pairs = [(c, morning.shifts[sid]) for c in morning.callouts for sid in c.shift_ids]
    return sorted(pairs, key=lambda p: (_utc(p[0].at), _utc(p[1].start), p[1].id))


def _participants(shift: Shift) -> int:
    return sum(g.size for g in shift.groups)


# --- baseline: group chat + phone tree ---------------------------------------------


def run_baseline(world: World, morning: Morning, params: SimParams = SimParams()) -> list[CaseResult]:
    state = State(world, morning)
    rng = random.Random(f"coachcover-sim/{morning.seed}/{morning.index}/phone")
    cases = _cases(morning)
    absent = {c.staff_id for c in morning.callouts}
    # the manager first posts every call-out in the chat, one after another
    posts, free = [], None
    for callout, _ in cases:
        notice = callout.at + timedelta(minutes=params.manager_notice_minutes)
        post = notice if free is None else max(notice, free)
        free = post + timedelta(minutes=params.chat_post_minutes)
        posts.append(post)
    phone_free = free
    results = []
    for (callout, shift), post in zip(cases, posts):
        deadline = shift.start
        staff = morning.staff
        chat = sorted(
            (morning.behavior[sid].answers_at(post), sid)
            for sid in sorted(staff)
            if sid not in absent and not state.busy(sid, shift) and morning.behavior[sid].wants(staff[sid], shift, params)
            and (shift.role in staff[sid].roles or not params.baseline_checks_role)
        )
        chat = [(t, sid) for t, sid in chat if _utc(t) < _utc(deadline)]
        first_chat = chat[0] if chat else None
        calls, phone_yes = 0, None
        calls_from = max(post + timedelta(minutes=params.chat_wait_minutes), phone_free)
        if first_chat is None or _utc(first_chat[0]) > _utc(calls_from):
            order = [
                sid for sid in sorted(staff)
                if sid not in absent and shift.role in staff[sid].roles and not state.busy(sid, shift)
            ]
            rng.shuffle(order)
            t = calls_from
            for sid in order:
                if first_chat and _utc(first_chat[0]) <= _utc(t):
                    break
                if _utc(t + timedelta(minutes=params.call_minutes)) > _utc(deadline):
                    break
                calls += 1
                b = morning.behavior[sid]
                awake = _utc(t) >= _utc(b.awake_from)
                picks_up = b.pickup_u < (params.p_pickup_awake if awake else params.p_pickup_asleep)
                t += timedelta(minutes=params.call_minutes)
                if picks_up and b.wants(staff[sid], shift, params):
                    phone_yes = (t, sid)
                    break
            phone_free = max(phone_free, t)
        manager = params.chat_post_minutes + calls * params.call_minutes + params.confirm_minutes
        options = [x for x in (first_chat and (*first_chat, "chat"), phone_yes and (*phone_yes, "phone")) if x]
        if options:
            yes_at, sid, channel = min(options, key=lambda x: (_utc(x[0]), x[2]))
            at = yes_at + timedelta(minutes=params.confirm_minutes)
            codes = _audit(state, sid, shift, at)
            state.book(sid, shift)
            results.append(CaseResult(shift.id, bool(shift.groups), callout.at, shift.start, at, "covered", channel, sid,
                                      True, codes, manager_minutes=manager, asked=calls))
            continue
        # nobody by the start: the lesson goes into the group next to it, unchecked, or is cancelled
        partners = state.partners(shift) if shift.groups else []
        if partners:
            partner = partners[0]
            merged = state.merge(partner, shift)
            codes = _audit(state, partner.assignee, merged, deadline)
            results.append(CaseResult(shift.id, True, callout.at, shift.start, deadline, "merged", "fallback",
                                      partner.assignee, True, codes, manager_minutes=manager, asked=calls))
        elif shift.groups:
            results.append(CaseResult(shift.id, True, callout.at, shift.start, deadline, "cancelled", "fallback", None, True,
                                      groups_cancelled=len(shift.groups), participants_cancelled=_participants(shift),
                                      manager_minutes=manager, asked=calls))
        else:
            results.append(CaseResult(shift.id, False, callout.at, shift.start, deadline, "uncovered", "fallback", None, True,
                                      manager_minutes=manager, asked=calls))
    return results


# --- CoachCover: rules, ranked offers with a timer, plan B ---------------------------


def plan_b_options(state: State, shift: Shift, absent: str, now: datetime, exclude: Iterable[str] = ()) -> list[dict]:
    """Plan B from the core's pure function (coachcover.planb), on the replay's own state."""
    options, _ = core_plan_b_options(shift=shift, absent=absent, partners=state.partners(shift),
                                     staff=state.morning.staff, policy=state.world.policy, context=state.context(),
                                     now=now, exclude=frozenset(exclude))
    return options


def _offer(morning: Morning, staff_id: str, shift: Shift, sent: datetime, window: timedelta,
           params: SimParams) -> tuple[str, datetime]:
    """One offer: ("yes" | "no" | "expired", when)."""
    b = morning.behavior[staff_id]
    answer = b.answers_at(sent)
    if _utc(answer) > _utc(sent + window):
        return "expired", sent + window
    return ("yes" if b.wants(morning.staff[staff_id], shift, params) else "no"), answer


def run_coachcover(world: World, morning: Morning, params: SimParams = SimParams()) -> list[CaseResult]:
    state = State(world, morning)
    policy = world.policy
    window = timedelta(minutes=policy.offer_timeout_minutes)
    reserve_roles = {r.code for r in policy.preset.roles.values() if r.reserve}
    notice = timedelta(minutes=params.manager_notice_minutes)
    decide = timedelta(minutes=params.decide_minutes)
    results = []
    for callout, shift in _cases(morning):
        t = callout.at + timedelta(minutes=params.intake_minutes)
        people = [s for s in morning.staff.values() if not (s.roles & reserve_roles)]
        queue = [r.candidate.staff_id for r in rank(assess(s, shift, t, policy, state.context()) for s in people)
                 if r.candidate.decision.ok]
        asked_ids: list[str] = []
        done = None
        preset = policy.preset
        waves = params.offers == "waves"
        size = preset.offer_wave_size if waves else 1
        collect = timedelta(seconds=preset.offer_collect_seconds if waves else 0)
        lead = preset.plan_b_minutes_before_start if waves else None
        deadline = shift.start - timedelta(minutes=lead) if lead is not None else None
        winner = None
        if policy.autonomy["assign_cover"] != "off":
            pending = list(queue)
            while pending and winner is None:
                # a wave: the next `size` people at once; the highest "yes" wins once nobody above it can still
                # answer or `collect` after the first "yes"; with nobody, the next wave when the last one is over
                wave, pending = pending[:size], pending[size:]
                asked_ids.extend(wave)
                answers = sorted(((*_offer(morning, sid, shift, t, window, params), i, sid) for i, sid in enumerate(wave)),
                                 key=lambda a: (_utc(a[1]), a[2]))
                held: list[tuple[int, str]] = []
                resolved: set[int] = set()
                first_yes = None
                settle = None
                for answer, when, i, sid in answers:
                    if first_yes is not None and _utc(when) > _utc(first_yes + collect):
                        settle = first_yes + collect
                        break
                    resolved.add(i)
                    if answer == "yes" and eligible(morning.staff[sid], shift, when, policy, state.context()).ok:
                        held.append((i, sid))
                        first_yes = first_yes or when
                    if held and all(k in resolved for k in range(min(held)[0])):
                        settle = when
                        break
                    t = when
                if held:
                    t = settle if settle is not None else first_yes + collect
                    winner = min(held)[1]
                elif answers:
                    t = max((a[1] for a in answers), key=_utc)
        # with a deadline, plan B goes to the manager at the deadline (or at once, for a late call-out) and the
        # manager acts after noticing it; a "yes" before that still covers the shift
        plan_b_at = max(deadline, callout.at + timedelta(minutes=params.intake_minutes), key=_utc) if deadline else None
        if winner is not None and (plan_b_at is None or _utc(t) < _utc(plan_b_at + notice + decide)):
            human = policy.autonomy["assign_cover"] == "propose"
            at = t + notice + decide if human else t
            codes = _audit(state, winner, shift, at)
            state.book(winner, shift)
            done = CaseResult(shift.id, bool(shift.groups), callout.at, shift.start, at, "covered", "offer", winner, human,
                              codes, manager_minutes=params.decide_minutes if human else 0.0, asked=len(asked_ids))
        if done is None:
            # plan B: when the queue is empty, or at the deadline while offers are still out (whichever is first)
            start_b = t if plan_b_at is None or _utc(t) < _utc(plan_b_at) else plan_b_at
            done = _plan_b(state, callout, shift, start_b, len(asked_ids), params, exclude=asked_ids)
        results.append(done)
    return results


def _plan_b(state: State, callout: Callout, shift: Shift, t: datetime, asked: int, params: SimParams,
            exclude: Iterable[str] = ()) -> CaseResult:
    morning, policy = state.morning, state.world.policy
    options = plan_b_options(state, shift, callout.staff_id, t, exclude=exclude)
    notice = timedelta(minutes=params.manager_notice_minutes)
    decide = timedelta(minutes=params.decide_minutes)
    manager = 0.0
    lesson = bool(shift.groups)
    base = dict(shift_id=shift.id, lesson=lesson, callout_at=callout.at, start=shift.start, channel="plan_b")
    first = True
    for option in options:
        if option["mode"] == "off":
            continue
        human = option["mode"] != "auto"
        if human:
            # the manager sees the card after a while the first time, then decides on the spot
            t = t + (notice if first else timedelta(0)) + decide
            manager += params.decide_minutes
        first = False
        kind = option["kind"]
        if kind == "merge":
            partner = state.shifts[option["into_shift_id"]]
            merged = state.merge(partner, shift)
            codes = _audit(state, partner.assignee, merged, t)
            return CaseResult(**base, closed_at=t, outcome="merged", staff_id=partner.assignee, human=human,
                              codes=codes, manager_minutes=manager, asked=asked)
        if kind == "lead":
            sid = option["staff_id"]
            codes = _audit(state, sid, shift, t)
            state.book(sid, shift)
            return CaseResult(**base, closed_at=t, outcome="reserve", staff_id=sid, human=human, codes=codes,
                              manager_minutes=manager, asked=asked)
        if kind == "overtime":
            sid = option["staff_id"]
            asked += 1
            answer, t = _offer(morning, sid, shift, t, timedelta(minutes=policy.offer_timeout_minutes), params)
            if answer == "yes":
                codes = _audit(state, sid, shift, t, allow=frozenset({Code.HOURS_LIMIT_EXCEEDED}))
                state.book(sid, shift)
                return CaseResult(**base, closed_at=t, outcome="overtime", staff_id=sid, human=True, codes=codes,
                                  approved_overtime=True, manager_minutes=manager, asked=asked)
            continue  # nobody took the overtime: the manager goes to the next option
        if kind == "reschedule":
            return CaseResult(**base, closed_at=t, outcome="moved", staff_id=None, human=human,
                              groups_cancelled=len(shift.groups), participants_cancelled=_participants(shift),
                              manager_minutes=manager, asked=asked)
        # close
        return CaseResult(**base, closed_at=t, outcome="cancelled" if lesson else "uncovered", staff_id=None, human=human,
                          groups_cancelled=len(shift.groups), participants_cancelled=_participants(shift),
                          manager_minutes=manager, asked=asked)
    # every option is off in the policy: the case stays open until the shift starts
    return CaseResult(**base, closed_at=shift.start, outcome="cancelled" if lesson else "uncovered", staff_id=None,
                      human=False, groups_cancelled=len(shift.groups), participants_cancelled=_participants(shift),
                      manager_minutes=manager, asked=asked)


# --- the run and its numbers -------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _p90(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)], 1)


def primary_category(codes: Iterable[str]) -> str | None:
    cats = {CATEGORY_OF[Code(c)] for c in codes}
    return next((c for c in PRIMARY_ORDER if c in cats), None)


def summarize(results: Sequence[CaseResult], per_morning_manager: Sequence[float]) -> dict:
    placements = [r for r in results if r.placed]
    bad = [r for r in placements if r.violation]
    primary = Counter(primary_category(r.codes) for r in bad)
    any_cat = Counter(cat for r in bad for cat in {CATEGORY_OF[Code(c)] for c in r.codes})
    by_code = Counter(c for r in bad for c in r.codes)
    minutes = [r.minutes for r in results]
    covered = [r for r in results if r.outcome in ("covered", "merged", "reserve", "overtime")]
    cases = len(results)
    return {
        "cases": cases,
        "placements": len(placements),
        "violations": len(bad),
        "violations_pct_of_placements": round(100 * len(bad) / len(placements), 1) if placements else None,
        "violations_by_reason": {c: primary.get(c, 0) for c in PRIMARY_ORDER},
        "violations_any_reason": {c: any_cat.get(c, 0) for c in PRIMARY_ORDER},
        "violations_by_code": dict(sorted(by_code.items())),
        "expired_certificate_placements": sum(1 for r in bad if EXPIRED_CODES & set(r.codes)),
        "median_minutes_to_close": _median(minutes),
        "p90_minutes_to_close": _p90(minutes),
        "mean_minutes_to_close": round(statistics.fmean(minutes), 1) if minutes else None,
        "closed_after_shift_start": sum(1 for r in results if r.late),
        "closed_without_human": sum(1 for r in results if not r.human),
        "closed_without_human_pct": round(100 * sum(1 for r in results if not r.human) / cases, 1) if cases else None,
        "covered": len(covered),
        "lessons_cancelled": sum(r.groups_cancelled for r in results),
        "participants_without_lesson": sum(r.participants_cancelled for r in results),
        "shifts_without_groups_left_empty": sum(1 for r in results if r.outcome == "uncovered"),
        "approved_overtime": sum(1 for r in results if r.approved_overtime),
        "outcomes": dict(sorted(Counter(r.outcome for r in results).items())),
        "channels": dict(sorted(Counter(r.channel for r in results).items())),
        "asked_total": sum(r.asked for r in results),
        "manager_minutes_total": round(sum(r.manager_minutes for r in results), 1),
        "manager_minutes_median_per_morning": _median(per_morning_manager),
    }


def _iso(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).isoformat(timespec="minutes")


def _case_row(r: CaseResult, tz: ZoneInfo) -> dict:
    return {
        "shift_id": r.shift_id,
        "lesson": r.lesson,
        "outcome": r.outcome,
        "channel": r.channel,
        "staff_id": r.staff_id,
        "human": r.human,
        "closed_at": _iso(r.closed_at, tz),
        "minutes": r.minutes,
        "late": r.late,
        "violation": primary_category(r.codes),
        "codes": list(r.codes),
        "approved_overtime": r.approved_overtime,
        "lessons_cancelled": r.groups_cancelled,
        "asked": r.asked,
        "manager_minutes": r.manager_minutes,
    }


def label(mornings: int = MORNINGS, seed: int = SEED) -> str:
    return f"synthetic school, {mornings} mornings, seed {seed}"


def command(mornings: int = MORNINGS, seed: int = SEED) -> str:
    return f"uv run python -m coachcover.sim --mornings {mornings} --seed {seed} --out docs/sim"


WHAT_IF = {
    "baseline_manager_checks_role": (
        "Baseline where the manager takes only people who hold the shift's role (job title); "
        "cards, programs, hours and the group norm are still not checked.",
        {"baseline_checks_role": True},
        BASELINE,
    ),
    "coachcover_one_offer_at_a_time": (
        "CoachCover before 2026-11-07: one offer at a time, plan B only when the queue is empty "
        "(the 'before' column of the before/after table).",
        {"offers": "one"},
        COACHCOVER,
    ),
}


def _replay(world: World, mornings: int, seed: int, params: SimParams, keep_rows: bool) -> tuple[list, dict, dict]:
    tz = world.tz
    rows, results = [], {BASELINE: [], COACHCOVER: []}
    manager = {BASELINE: [], COACHCOVER: []}
    for i in range(mornings):
        m = make_morning(world, i, seed, params)
        runs = {BASELINE: run_baseline(world, m, params), COACHCOVER: run_coachcover(world, m, params)}
        for key, items in runs.items():
            results[key].extend(items)
            manager[key].append(sum(r.manager_minutes for r in items))
        if keep_rows:
            rows.append({
                "index": i,
                "day": m.day.isoformat(),
                "callouts": [{"staff_id": c.staff_id, "at": _iso(c.at, tz), "shift_ids": list(c.shift_ids)} for c in m.callouts],
                "changes": list(m.changes),
                BASELINE: [_case_row(r, tz) for r in runs[BASELINE]],
                COACHCOVER: [_case_row(r, tz) for r in runs[COACHCOVER]],
            })
    return rows, results, manager


def run(mornings: int = MORNINGS, seed: int = SEED, params: SimParams = SimParams(), what_if: bool = True) -> dict:
    """The whole replay. Deterministic: the same arguments give the same dict."""
    if not isinstance(mornings, int) or isinstance(mornings, bool) or mornings < 1:
        raise ValueError("mornings must be an int >= 1")
    if params.offers not in ("waves", "one"):
        raise ValueError("offers must be 'waves' or 'one'")
    world = load_world()
    tz = world.tz
    rows, results, manager = _replay(world, mornings, seed, params, keep_rows=True)
    variants = {}
    if what_if:
        for name, (text, change, key) in WHAT_IF.items():
            _, other, other_manager = _replay(world, mornings, seed, replace(params, **change), keep_rows=False)
            variants[name] = {"text": text, "params": change, "strategy": key,
                              "summary": summarize(other[key], other_manager[key])}
    policy = world.policy
    result = {
        "label": label(mornings, seed),
        "synthetic": True,
        "version": SIM_VERSION,
        "seed": seed,
        "mornings": mornings,
        "command": command(mornings, seed),
        "school": {"preset": policy.preset.code, "venues": len(world.venues), "staff": len(world.staff),
                   "shifts": len(world.shifts), "week_start": world.week_start.isoformat(), "tz": str(tz)},
        "policy": {"offer_timeout_minutes": policy.offer_timeout_minutes, "max_weekly_hours": policy.max_weekly_hours,
                   "offer_wave_size": policy.preset.offer_wave_size,
                   "offer_collect_seconds": policy.preset.offer_collect_seconds,
                   "plan_b_minutes_before_start": policy.preset.plan_b_minutes_before_start,
                   "autonomy": dict(policy.autonomy)},
        "params": _jsonable(asdict(params)),
        "assumptions": ASSUMPTIONS,
        "summary": {key: summarize(results[key], manager[key]) for key in (BASELINE, COACHCOVER)},
        "what_if": variants,
        "mornings_detail": rows,
    }
    result["before_after"] = before_after(result)
    return result


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def stats_block(result: dict) -> dict:
    """The `sim` block of GET /api/stats (docs/API-CONTRACT-2026-11-06.md)."""
    b, c = result["summary"][BASELINE], result["summary"][COACHCOVER]
    return {
        "label": result["label"],
        "synthetic": True,
        "mornings": result["mornings"],
        "seed": result["seed"],
        "cases": b["cases"],
        "baseline_violations": b["violations"],
        "coachcover_violations": c["violations"],
        "baseline_placements": b["placements"],
        "coachcover_placements": c["placements"],
        "baseline_violations_by_reason": b["violations_by_reason"],
        "coachcover_violations_by_reason": c["violations_by_reason"],
        "baseline_expired_certificate_placements": b["expired_certificate_placements"],
        "baseline_median_minutes_to_close": b["median_minutes_to_close"],
        "coachcover_median_minutes_to_close": c["median_minutes_to_close"],
        "baseline_closed_without_human_pct": b["closed_without_human_pct"],
        "coachcover_closed_without_human_pct": c["closed_without_human_pct"],
        "baseline_lessons_cancelled": b["lessons_cancelled"],
        "coachcover_lessons_cancelled": c["lessons_cancelled"],
        "baseline_closed_after_shift_start": b["closed_after_shift_start"],
        "coachcover_closed_after_shift_start": c["closed_after_shift_start"],
        "baseline_manager_minutes_median_per_morning": b["manager_minutes_median_per_morning"],
        "coachcover_manager_minutes_median_per_morning": c["manager_minutes_median_per_morning"],
        "baseline_violations_if_manager_checks_role": (
            result.get("what_if", {}).get("baseline_manager_checks_role", {}).get("summary", {}).get("violations")
        ),
        "command": result["command"],
    }


@lru_cache(maxsize=4)
def cached_run(mornings: int = MORNINGS, seed: int = SEED) -> dict:
    return run(mornings, seed)


def sim_stats(mornings: int = MORNINGS, seed: int = SEED) -> dict:
    """For /api/stats: the block from a real in-process run (cached; the image has no docs/ folder)."""
    return stats_block(cached_run(mornings, seed))


# --- report ---------------------------------------------------------------------------

_ROWS = (
    ("Placements of a person without permission", "violations"),
    ("  of them: no role for the shift", ("violations_by_reason", "role")),
    ("  of them: certificate expired, missing or unconfirmed", ("violations_by_reason", "certificate")),
    ("  of them: no program or level, renewal overdue", ("violations_by_reason", "program")),
    ("  of them: over the weekly hours limit", ("violations_by_reason", "hours")),
    ("  of them: group norm broken", ("violations_by_reason", "ratio")),
    ("Placements with an expired certificate (any reason list)", "expired_certificate_placements"),
    ("People placed (cover, merge, reserve, overtime)", "placements"),
    ("Median minutes from call-out to a closed shift", "median_minutes_to_close"),
    ("90th percentile, minutes", "p90_minutes_to_close"),
    ("Closed after the shift had started", "closed_after_shift_start"),
    ("Closed without a person (no manager step), %", "closed_without_human_pct"),
    ("Lessons cancelled or moved", "lessons_cancelled"),
    ("Participants left without their lesson", "participants_without_lesson"),
    ("Shifts without groups left empty", "shifts_without_groups_left_empty"),
    ("Approved overtime", "approved_overtime"),
    ("Manager minutes, median per morning", "manager_minutes_median_per_morning"),
)


def _value(summary: dict, key) -> object:
    if isinstance(key, tuple):
        return summary[key[0]][key[1]]
    return summary[key]


def _cell(value: object) -> str:
    return "—" if value is None else str(value)


def table_markdown(result: dict) -> str:
    b, c = result["summary"][BASELINE], result["summary"][COACHCOVER]
    lines = [f"| Metric | {STRATEGY_LABEL[BASELINE]} | {STRATEGY_LABEL[COACHCOVER]} |", "|---|---:|---:|"]
    lines.append(f"| Shifts that needed cover | {b['cases']} | {c['cases']} |")
    for title, key in _ROWS:
        lines.append(f"| {title} | {_cell(_value(b, key))} | {_cell(_value(c, key))} |")
    return "\n".join(lines)


_WHAT_IF_ROWS = (
    ("Placements without permission", "violations"),
    ("  no role / certificate / program / hours / group norm", None),
    ("Median minutes to close", "median_minutes_to_close"),
    ("90th percentile, minutes", "p90_minutes_to_close"),
    ("Closed after the shift had started", "closed_after_shift_start"),
    ("Closed without a person, %", "closed_without_human_pct"),
)


def what_if_markdown(result: dict) -> str:
    variants = result.get("what_if") or {}
    if not variants:
        return "Not computed in this run."
    names = list(variants)
    head = "| Metric | " + " | ".join(f"`{n}`" for n in names) + " |"
    lines = [head, "|---|" + "---:|" * len(names)]
    for title, key in _WHAT_IF_ROWS:
        cells = []
        for n in names:
            s = variants[n]["summary"]
            if key is None:
                r = s["violations_by_reason"]
                cells.append(" / ".join(str(r[c]) for c in ("role", "certificate", "program", "hours", "ratio")))
            else:
                cells.append(_cell(s[key]))
        lines.append(f"| {title} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.extend(f"- `{n}`: {variants[n]['text']}" for n in names)
    return "\n".join(lines)


BEFORE_AFTER_ROWS = (
    ("Shifts that needed cover", "cases"),
    ("People placed", "placements"),
    ("Placements that break a rule", "violations"),
    ("Placements with an expired card", "expired_certificate_placements"),
    ("Minutes from call-out to closed, median", "median_minutes_to_close"),
    ("Minutes from call-out to closed, p90", "p90_minutes_to_close"),
    ("Minutes from call-out to closed, mean", "mean_minutes_to_close"),
    ("Closed after the shift started", "closed_after_shift_start"),
    ("Closed without a person", "closed_without_human"),
    ("Closed without a person, %", "closed_without_human_pct"),
    ("Shifts covered (offer or plan B)", "covered"),
    ("Lessons cancelled or moved", "lessons_cancelled"),
    ("Children without their lesson", "participants_without_lesson"),
    ("Shifts without groups left empty", "shifts_without_groups_left_empty"),
    ("Approved overtime", "approved_overtime"),
    ("Offers sent (people asked)", "asked_total"),
    ("Manager minutes, total", "manager_minutes_total"),
)
BEFORE_KEY = "coachcover_one_offer_at_a_time"


def before_after(result: dict) -> dict | None:
    """CoachCover one offer at a time (before) against waves + plan B by the deadline (after, the core as built)."""
    before = (result.get("what_if") or {}).get(BEFORE_KEY)
    if before is None:
        return None
    b, a = before["summary"], result["summary"][COACHCOVER]
    return {"before": "one offer at a time, plan B when the queue is empty",
            "after": "waves of 3, highest yes in 60 s, plan B 20 min before the start",
            "rows": [[title, b[key], a[key]] for title, key in BEFORE_AFTER_ROWS],
            "outcomes": {"before": b["outcomes"], "after": a["outcomes"]}}


def before_after_markdown(result: dict) -> str:
    table = before_after(result)
    if table is None:
        return "Not computed in this run."
    lines = [f"| Metric | Before: {table['before']} | After: {table['after']} |", "|---|---:|---:|"]
    lines += [f"| {title} | {_cell(b)} | {_cell(a)} |" for title, b, a in table["rows"]]
    lines += ["", f"Outcomes before: {table['outcomes']['before']}; after: {table['outcomes']['after']}."]
    return "\n".join(lines)


def report_markdown(result: dict) -> str:
    b, c = result["summary"][BASELINE], result["summary"][COACHCOVER]
    p = result["params"]
    out = [
        "# 50 training mornings: group chat + phone tree vs CoachCover",
        "",
        f"**{result['label']}.** Every number below comes from one run of `coachcover/sim.py`; nothing is typed by hand.",
        f"Repeat: `{result['command']}` (same seed, same numbers).",
        "",
        "The school is fictional (`coachcover/seed.py`): "
        f"{result['school']['venues']} venues, {result['school']['staff']} staff, {result['school']['shifts']} shifts in the week of "
        f"{result['school']['week_start']}, time zone {result['school']['tz']}. Each morning is a weekday of that week with 1-3 "
        "call-outs before 06:00 and a changed staff state; both strategies run on the same morning.",
        "",
        "## Result",
        "",
        table_markdown(result),
        "",
        f"Outcomes, baseline: {b['outcomes']}; CoachCover: {c['outcomes']}.",
        f"Blocking codes behind the baseline violations: {b['violations_by_code']}.",
        "",
        "A placement is one person put on a shift (cover, the teacher of a merged group, a reserve person, approved "
        "overtime). It is a violation when `rules.eligible` returns a blocking reason for that person and shift at the "
        "moment of booking. The breakdown counts each violation once, under the first of: role, certificate, program, "
        "hours, group norm. CoachCover's approved overtime is not counted as a violation: the manager approved it with "
        "its price, as the core does.",
        "",
        "## The two strategies",
        "",
        f"- **{STRATEGY_LABEL[BASELINE]}.** The manager sees the call-out after {p['manager_notice_minutes']} min, posts in the "
        f"staff chat ({p['chat_post_minutes']} min per post) and books the first \"yes\" that comes before the shift starts "
        f"(+{p['confirm_minutes']} min). After {p['chat_wait_minutes']} min without a \"yes\" the manager also phones free "
        f"people who hold the shift's role, in a random order, {p['call_minutes']} min per call. Cards, programs, weekly hours "
        "and the group norm are not checked: that is how a chat and a general scheduler work. Nobody by the start: a lesson "
        "goes into the group next to it without checking the norm, otherwise it is cancelled; a shift without groups stays empty.",
        f"- **{STRATEGY_LABEL[COACHCOVER]}.** {p['intake_minutes']} min after the call-out the case opens. The rules rank every "
        "non-reserve person; offers go to eligible people only, in waves of "
        f"{result['policy']['offer_wave_size']} in queue order, with the policy's timer "
        f"({result['policy']['offer_timeout_minutes']} min); the highest \"yes\" wins once nobody above it can still answer, "
        f"at most {result['policy']['offer_collect_seconds']} s after the first \"yes\"; the rules run again at \"yes\". "
        f"Plan B goes to the manager {result['policy']['plan_b_minutes_before_start']} min before the shift starts even while "
        "offers are out (a \"yes\" before the manager acts still covers the shift), or earlier when nobody is left, in the "
        "core's order: merge into the group next to it within the norm, a reserve person, overtime (an offer to a person blocked "
        "only by hours), move the lesson, close. Plan B is `propose` in the policy, so the manager approves it "
        f"({p['manager_notice_minutes']} min to see the card, {p['decide_minutes']} min to decide). If an overtime offer is not "
        "taken, the manager goes to the next option.",
        "",
        "## Before / after: offer waves",
        "",
        "CoachCover on the same 50 mornings, before (one offer at a time) and after (waves + plan B by the deadline). "
        "The baseline does not change.",
        "",
        before_after_markdown(result),
        "",
        "## What if (not the main table)",
        "",
        what_if_markdown(result),
        "",
        "## Assumptions",
        "",
        "Every value is an assumption of this replay, not a measurement of a real school.",
        "",
        "| Parameter | Value | Meaning |",
        "|---|---|---|",
    ]
    for key, text in result["assumptions"].items():
        out.append(f"| `{key}` | {p[key]} | {text} |")
    out += [
        "",
        f"Policy of the preset: offer timer {result['policy']['offer_timeout_minutes']} min, weekly limit "
        f"{result['policy']['max_weekly_hours']} h, autonomy {result['policy']['autonomy']}.",
        "",
        "## Limits of this replay",
        "",
        "- Synthetic data: one fictional school, one week, changed per morning. Real numbers need a real school's schedule.",
        "- The cases of one morning are worked one after another in call-out order; a person booked for an earlier case is busy for a later one that overlaps.",
        "- People answer the chat, a call and an offer with the same delay and the same wish; the chat reaches everyone at once, CoachCover asks a wave of three at a time.",
        "- The baseline manager never checks anything. A careful manager would check some things; the replay counts what nobody checks.",
        "- A shift counts as closed when a person is booked or the fallback is decided; the time runs from the call-out.",
        "- In plan B a reserve person is booked on the manager's approval without being asked, as the core does; the baseline manager never calls a reserve person.",
        "- \"Closed without a person\" is 0 for the baseline by construction: the manager books every shift by hand.",
        "- Plan B by the deadline: the manager acts on it after noticing the card; the offers of the open wave keep running until then.",
        "",
    ]
    return "\n".join(out)


def write(result: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / f"{OUT_NAME}.json", out_dir / f"{OUT_NAME}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    md_path.write_text(report_markdown(result), encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay synthetic mornings: group chat + phone tree vs CoachCover.")
    parser.add_argument("--mornings", type=int, default=MORNINGS)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=Path, default=None, help="folder for SIM-50-MORNINGS.json/.md (default: print only)")
    args = parser.parse_args(argv)
    result = run(args.mornings, args.seed)
    print(result["label"])
    print(table_markdown(result))
    if args.out is not None:
        for path in write(result, args.out):
            print("wrote", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
