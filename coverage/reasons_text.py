"""Human text for rule reasons. Deterministic templates, English and Russian.

The rules return codes and parameters only; this module turns them into a
short line for a person. Codes are replaced by preset labels when a preset is
given.
"""

from __future__ import annotations

from datetime import date, datetime

from coverage.model import Preset
from coverage.rules import Code, Reason

LANGUAGES = ("en", "ru")

TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        Code.ROLE_MISSING: "does not hold the {role} role",
        Code.CERT_MISSING: "has no {cert_type} certificate",
        Code.CERT_NOT_YET_VALID: "{cert_type} is valid only from {valid_from}",
        Code.CERT_EXPIRED: "{cert_type} expired on {expires_on}",
        Code.CERT_EXPIRES_DURING_SHIFT: "{cert_type} expires on {expires_on}, before the shift ends",
        Code.CERT_NEEDS_REVIEW: "{cert_type}: the two readings of the card disagree, a person must check it",
        Code.CERT_EXPIRES_ON_SHIFT_DAY: "{cert_type} expires on the day of the shift ({expires_on})",
        "CERT_EXPIRES_TODAY": "{cert_type} expires today",
        Code.PROGRAM_MISSING: "not trained in {program}",
        Code.LEVEL_MISSING: "not cleared for {program}, {level}",
        Code.PROGRAM_RENEWAL_OVERDUE: "{program} renewal was due on {due_on}",
        Code.HOURS_LIMIT_EXCEEDED: "would have {hours_after} h in the week of {week_start} (limit {limit_hours} h)",
        Code.OVERLAPS_COMMITMENT: "already booked {start}-{end}",
        Code.RATIO_EXCEEDED: "{participants} participants for {instructors} instructor(s), the limit is {capacity}",
    },
    "ru": {
        Code.ROLE_MISSING: "нет роли «{role}»",
        Code.CERT_MISSING: "нет сертификата {cert_type}",
        Code.CERT_NOT_YET_VALID: "{cert_type} действует только с {valid_from}",
        Code.CERT_EXPIRED: "{cert_type} истёк {expires_on}",
        Code.CERT_EXPIRES_DURING_SHIFT: "{cert_type} истекает {expires_on}, до конца смены",
        Code.CERT_NEEDS_REVIEW: "{cert_type}: два прочтения удостоверения не сошлись, нужна проверка человеком",
        Code.CERT_EXPIRES_ON_SHIFT_DAY: "{cert_type} истекает в день смены ({expires_on})",
        "CERT_EXPIRES_TODAY": "{cert_type} истекает сегодня",
        Code.PROGRAM_MISSING: "нет программы {program}",
        Code.LEVEL_MISSING: "нет допуска к {program}, {level}",
        Code.PROGRAM_RENEWAL_OVERDUE: "обновление программы {program} просрочено с {due_on}",
        Code.HOURS_LIMIT_EXCEEDED: "будет {hours_after} ч за неделю с {week_start} (лимит {limit_hours} ч)",
        Code.OVERLAPS_COMMITMENT: "уже занят {start}–{end}",
        Code.RATIO_EXCEEDED: "{participants} участников на {instructors} инстр., норма {capacity}",
    },
}


def _date_text(value: str, lang: str) -> str:
    day = date.fromisoformat(value)
    return day.strftime("%d.%m.%Y") if lang == "ru" else day.isoformat()


def _time_text(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%H:%M")


def _number(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def describe(reason: Reason, lang: str = "en", preset: Preset | None = None) -> str:
    if lang not in LANGUAGES:
        raise ValueError(f"unsupported language {lang!r}; use one of {LANGUAGES}")
    params = dict(reason.params)
    key: str = reason.code
    if reason.code == Code.CERT_EXPIRES_ON_SHIFT_DAY and params.get("today"):
        key = "CERT_EXPIRES_TODAY"
    if preset is not None:
        if "role" in params and params["role"] in preset.roles:
            params["role"] = preset.roles[params["role"]].label
        if "cert_type" in params and params["cert_type"] in preset.cert_types:
            params["cert_type"] = preset.cert_types[params["cert_type"]].label
        program = preset.programs.get(params.get("program"))
        if program is not None:
            if "level" in params and params["level"] in program.levels:
                params["level"] = program.levels[params["level"]].label
            params["program"] = program.label
    for name in ("valid_from", "expires_on", "due_on", "week_start"):
        if name in params:
            params[name] = _date_text(params[name], lang)
    for name in ("start", "end"):
        if name in params:
            params[name] = _time_text(params[name])
    for name in ("hours_after", "limit_hours"):
        if name in params:
            params[name] = _number(params[name])
    return TEMPLATES[lang][key].format(**params)
