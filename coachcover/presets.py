"""Load presets (data files in coachcover/presets/) and build a policy from them.

The loader is strict: a missing or unknown key, a wrong type or a dangling
reference raises ValueError, so a typo in a preset file cannot pass silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coachcover.model import MONEY_KEYS, WEEKDAYS, CertType, Level, Policy, Preset, Program, Role, check_autonomy

PRESET_DIR = Path(__file__).parent / "presets"

_PRESET_KEYS = {"code", "label", "note", "venue_term", "cert_types", "roles", "programs", "policy_defaults"}
_PRESET_OPTIONAL = {"note", "young_children", "copy", "money", "min_rest_hours", "offers", "forecast"}
_PRICE_KEYS = {"value", "unit", "assumption", "note"}
_DEFAULT_KEYS = {
    "max_weekly_hours",
    "week_start",
    "makeup_window_days",
    "autonomy",
    "offer_timeout_minutes",
    "overtime_cost_per_hour",
    "default_remedy",
}


def _keys(obj: Any, required: set[str], where: str, optional: set[str] = frozenset()) -> dict:
    if not isinstance(obj, dict):
        raise ValueError(f"{where}: expected an object")
    missing = required - obj.keys()
    unknown = obj.keys() - required - optional
    if missing or unknown:
        raise ValueError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return obj


def _list(obj: Any, where: str) -> list:
    if not isinstance(obj, list):
        raise ValueError(f"{where}: expected a list")
    return obj


def _bool(obj: Any, where: str) -> bool:
    if not isinstance(obj, bool):
        raise ValueError(f"{where}: expected true or false")
    return obj


def _unique(items: list, where: str) -> dict:
    out: dict = {}
    for item in items:
        if item.code in out:
            raise ValueError(f"{where}: duplicate code {item.code!r}")
        out[item.code] = item
    return out


def available() -> list[str]:
    return sorted(p.stem for p in PRESET_DIR.glob("*.json"))


def read_preset_file(code: str) -> dict:
    if code not in available():
        raise ValueError(f"unknown preset {code!r}; known: {available()}")
    return json.loads((PRESET_DIR / f"{code}.json").read_text(encoding="utf-8"))


def preset_from_dict(data: dict) -> Preset:
    _keys(data, _PRESET_KEYS - {"note"}, "preset", optional=_PRESET_OPTIONAL)
    where = f"preset {data['code']}"
    cert_types = _unique(
        [
            CertType(
                **{
                    **_keys(c, {"code", "label", "validity_months"}, f"{where} cert type", optional={"match", "short"}),
                    "match": tuple(m.upper() for m in _list(c.get("match", []), f"{where} cert type match")),
                }
            )
            for c in _list(data["cert_types"], f"{where} cert_types")
        ],
        f"{where} cert types",
    )
    roles = _unique(
        [
            Role(
                code=r["code"],
                label=r["label"],
                required_certs=tuple(
                    _list(
                        _keys(r, {"code", "label", "required_certs"}, f"{where} role", optional={"reserve", "short"})[
                            "required_certs"
                        ],
                        f"{where} role required_certs",
                    )
                ),
                reserve=_bool(r.get("reserve", False), f"{where} role reserve"),
                short=r.get("short", ""),
            )
            for r in _list(data["roles"], f"{where} roles")
        ],
        f"{where} roles",
    )
    programs = []
    for p in _list(data["programs"], f"{where} programs"):
        _keys(p, {"code", "label", "renewal_months", "levels"}, f"{where} program", optional={"short", "clearance"})
        levels = _unique(
            [
                Level(**_keys(lv, {"code", "label", "max_per_instructor"}, f"{where} program {p['code']} level"))
                for lv in _list(p["levels"], f"{where} program {p['code']} levels")
            ],
            f"{where} program {p['code']} levels",
        )
        programs.append(
            Program(code=p["code"], label=p["label"], renewal_months=p["renewal_months"], levels=levels, short=p.get("short", ""),
                    clearance=p.get("clearance", ""))
        )
    _policy_defaults(data)  # validate early
    young = data.get("young_children")
    if young is not None:
        _keys(young, {"max_age", "max_per_instructor"}, f"{where} young_children")
    return Preset(
        code=data["code"],
        label=data["label"],
        roles=roles,
        cert_types=cert_types,
        programs=_unique(programs, f"{where} programs"),
        venue_term=_venue_term(data),
        young_age_max=young["max_age"] if young else None,
        young_max_per_instructor=young["max_per_instructor"] if young else None,
        copy=_copy(data),
        money=_money(data),
        min_rest_hours=data.get("min_rest_hours"),
        **_offers(data),
    )


def _offers(data: dict) -> dict:
    """Offer waves (product decision): how many at once, how long to collect answers, when plan B goes out anyway."""
    offers = data.get("offers")
    if offers is None:
        return {}
    _keys(offers, set(), "preset offers", optional={"wave_size", "collect_seconds", "plan_b_minutes_before_start"})
    return {
        "offer_wave_size": offers.get("wave_size", 1),
        "offer_collect_seconds": offers.get("collect_seconds", 0),
        "plan_b_minutes_before_start": offers.get("plan_b_minutes_before_start"),
    }


def _money(data: dict) -> dict:
    """Prices for the money card: every one says its unit and whether it is an assumption."""
    if "money" not in data:
        return {}
    where = f"preset {data['code']} money"
    block = _keys(data["money"], set(MONEY_KEYS), where)
    out = {}
    for name, entry in block.items():
        _keys(entry, _PRICE_KEYS, f"{where}.{name}", optional={"source"})
        value = entry["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{where}.{name}: value must be a number >= 0")
        if not isinstance(entry["assumption"], bool):
            raise ValueError(f"{where}.{name}: assumption must be true or false")
        if not entry["assumption"] and not entry.get("source"):
            raise ValueError(f"{where}.{name}: a price that is not an assumption needs a source")
        if not (isinstance(entry["unit"], str) and isinstance(entry["note"], str)):
            raise ValueError(f"{where}.{name}: unit and note are text")
        out[name] = dict(entry)
    if out["refund_share"]["value"] > 1:
        raise ValueError(f"{where}.refund_share: value must be between 0 and 1")
    if out["overtime_multiplier"]["value"] < 1:
        raise ValueError(f"{where}.overtime_multiplier: value must be >= 1")
    return out


def _copy(data: dict) -> dict[str, str]:
    copy = data.get("copy", {})
    if not isinstance(copy, dict) or not all(isinstance(v, str) for v in copy.values()):
        raise ValueError(f"preset {data['code']}: copy must map names to strings")
    return dict(copy)


def _venue_term(data: dict) -> dict[str, str]:
    term = _keys(data["venue_term"], {"en", "ru"}, f"preset {data['code']} venue_term")
    if not all(isinstance(v, str) and v.strip() for v in term.values()):
        raise ValueError(f"preset {data['code']}: venue_term values must be non-empty strings")
    return dict(term)


def _policy_defaults(data: dict) -> dict:
    defaults = _keys(data["policy_defaults"], _DEFAULT_KEYS, f"preset {data['code']} policy_defaults")
    if defaults["week_start"] not in WEEKDAYS:
        raise ValueError(f"preset {data['code']}: week_start must be one of {WEEKDAYS}")
    check_autonomy(defaults["autonomy"])
    return defaults


def load_preset(code: str) -> Preset:
    return preset_from_dict(read_preset_file(code))


def default_policy(code: str, **overrides: Any) -> Policy:
    """Policy from a preset's defaults; keyword overrides win (week_start as int 0..6 or weekday name)."""
    data = read_preset_file(code)
    preset = preset_from_dict(data)
    values = {**_policy_defaults(data), **overrides}
    unknown = values.keys() - _DEFAULT_KEYS
    if unknown:
        raise ValueError(f"unknown policy settings {sorted(unknown)}")
    if "autonomy" in overrides:  # a partial override keeps the preset's other actions
        values["autonomy"] = {**_policy_defaults(data)["autonomy"], **overrides["autonomy"]}
    week_start = values["week_start"]
    if isinstance(week_start, str):
        if week_start not in WEEKDAYS:
            raise ValueError(f"week_start must be one of {WEEKDAYS}")
        week_start = WEEKDAYS.index(week_start)
    return Policy(
        preset=preset,
        max_weekly_hours=values["max_weekly_hours"],
        week_start=week_start,
        makeup_window_days=values["makeup_window_days"],
        autonomy=dict(values["autonomy"]),
        offer_timeout_minutes=values["offer_timeout_minutes"],
        overtime_cost_per_hour=values["overtime_cost_per_hour"],
        default_remedy=values["default_remedy"],
    )
