"""Human text for rule reasons and queue places. Deterministic templates, English and Russian.

The rules return codes and parameters only; this module turns them into a
short line for a person. Codes are replaced by preset labels when a preset is
given.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from coachcover.model import Preset
from coachcover.rules import Code, Ranked, Reason

LANGUAGES = ("en", "ru")

TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        Code.IS_THE_ABSENT_PERSON: "is the person who cannot come",
        Code.CALLED_OUT: "called out for {from_day}",
        Code.ROLE_MISSING: "does not hold the {role} role",
        Code.CERT_MISSING: "has no {cert_type} certificate",
        Code.CERT_NOT_YET_VALID: "{cert_type} is valid only from {valid_from}",
        Code.CERT_EXPIRED: "{cert_type} expired on {expires_on}",
        Code.CERT_REVOKED: "{cert_type} valid until {expires_on}, but revoked by the issuer on {revoked_on}",
        Code.CERT_EXPIRES_DURING_SHIFT: "{cert_type} expires on {expires_on}, before the shift ends",
        Code.CERT_NEEDS_REVIEW: "{cert_type}: the two readings of the card disagree, a person must check it",
        Code.CERT_EXPIRES_ON_SHIFT_DAY: "{cert_type} expires on the day of the shift ({expires_on})",
        "CERT_EXPIRES_TODAY": "{cert_type} expires today",
        Code.PROGRAM_MISSING: "not trained in {program}",
        Code.LEVEL_MISSING: "not cleared for {program}, {level}",
        Code.PROGRAM_RENEWAL_OVERDUE: "{program} renewal was due on {due_on}",
        Code.HOURS_LIMIT_EXCEEDED: "would have {hours_after} h in the week of {week_start} (limit {limit_hours} h)",
        Code.OVERLAPS_COMMITMENT: "already booked {start}-{end}",
        Code.REST_TOO_SHORT: "only {rest_hours} h of rest since {since} (needs {min_rest_hours} h)",
        "REST_TOO_SHORT_AFTER": "only {rest_hours} h of rest before {until} (needs {min_rest_hours} h)",
        Code.RATIO_EXCEEDED: "{participants} participants for {instructors} instructor(s), the limit is {capacity}",
    },
    "ru": {
        Code.IS_THE_ABSENT_PERSON: "это тот, кто не выходит",
        Code.CALLED_OUT: "сообщил(а), что не выйдет {from_day}",
        Code.ROLE_MISSING: "нет роли «{role}»",
        Code.CERT_MISSING: "нет сертификата {cert_type}",
        Code.CERT_NOT_YET_VALID: "{cert_type} действует только с {valid_from}",
        Code.CERT_EXPIRED: "{cert_type} истёк {expires_on}",
        Code.CERT_REVOKED: "{cert_type} действует до {expires_on}, но отозван выдавшей организацией {revoked_on}",
        Code.CERT_EXPIRES_DURING_SHIFT: "{cert_type} истекает {expires_on}, до конца смены",
        Code.CERT_NEEDS_REVIEW: "{cert_type}: два прочтения удостоверения не сошлись, нужна проверка человеком",
        Code.CERT_EXPIRES_ON_SHIFT_DAY: "{cert_type} истекает в день смены ({expires_on})",
        "CERT_EXPIRES_TODAY": "{cert_type} истекает сегодня",
        Code.PROGRAM_MISSING: "нет программы {program}",
        Code.LEVEL_MISSING: "нет допуска к {program}, {level}",
        Code.PROGRAM_RENEWAL_OVERDUE: "обновление программы {program} просрочено с {due_on}",
        Code.HOURS_LIMIT_EXCEEDED: "будет {hours_after} ч за неделю с {week_start} (лимит {limit_hours} ч)",
        Code.OVERLAPS_COMMITMENT: "уже занят {start}–{end}",
        Code.REST_TOO_SHORT: "отдых только {rest_hours} ч после {since} (нужно {min_rest_hours} ч)",
        "REST_TOO_SHORT_AFTER": "отдых только {rest_hours} ч до {until} (нужно {min_rest_hours} ч)",
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
    if reason.code == Code.CALLED_OUT:
        params["from_day"] = _date_text(params["from"][:10], lang)
    if reason.code == Code.REST_TOO_SHORT and params.get("side") == "after":
        key = "REST_TOO_SHORT_AFTER"
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
    for name in ("valid_from", "expires_on", "due_on", "week_start", "revoked_on"):
        if name in params:
            params[name] = _date_text(params[name], lang)
    for name in ("start", "end", "since", "until"):
        if name in params:
            params[name] = _time_text(params[name])
    for name in ("hours_after", "limit_hours", "rest_hours", "min_rest_hours"):
        if name in params:
            params[name] = _number(params[name])
    return TEMPLATES[lang][key].format(**params)


_PLACE = {
    "en": {
        "ordinals": {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
                     8: "eighth", 9: "ninth", 10: "tenth"},
        "position": "#{n}",
        "head": "{name} {position}: ",
        "not_eligible": "not eligible",
        "hours": "{hours} h this week",
        "not_cleared": "not cleared for {levels}",
        "same_venue": "same {venue}",
        "km": "{km} km away",
        "other_venue": "other {venue}",
        "venue": "venue",
        "speaks": "speaks {language} like {n} of {total} families",
    },
    "ru": {
        "ordinals": {},
        "position": "№{n}",
        "head": "{name}, {position}: ",
        "not_eligible": "не допущен(а)",
        "hours": "{hours} ч на этой неделе",
        "not_cleared": "нет допуска к {levels}",
        "same_venue": "тот же {venue}",
        "km": "{km} км",
        "other_venue": "другой {venue}",
        "venue": "объект",
        "speaks": "говорит на языке {n} из {total} семей ({language})",
    },
}


# The school's working language: everyone speaks it, so it is never called out in a queue line.
WORKING_LANGUAGE = "en"
LANGUAGE_NAMES = {
    "en": {"en": "English", "es": "Spanish", "ru": "Russian", "fr": "French", "pt": "Portuguese", "zh": "Chinese"},
    "ru": {"en": "английский", "es": "испанский", "ru": "русский", "fr": "французский", "pt": "португальский",
           "zh": "китайский"},
}


def _level_text(program: str, level: str, preset: Preset | None) -> str:
    if preset is not None and program in preset.programs and level in preset.programs[program].levels:
        return preset.programs[program].levels[level].label
    return f"{program} {level}"


def describe_place(ranked: Ranked, name: str, lang: str = "en", preset: Preset | None = None) -> str:
    """One line for a queue place, e.g. "Zoe first: 12 h this week, Level 3, same venue". Tenure stays in place()."""
    if lang not in LANGUAGES:
        raise ValueError(f"unsupported language {lang!r}; use one of {LANGUAGES}")
    t = _PLACE[lang]
    place = ranked.place()
    position = t["ordinals"].get(place["position"]) or t["position"].format(n=place["position"])
    parts = [] if place["eligible"] else [t["not_eligible"]]
    parts.append(t["hours"].format(hours=_number(place["week_hours"])))
    levels = ", ".join(_level_text(program, level, preset) for program, level in place["levels"])
    if place["level_match"] is True:
        parts.append(levels)
    elif place["level_match"] is False:
        parts.append(t["not_cleared"].format(levels=levels))
    venue = (preset.venue_term.get(lang) if preset is not None else None) or t["venue"]
    if place["same_venue"]:
        parts.append(t["same_venue"].format(venue=venue))
    elif place["distance_km"] is not None:
        parts.append(t["km"].format(km=_number(place["distance_km"])))
    else:
        parts.append(t["other_venue"].format(venue=venue))
    family = place.get("family_language")
    if family and family["speaks"] and family["language"] != WORKING_LANGUAGE:
        language = LANGUAGE_NAMES[lang].get(family["language"], family["language"])
        parts.append(t["speaks"].format(language=language, n=family["families"], total=family["of"]))
    return t["head"].format(name=name, position=position) + ", ".join(parts)


# a short name for a venue's zone on screens ("08:40:10 ET"); machine fields stay in UTC
ZONE_SHORT = {"America/New_York": "ET", "America/Detroit": "ET", "America/Chicago": "CT", "America/Denver": "MT",
              "America/Phoenix": "MT", "America/Los_Angeles": "PT", "Pacific/Honolulu": "HT", "America/Anchorage": "AKT"}


def local_stamp(value: datetime, tz: ZoneInfo | str, fmt: str = "%H:%M:%S") -> str:
    """A moment in the venue's own time with its zone, for people: "08:40:10 ET"."""
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    local = value.astimezone(zone)
    return f"{local:{fmt}} {ZONE_SHORT.get(str(zone)) or local.tzname()}"
