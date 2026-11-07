"""Plan B options for an uncovered shift. Pure: no database, no network; the pipeline and the replay both call it.

Order: merge into a concurrent group within the norm, a reserve person steps in, overtime (priced), move the
lesson (makeup), close (the preset's remedy). Each option carries its autonomy mode: the strictest of the
policy's plan_b and the option's own action; overtime is never automatic.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Callable, Collection, Mapping, Sequence
from zoneinfo import ZoneInfo

from coachcover.model import Context, Policy, Shift, Staff
from coachcover.rules import Code, assess, eligible, group_norm, rank, ratio_reason

MODE_ORDER = {"auto": 0, "propose": 1, "off": 2}
OPTION_ORDER = ("merge", "lead", "overtime", "reschedule", "close")


def strictest(*modes: str) -> str:
    return max(modes, key=MODE_ORDER.__getitem__)


def plan_b_options(
    *,
    shift: Shift,
    absent: str | None,
    partners: Sequence[Shift],
    staff: Mapping[str, Staff],
    policy: Policy,
    context: Context,
    now: datetime,
    exclude: Collection[str] = (),
) -> tuple[list[dict], list[dict]]:
    """(options in order, refused merges). `partners` are scheduled shifts with groups at the same venue, role,
    start and end, each with its holder as `assignee`. `exclude`: people already asked in this case (they said
    no, did not answer or were withdrawn): never suggested again, not even as overtime."""
    auto = policy.autonomy
    preset = policy.preset
    tz = ZoneInfo(context.venues[shift.venue].tz)
    group_ids = [g.id for g in shift.groups]
    families = sum(g.size for g in shift.groups)
    options: list[dict] = []
    rejected: list[dict] = []
    if shift.groups and auto["merge_groups"] != "off":
        for partner in partners:
            teacher = staff.get(partner.assignee)
            if teacher is None:
                continue
            merged = dataclasses.replace(partner, groups=partner.groups + shift.groups, assignee=None,
                                         merged_shift_ids=(shift.id,))
            decision = eligible(teacher, merged, now, policy, context)
            participants = sum(g.size for g in merged.groups)
            if decision.ok:
                per = min(group_norm(g, preset) for g in merged.groups)
                options.append({
                    "kind": "merge", "mode": strictest(auto["plan_b"], auto["merge_groups"]),
                    "into_shift_id": partner.id, "staff_id": teacher.id, "staff_name": teacher.name,
                    "groups": [g.id for g in merged.groups], "participants": participants,
                    "capacity": per * merged.instructors,
                })
            else:
                rejected.append({"kind": "merge", "into_shift_id": partner.id, "participants": participants,
                                 "codes": [r.code.value for r in decision.reasons if r.blocking]})
    reserve_roles = {r.code for r in preset.roles.values() if r.reserve}
    leads = [s for s in staff.values() if s.roles & reserve_roles and s.id != absent and s.id not in exclude]
    lead_rank = rank(assess(s, shift, now, policy, context) for s in leads)
    ready = [r for r in lead_rank if r.candidate.decision.ok]
    if ready:
        lead = staff[ready[0].candidate.staff_id]
        options.append({"kind": "lead", "mode": auto["plan_b"], "staff_id": lead.id, "staff_name": lead.name})
    elif leads:
        rejected.append({"kind": "lead", "codes": sorted({r.code.value for x in lead_rank
                                                           for r in x.candidate.decision.reasons if r.blocking})})
    if auto["overtime"] != "off":
        people = [s for s in staff.values()
                  if not (s.roles & reserve_roles) and s.id != absent and s.id not in exclude]
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
    local_day = shift.start.astimezone(tz).date()
    makeup_until = local_day + timedelta(days=policy.makeup_window_days)
    if shift.groups:
        options.append({"kind": "reschedule", "mode": auto["plan_b"], "groups": group_ids, "families": families,
                        "remedy": "makeup", "valid_until": makeup_until})
    options.append({"kind": "close", "mode": auto["plan_b"], "zone_id": shift.zone, "groups": group_ids,
                    "families": families, "remedy": policy.default_remedy,
                    "valid_until": makeup_until if policy.default_remedy == "makeup" else None})
    options.sort(key=lambda o: OPTION_ORDER.index(o["kind"]))
    return options, rejected


RATIO_OPTION_ORDER = ("lead", "split", "move", "cancel_lesson")


def ratio_plan_options(
    *,
    seat: Shift,
    lesson: Shift,
    group_id: str,
    move: int,
    moved_families: Sequence[tuple[str, int]],
    partners: Sequence[Shift],
    later: Sequence[Shift],
    staff: Mapping[str, Staff],
    policy: Policy,
    context: Context,
    now: datetime,
    exclude: Collection[str] = (),
) -> tuple[list[dict], list[dict]]:
    """Plan B for a broken children-per-instructor norm (case source "ratio"). Only options that keep every lesson
    within its norm: (a) a reserve person takes the open instructor seat; (b) another cleared instructor teaching
    at the same time takes the `move` newest children as a group of their own level; (c) the `move` newest
    children go to the nearest lesson of their level with room. With none of these, the whole lesson is cancelled
    with credits. There is never an option that leaves the lesson over the norm.

    `seat` is the open instructor shift (same groups as `lesson`, `instructors` = the number the norm needs);
    `lesson` is the shift it was added to, with its holder as `assignee`; `partners` are scheduled shifts with
    groups at the same venue and time, `later` scheduled lessons of other groups, each with its holder."""
    auto = policy.autonomy
    preset = policy.preset
    group = next(g for g in lesson.groups if g.id == group_id)
    options: list[dict] = []
    rejected: list[dict] = []
    reserve_roles = {r.code for r in preset.roles.values() if r.reserve}
    leads = [s for s in staff.values() if s.roles & reserve_roles and s.id not in exclude]
    lead_rank = rank(assess(s, seat, now, policy, context) for s in leads)
    ready = [r for r in lead_rank if r.candidate.decision.ok]
    if ready:
        lead = staff[ready[0].candidate.staff_id]
        options.append({"kind": "lead", "mode": auto["plan_b"], "staff_id": lead.id, "staff_name": lead.name,
                        "seat_shift_id": seat.id})
    elif leads:
        rejected.append({"kind": "lead", "codes": sorted({r.code.value for x in lead_rank
                                                           for r in x.candidate.decision.reasons if r.blocking})})
    moving = dataclasses.replace(group, id=f"{group.id}-moved", size=move, families=tuple(moved_families))
    mover_mode = strictest(auto["plan_b"], auto["merge_groups"])
    if move > 0 and auto["merge_groups"] != "off":
        for partner in partners:
            teacher = staff.get(partner.assignee)
            if teacher is None or {g.id for g in partner.groups} & {g.id for g in lesson.groups}:
                continue
            taken = dataclasses.replace(partner, groups=partner.groups + (moving,), assignee=None)
            decision = eligible(teacher, taken, now, policy, context)
            if decision.ok:
                per = min(group_norm(g, preset) for g in taken.groups)
                options.append({"kind": "split", "mode": mover_mode, "into_shift_id": partner.id, "staff_id": teacher.id,
                                "staff_name": teacher.name, "children": move, "group_id": group.id,
                                "participants_after": sum(g.size for g in taken.groups),
                                "capacity": per * taken.instructors, "families": dict(moved_families)})
                break
            rejected.append({"kind": "split", "into_shift_id": partner.id,
                             "codes": [r.code.value for r in decision.reasons if r.blocking]})
        tz = ZoneInfo(context.venues[lesson.venue].tz)
        for target in sorted(later, key=lambda s: (s.venue != lesson.venue, s.start.astimezone(tz), s.id)):
            same = [g for g in target.groups if (g.program, g.level) == (group.program, group.level) and g.id != group.id]
            if not same or target.id == lesson.id:
                continue
            into = same[0]
            grown = tuple(dataclasses.replace(g, size=g.size + move) if g.id == into.id else g for g in target.groups)
            if ratio_reason(grown, target.instructors, preset) is None:
                options.append({"kind": "move", "mode": mover_mode, "to_shift_id": target.id, "to_group_id": into.id,
                                "staff_id": target.assignee, "children": move, "group_id": group.id,
                                "starts_at": target.start, "venue_id": target.venue,
                                "families": dict(moved_families)})
                break
    if not options:
        families = sum(g.size for g in lesson.groups)
        options.append({"kind": "cancel_lesson", "mode": auto["plan_b"], "shift_id": lesson.id,
                        "groups": [g.id for g in lesson.groups], "families": families, "remedy": "credit"})
    options.sort(key=lambda o: RATIO_OPTION_ORDER.index(o["kind"]))
    return options, rejected


# --- a private lesson (one child, one coach) ---------------------------------------------------------------------


def is_private(shift: Shift) -> bool:
    return bool(shift.groups) and all(g.kind == "private" for g in shift.groups)


def own_coach_slot(
    *,
    shift: Shift,
    coach: Staff,
    policy: Policy,
    context_for: Callable[[Shift], Context],
    tz: ZoneInfo,
    now: datetime,
    days: int = 7,
) -> tuple[Shift | None, list[dict]]:
    """The nearest time the family's own coach can teach the lesson instead: the same local start time on each of the
    next `days` days (the family already comes at that hour), checked by the same rules as any shift (cards, weekly
    hours, overlaps, rest, absences). Returns (the moved lesson as a shift, or None; every day tried, with why not)."""
    local = shift.start.astimezone(tz)
    length = shift.end.astimezone(UTC) - shift.start.astimezone(UTC)
    tried = []
    for d in range(1, days + 1):
        start = datetime.combine(local.date() + timedelta(days=d), local.timetz().replace(tzinfo=None), tzinfo=tz)
        if start.astimezone(UTC) <= now.astimezone(UTC):
            continue
        moved = dataclasses.replace(shift, id=f"{shift.id}-M{d}", start=start, end=(start.astimezone(UTC) + length).astimezone(tz),
                                    assignee=None, merged_shift_ids=())
        decision = eligible(coach, moved, now, policy, context_for(moved))
        tried.append({"start": start, "ok": decision.ok, "codes": [r.code.value for r in decision.reasons if r.blocking]})
        if decision.ok:
            return moved, tried
    return None, tried


def private_plan_options(*, shift: Shift, coach: Staff | None, slot: Shift | None, policy: Policy, tz: ZoneInfo,
                         price_usd: float) -> list[dict]:
    """Plan B for a private lesson nobody covers, or whose family did not answer: never another coach without the
    family's yes. (a) the family's own coach at the nearest free time; (b) a credit for one lesson."""
    auto = policy.autonomy
    options = []
    groups = [g.id for g in shift.groups]
    if coach is not None and slot is not None:
        options.append({"kind": "reschedule_own", "mode": auto["plan_b"], "staff_id": coach.id, "staff_name": coach.name,
                        "starts_at": slot.start, "ends_at": slot.end, "when": f"{slot.start.astimezone(tz):%a %H:%M}",
                        "groups": groups, "families": len(groups)})
    options.append({"kind": "credit", "mode": auto["plan_b"], "groups": groups, "families": len(groups),
                    "remedy": "credit", "amount_usd": price_usd})
    return options
