"""Money on the screen: what an uncovered morning costs, and what one cover costs in model calls.

Every price comes from the preset's `money` block and says where it comes from. Most are assumptions:
no real school's price list was available, so the preset's values are conservative placeholders,
marked `assumption: true`, to be replaced by the school's own numbers. The hourly wage follows the
policy's `overtime_cost_per_hour`, so the money card and the plan B overtime option show the same rate.

- `uncovered_cost`: refunds for the lessons that would not run (participants x
  price x refund share), overtime for the shifts without groups someone would
  have to stay on for (hours x wage x 1.5), and an optional fine or closure cost
  (0 by default). Pure.
- `calls_cost`: the model bill of a set of ledger rows; token-priced models are
  recomputed as model x tokens x price from `model_prices.json`, audio models use
  the ledger's own cost (the ledger keeps no audio seconds). Pure.
- `case_cost`, `today_money`: the same from the database, for GET /api/today.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from coachcover.model import MONEY_KEYS, Shift
from coachcover.models import call_cost, load_prices

CURRENCY = "USD"


@dataclass(frozen=True)
class Price:
    value: float
    unit: str
    note: str
    source: str | None = None
    assumption: bool = True  # not checked against a real school

    def __post_init__(self) -> None:
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool) or self.value < 0:
            raise ValueError(f"price {self.unit!r}: value must be a number >= 0")

    def as_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "assumption": self.assumption, "source": self.source,
                "note": self.note}


@dataclass(frozen=True)
class MoneyParams:
    lesson_price: Price  # per participant per lesson
    refund_share: Price  # 0..1 of the lesson price given back as a refund or credit
    hourly_wage: Price
    overtime_multiplier: Price
    inspection_penalty: Price  # per uncovered morning
    manager_hour: Price

    def __post_init__(self) -> None:
        if self.refund_share.value > 1:
            raise ValueError("refund_share must be between 0 and 1")
        if self.overtime_multiplier.value < 1:
            raise ValueError("overtime_multiplier must be >= 1")

    @property
    def overtime_hour_usd(self) -> float:
        return round(self.hourly_wage.value * self.overtime_multiplier.value, 2)

    def as_dict(self) -> dict:
        return {name: getattr(self, name).as_dict() for name in
                ("lesson_price", "refund_share", "hourly_wage", "overtime_multiplier", "inspection_penalty", "manager_hour")}


def _price(entry: Mapping[str, Any]) -> Price:
    return Price(float(entry["value"]), entry["unit"], entry["note"], entry.get("source"), bool(entry["assumption"]))


def money_params(prices: Mapping[str, Mapping[str, Any]], overtime_cost_per_hour: float | None = None,
                 **values: float) -> MoneyParams:
    """Prices from the preset's `money` block, with the policy's overtime price when given; keyword values
    replace a price's value (a replaced value keeps its unit and is marked as the school's own number)."""
    if not prices:
        raise ValueError("the preset has no money block")
    params = MoneyParams(**{name: _price(prices[name]) for name in MONEY_KEYS})
    if overtime_cost_per_hour is not None:
        multiplier = params.overtime_multiplier.value
        params = replace(params, hourly_wage=Price(
            overtime_cost_per_hour / multiplier, "USD per hour",
            f"Derived from the policy's overtime price (overtime_cost_per_hour / {multiplier:g}); the preset calls that "
            "price an example, so this is an assumption too.",
            f"assumption: policy overtime_cost_per_hour / {multiplier:g}", True))
    unknown = set(values) - set(params.as_dict())
    if unknown:
        raise ValueError(f"unknown money parameters {sorted(unknown)}")
    changed = {k: replace(getattr(params, k), value=v, source="set by the school", assumption=False)
               for k, v in values.items()}
    return replace(params, **changed)


def usd(value: float) -> str:
    rounded = round(value, 2)
    return f"${rounded:,.0f}" if rounded == int(rounded) else f"${rounded:,.2f}"


def cents_label(cents: float) -> str:
    if cents >= 1:
        return f"{cents:.2f}¢"
    return f"{cents:.3f}¢" if cents >= 0.001 else f"{cents:.4f}¢"


# --- the cost of an uncovered morning --------------------------------------------


def _hours(shift: Shift) -> float:
    # in UTC: datetimes sharing one tzinfo subtract by wall clock, wrong across DST
    return round((shift.end.astimezone(UTC) - shift.start.astimezone(UTC)).total_seconds() / 3600, 2)


def uncovered_cost(shifts: Sequence[Shift], params: MoneyParams) -> dict:
    """What the morning costs if none of these shifts finds a person."""
    lines = []
    ids = [s.id for s in shifts]
    if len(set(ids)) != len(ids):
        raise ValueError("the same shift is listed twice")
    for shift in sorted(shifts, key=lambda s: (s.start.astimezone(UTC), s.id)):
        if shift.groups:
            participants = sum(g.size for g in shift.groups)
            amount = participants * params.lesson_price.value * params.refund_share.value
            lines.append({
                "kind": "refunds", "shift_id": shift.id, "participants": participants,
                "usd": round(amount, 2),
                "label": f"Refunds: {participants} participants × {usd(params.lesson_price.value)}"
                         + ("" if params.refund_share.value == 1 else f" × {params.refund_share.value:g}"),
            })
        else:
            hours = _hours(shift)
            amount = hours * params.overtime_hour_usd
            lines.append({
                "kind": "overtime", "shift_id": shift.id, "hours": hours, "usd": round(amount, 2),
                "label": f"Overtime: {hours:g} h × {usd(params.hourly_wage.value)} × {params.overtime_multiplier.value:g}",
            })
    if shifts and params.inspection_penalty.value > 0:
        lines.append({"kind": "inspection", "shift_id": None, "usd": round(params.inspection_penalty.value, 2),
                      "label": f"Inspection fine or closure: {usd(params.inspection_penalty.value)}"})
    total = round(sum(line["usd"] for line in lines), 2)
    return {
        "total_usd": total,
        "participants": sum(line.get("participants", 0) for line in lines),
        "overtime_hours": round(sum(line.get("hours", 0) for line in lines), 2),
        "lines": lines,
    }


# --- the cost of a cover in model calls ---------------------------------------------


def call_price(call: Mapping[str, Any], prices: Mapping[str, Mapping[str, float]]) -> tuple[float | None, str]:
    """(usd, how): token models recomputed from the price table, audio models from the ledger's cost."""
    model = call.get("model_returned") or call.get("model_requested")
    price = prices.get(model) or {}
    tokens_in, tokens_out = call.get("input_tokens"), call.get("output_tokens")
    if "input_per_1m_tokens" in price and tokens_in is not None and tokens_out is not None:
        return call_cost(dict(prices), model, tokens_in, tokens_out, None), "tokens x price"
    stored = call.get("cost_usd")
    if stored is not None:
        return float(stored), "ledger"
    return None, "unpriced"


def calls_cost(calls: Iterable[Mapping[str, Any]], prices: Mapping[str, Mapping[str, float]] | None = None,
               weight: float = 1.0) -> dict:
    """The bill of these ledger rows; `weight` scales a share (a call-out's calls split between its cases)."""
    prices = load_prices() if prices is None else prices
    total, count, unpriced, recorded, synthetic = 0.0, 0, 0, 0, 0
    per_model: dict[str, dict] = {}
    for call in calls:
        count += 1
        value, _ = call_price(call, prices)
        model = call.get("model_returned") or call.get("model_requested") or "?"
        row = per_model.setdefault(model, {"calls": 0, "usd": 0.0})
        row["calls"] += 1
        if value is None:
            unpriced += 1
            continue
        row["usd"] += value * weight
        total += value * weight
        recorded += call.get("source") == "recorded"
        synthetic += bool(call.get("synthetic"))
    for row in per_model.values():
        row["usd"] = round(row["usd"], 8)
    return {"usd": round(total, 8), "cents": round(total * 100, 6), "calls": count, "unpriced": unpriced,
            "recorded": recorded, "synthetic": synthetic, "per_model": dict(sorted(per_model.items()))}


def _merge_bills(*bills: dict) -> dict:
    per_model: dict[str, dict] = {}
    for bill in bills:
        for model, row in bill["per_model"].items():
            acc = per_model.setdefault(model, {"calls": 0, "usd": 0.0})
            acc["calls"] += row["calls"]
            acc["usd"] = round(acc["usd"] + row["usd"], 8)
    total = round(sum(b["usd"] for b in bills), 8)
    return {"usd": total, "cents": round(total * 100, 6), "calls": sum(b["calls"] for b in bills),
            "unpriced": sum(b["unpriced"] for b in bills), "recorded": sum(b["recorded"] for b in bills),
            "synthetic": sum(b["synthetic"] for b in bills), "per_model": dict(sorted(per_model.items()))}


# --- from the database ---------------------------------------------------------------

_CALL_COLUMNS = ("model_requested", "model_returned", "source", "ok", "input_tokens", "output_tokens", "cost_usd",
                 "synthetic")
_CALL_SELECT = (
    "SELECT model_requested, model_returned, source, ok, input_tokens, output_tokens, cost_usd,"
    " coalesce((ref->>'synthetic')::boolean, false) FROM model_call"
)


def case_cost(conn, case_id: int, prices: Mapping | None = None) -> dict:
    """Model cost of one cover: the case's own calls plus its share of the call-out's calls
    (transcript and reading the message are made before the cases exist and serve all of them)."""
    row = conn.execute("SELECT callout_id FROM cover_case WHERE id = %s", (case_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown case {case_id}")
    own = [dict(zip(_CALL_COLUMNS, r)) for r in conn.execute(f"{_CALL_SELECT} WHERE case_id = %s ORDER BY id", (case_id,))]
    bill = calls_cost(own, prices)
    shared_between, shared_calls = 0, 0
    if row[0] is not None:
        callout = str(row[0])
        shared_between = conn.execute("SELECT count(*) FROM cover_case WHERE callout_id = %s", (row[0],)).fetchone()[0]
        shared = [dict(zip(_CALL_COLUMNS, r)) for r in conn.execute(
            f"{_CALL_SELECT} WHERE case_id IS NULL AND ref->>'callout_id' = %s ORDER BY id", (callout,))]
        shared_calls = len(shared)
        if shared and shared_between:
            bill = _merge_bills(bill, calls_cost(shared, prices, weight=1 / shared_between))
    return {"case_id": case_id, **bill, "own_calls": len(own), "shared_calls": shared_calls,
            "shared_between_cases": shared_between}


def _today_cases(conn, policy_id: str, now: datetime) -> list[tuple[int, str]]:
    tz = ZoneInfo(conn.execute("SELECT tz FROM venue WHERE policy_id = %s ORDER BY id LIMIT 1", (policy_id,)).fetchone()[0])
    day_from = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return conn.execute(
        "SELECT c.id, c.shift_id FROM cover_case c JOIN shift s ON s.id = c.shift_id JOIN venue v ON v.id = s.venue_id"
        " WHERE v.policy_id = %s AND c.opened_at >= %s AND c.opened_at <= %s ORDER BY c.opened_at, c.id",
        (policy_id, day_from, now),
    ).fetchall()


def summary_lines(uncovered: dict, params: MoneyParams) -> list[dict]:
    """The cost lines added up by kind, for a card (the per-shift lines stay in `uncovered["lines"]`)."""
    out = []
    refunds = [line for line in uncovered["lines"] if line["kind"] == "refunds"]
    if refunds:
        n = sum(line["participants"] for line in refunds)
        share = "" if params.refund_share.value == 1 else f" × {params.refund_share.value:g}"
        out.append({"kind": "refunds", "usd": round(sum(line["usd"] for line in refunds), 2),
                    "label": f"Refunds: {n} participants × {usd(params.lesson_price.value)}{share}"})
    overtime = [line for line in uncovered["lines"] if line["kind"] == "overtime"]
    if overtime:
        hours = round(sum(line["hours"] for line in overtime), 2)
        out.append({"kind": "overtime", "usd": round(sum(line["usd"] for line in overtime), 2),
                    "label": f"Overtime: {hours:g} h × {usd(params.hourly_wage.value)} × {params.overtime_multiplier.value:g}"})
    out.extend({"kind": line["kind"], "usd": line["usd"], "label": line["label"]}
               for line in uncovered["lines"] if line["kind"] == "inspection")
    return out


def nobody_accepted_card(uncovered: dict, params: MoneyParams, *, case_bill: dict | None = None,
                         manager_minutes: float | None = None, manager_minutes_source: str | None = None) -> dict:
    """The "If nobody had accepted" block: the cost lines, then what CoachCover spent instead."""
    compare = []
    if case_bill is not None:
        how = [f"{case_bill['own_calls']} own call(s)"] if case_bill.get("own_calls") else []
        if case_bill.get("shared_calls"):
            how.append(f"1/{case_bill['shared_between_cases']} of {case_bill['shared_calls']} call-out call(s)")
        compare.append({
            "kind": "models",
            "label": f"Models for this cover: {cents_label(case_bill['cents'])}" + (f" ({' + '.join(how)})" if how else ""),
            "cents": case_bill["cents"],
        })
    if manager_minutes is not None:
        cost = manager_minutes / 60 * params.manager_hour.value
        compare.append({
            "kind": "manager",
            "label": f"Manager on the chat and phone: {manager_minutes:g} min ≈ {usd(cost)} at "
                     f"{usd(params.manager_hour.value)}/h" + (f" ({manager_minutes_source})" if manager_minutes_source else ""),
            "usd": round(cost, 2),
            "minutes": manager_minutes,
        })
    return {
        "title": "If nobody had accepted",
        "total_usd": uncovered["total_usd"],
        "total_label": usd(uncovered["total_usd"]),
        "lines": summary_lines(uncovered, params),
        "compare": compare,
        "assumptions": params.as_dict(),
        "note": "Prices are assumptions until the school sets its own.",
    }


def today_money(conn, policy_id: str, now: datetime, params: MoneyParams | None = None, *,
                manager_minutes: float | None = None, manager_minutes_source: str | None = None) -> dict:
    """`money` for GET /api/today plus the "If nobody had accepted" card, for the cases opened today up to `now`.

    The cost is of today's call-out shifts as if nobody had taken them; the cover cost is the first case of the
    day (the one the screen shows). Without `manager_minutes` the card has no manager line.
    """
    from coachcover.repo import load_policy, load_shift  # the database layer only when it is used

    if params is None:
        policy = load_policy(conn, policy_id)
        params = money_params(policy.preset.money, policy.overtime_cost_per_hour)
    cases = _today_cases(conn, policy_id, now)
    shifts = [load_shift(conn, sid) for sid in dict.fromkeys(sid for _, sid in cases)]
    uncovered = uncovered_cost(shifts, params)
    bill = case_cost(conn, cases[0][0]) if cases else None
    if cases:
        parts = [line["label"] for line in summary_lines(uncovered, params)]
        label = f"If nobody had accepted: {usd(uncovered['total_usd'])}" + (f" ({'; '.join(parts)})" if parts else "")
    else:
        label = "No call-outs today"
    money = {
        "uncovered_morning_usd": uncovered["total_usd"],
        "uncovered_morning_label": label,
        "case_cost_cents": round(bill["cents"], 4) if bill else None,
        "manager_hour_usd": params.manager_hour.value,
    }
    card = nobody_accepted_card(uncovered, params, case_bill=bill, manager_minutes=manager_minutes,
                                manager_minutes_source=manager_minutes_source)
    return {"money": money, "card": card, "uncovered": uncovered, "case_cost": bill}
