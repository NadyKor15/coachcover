"""Load presets (data files in coverage/presets/) and build a policy from them.

The loader is strict: a missing or unknown key, a wrong type or a dangling
reference raises ValueError, so a typo in a preset file cannot pass silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coverage.model import WEEKDAYS, CertType, Level, Policy, Preset, Program, Role, check_autonomy

PRESET_DIR = Path(__file__).parent / "presets"

_PRESET_KEYS = {"code", "label", "note", "cert_types", "roles", "programs", "policy_defaults"}
_DEFAULT_KEYS = {"max_weekly_hours", "week_start", "makeup_window_days", "autonomy"}


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
    _keys(data, _PRESET_KEYS - {"note"}, "preset", optional={"note"})
    where = f"preset {data['code']}"
    cert_types = _unique(
        [
            CertType(**_keys(c, {"code", "label", "validity_months"}, f"{where} cert type"))
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
                        _keys(r, {"code", "label", "required_certs"}, f"{where} role")["required_certs"],
                        f"{where} role required_certs",
                    )
                ),
            )
            for r in _list(data["roles"], f"{where} roles")
        ],
        f"{where} roles",
    )
    programs = []
    for p in _list(data["programs"], f"{where} programs"):
        _keys(p, {"code", "label", "renewal_months", "levels"}, f"{where} program")
        levels = _unique(
            [
                Level(**_keys(lv, {"code", "label", "max_per_instructor"}, f"{where} program {p['code']} level"))
                for lv in _list(p["levels"], f"{where} program {p['code']} levels")
            ],
            f"{where} program {p['code']} levels",
        )
        programs.append(Program(code=p["code"], label=p["label"], renewal_months=p["renewal_months"], levels=levels))
    _policy_defaults(data)  # validate early
    return Preset(
        code=data["code"],
        label=data["label"],
        roles=roles,
        cert_types=cert_types,
        programs=_unique(programs, f"{where} programs"),
    )


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
    )
