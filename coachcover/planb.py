"""Plan B options for an uncovered shift. Pure: no database, no network; the pipeline and the replay both call it.

Order: merge into a concurrent group within the norm, a reserve person steps in, overtime (priced), move the
lesson (makeup), close (the preset's remedy). Each option carries its autonomy mode: the strictest of the
policy's plan_b and the option's own action; overtime is never automatic.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Collection, Mapping, Sequence
from zoneinfo import ZoneInfo

from coachcover.model import Context, Policy, Shift, Staff
from coachcover.rules import Code, assess, eligible, group_norm, rank

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
