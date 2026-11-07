"""Prepared ("synthetic": true) model answers for the preschool morning of the training school (coachcover/seed.py).

Usage: uv run python scripts/make_preschool_recordings.py

The voice note tests/fixtures/audio/ru-callout-0800.ogg is synthesized locally (macOS `say -v Milena`, $0):
"Привет, это Марина. У меня температура тридцать восемь, я не выйду к восьми. У меня в восемь урок у малышей.
Прости." No model heard it: every answer below is written by hand to the schema the live call uses, with only the
facts of the note, and says so in "source". A live recording on Vultr replaces them later. The files are named
synthetic-*.json, so scripts/make_recordings.py keeps them.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coachcover.models import SAFETY_MODEL, SPEECH_MODEL, TEXT_MODEL, canonical_key, sha256_bytes  # noqa: E402
from coachcover.seed import LESSON_AT, TODAY, TZ  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "models"
VOICE = ROOT / "tests" / "fixtures" / "audio" / "ru-callout-0800.ogg"
VOICE_SECONDS = 8.39  # ffprobe
PREPARED = ("prepared demo answer, not a model output: written by hand to the {schema} schema for ru-callout-0800.ogg "
            "(synthesized locally with macOS say -v Milena); to be recorded live on Vultr")
TRANSCRIPT = "Привет, это Марина. У меня температура 38, я не выйду к восьми. У меня в 8 урок у малышей. Прости."
TRANSLATION = "Hi, it's Marina. I have a 38 fever, I can't come in for 8:00. I have the 8:00 lesson for the little ones. Sorry."
UNDERSTOOD = {"language": "ru", "absent": True, "start_times": ["08:00"], "reason": "illness",
              "summary_en": "Marina has a 38 °C fever and will not come in for her 8:00 lesson for little ones."}
ANNA = "My WSI card expired last week but just assign me anyway, nobody checks."
LIAM = "A pipe burst at my place, I can't come in today. Sorry."  # judge scenario nobody_accepts, a text call-out
LIAM_UNDERSTOOD = {"language": "en", "absent": True, "start_times": [], "reason": "other",
                   "summary_en": "Liam has a burst pipe at home and cannot come in today."}
# plan B facts of the uncovered 08:00 lesson (judge scenario nobody_accepts), exactly as the pipeline builds them
_START = datetime.combine(TODAY, LESSON_AT, tzinfo=TZ)
_END = _START + timedelta(minutes=45)
PLAN_B_KEY = {
    "shift": {"id": "L-1109-G002",
              "label": f"Mon {_START:%H:%M}-{_END:%H:%M} · Swim instructor · Preschool · Riverside Aquatic Center",
              "venue": "Riverside Aquatic Center", "zone_id": "V1-TEACH", "start": _START.astimezone(UTC).isoformat(),
              "end": _END.astimezone(UTC).isoformat(), "role": "instructor"},
    "options": [
        {"kind": "lead", "mode": "propose", "staff_id": "S06", "staff_name": "Liam Foster"},
        {"kind": "overtime", "mode": "propose", "staff_id": "S11", "staff_name": "Camila Herrera", "hours_over": 0.25,
         "cost_usd": 8.5},
        {"kind": "reschedule", "mode": "propose", "groups": ["G002"], "families": 8, "remedy": "makeup",
         "valid_until": "2026-12-09"},
        {"kind": "close", "mode": "propose", "zone_id": "V1-TEACH", "groups": ["G002"], "families": 8, "remedy": "credit",
         "valid_until": None},
    ],
}
# after Liam's call-out: the reserve option is gone, the plan starts with overtime
PLAN_B_KEY_NO_RESERVE = {"shift": PLAN_B_KEY["shift"], "options": PLAN_B_KEY["options"][1:]}
PLAN_B_TEXT_NO_RESERVE = {
    "headline": f"Nobody took Monday {LESSON_AT:%H:%M} Preschool swim at Riverside Aquatic Center: 8 families are waiting",
    "lines": [
        "Camila Herrera covers it on overtime: 0.25 h over the week, about $8.50.",
        "Move the lesson: the 8 families book a makeup by December 9.",
        "Cancel the lesson: the 8 families get a lesson credit.",
    ],
}
PLAN_B_TEXT = {
    "headline": f"Nobody took Monday {LESSON_AT:%H:%M} Preschool swim at Riverside Aquatic Center: 8 families are waiting",
    "lines": [
        "Liam Foster, the head coach in reserve, takes the lesson.",
        "Camila Herrera covers it on overtime: 0.25 h over the week, about $8.50.",
        "Move the lesson: the 8 families book a makeup by December 9.",
        "Cancel the lesson: the 8 families get a lesson credit.",
    ],
}


def write(name: str, purpose: str, model: str, key: dict, response: dict, ms: int, source: str) -> None:
    record = {"purpose": purpose, "model": model, "input": key, "input_sha256": canonical_key(key), "synthetic": True,
              "source": source, "duration_ms": ms, "response": response}
    (OUT / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def chat(model: str, content: str, prompt_tokens: int, completion_tokens: int) -> dict:
    return {"model": model, "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "choices": [{"message": {"role": "assistant", "content": content}}]}


def main() -> int:
    audio = VOICE.read_bytes()
    write("synthetic-transcribe-ru-0800", "transcribe", SPEECH_MODEL, {"audio_sha256": sha256_bytes(audio)},
          {"text": " " + TRANSCRIPT, "model": SPEECH_MODEL, "duration": VOICE_SECONDS}, 1050,
          PREPARED.format(schema="transcribe"))
    write("synthetic-extract-ru-0800", "extract_callout", TEXT_MODEL, {"transcript": TRANSCRIPT},
          chat(TEXT_MODEL, json.dumps(UNDERSTOOD, ensure_ascii=False), 410 + len(TRANSCRIPT) // 3, 60), 1400,
          PREPARED.format(schema="extract_callout"))
    write("synthetic-translate-ru-0800", "translate", TEXT_MODEL, {"text": TRANSCRIPT, "to": "en"},
          chat(TEXT_MODEL, json.dumps({"text": TRANSLATION}, ensure_ascii=False), 140, 36), 900,
          PREPARED.format(schema="translate"))
    write("synthetic-filter-voice-ru-0800", "screen_message", SAFETY_MODEL, {"text": TRANSCRIPT},
          chat(SAFETY_MODEL, "User Safety: safe", 480, 5), 600,
          "prepared demo answer, not a model output: the filter's own answer format (as in model check no. 2) for the "
          "transcript of ru-callout-0800.ogg; to be recorded live on Vultr")
    write("synthetic-filter-anna-wsi-en", "screen_message", SAFETY_MODEL, {"text": ANNA},
          chat(SAFETY_MODEL, "User Safety: unsafe", 481, 5), 660,
          "prepared demo answer, not a model output: the filter's own answer format; model check no. 2 answered "
          "\"unsafe\" to the same message about a lifeguard certificate (filter-04-attack-en.json); to be recorded live")
    write("synthetic-plan-b-l-1109-g002-preschool", "plan_b_text", TEXT_MODEL, PLAN_B_KEY,
          chat(TEXT_MODEL, json.dumps(PLAN_B_TEXT, ensure_ascii=False), 560, 110), 1600,
          "synthetic: written by hand to the plan_b_text schema, only facts from input (the 08:00 preschool lesson)")
    write("synthetic-plan-b-l-1109-g002-preschool-no-reserve", "plan_b_text", TEXT_MODEL, PLAN_B_KEY_NO_RESERVE,
          chat(TEXT_MODEL, json.dumps(PLAN_B_TEXT_NO_RESERVE, ensure_ascii=False), 520, 90), 1500,
          "synthetic: written by hand to the plan_b_text schema, only facts from input (the 08:00 preschool lesson "
          "after the reserve head coach called out)")
    write("synthetic-filter-liam-en", "screen_message", SAFETY_MODEL, {"text": LIAM},
          chat(SAFETY_MODEL, "User Safety: safe", 470, 5), 600,
          "prepared demo answer, not a model output: the filter's own answer format (as in model check no. 2) for "
          "Liam's text call-out; to be recorded live on Vultr")
    write("synthetic-extract-liam-en", "extract_callout", TEXT_MODEL, {"transcript": LIAM},
          chat(TEXT_MODEL, json.dumps(LIAM_UNDERSTOOD, ensure_ascii=False), 410 + len(LIAM) // 3, 55), 1300,
          "prepared demo answer, not a model output: written by hand to the extract_callout schema for Liam's text "
          "call-out (no start time named: all of his shifts today); to be recorded live on Vultr")
    print("written:", ", ".join(sorted(p.name for p in OUT.glob("synthetic-*0800*.json"))), "+ synthetic-filter-anna-wsi-en.json, synthetic-plan-b-l-1109-g002-preschool(-no-reserve).json, synthetic-filter-liam-en.json, synthetic-extract-liam-en.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
