"""Build replay recordings in tests/fixtures/models from the 2026-11-06 model check, plus marked synthetic answers.

Usage: uv run python scripts/make_recordings.py <path to spike-result.json>

Real recordings: transcripts and card readings from the check run (answers, tokens, timings as recorded).
The check kept transcript text only, not the response's model field; the transcript recordings add
"model" with a note, to be verified against the live endpoint.
Synthetic recordings ("synthetic": true): call-out understanding, plan B text, family messages and one
disagreeing pair of card readings. They follow the schemas the live calls use.
Hand-written synthetic answers for plan B text and family messages live in tests/fixtures/models/
as files named synthetic-*.json and are not touched by this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coachcover.models import SECOND_READER_MODEL, SPEECH_MODEL, TEXT_MODEL, canonical_key, sha256_bytes  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
OUT = FIXTURES / "models"
SPIKE = "model check 2026-11-06 (spike-result.json)"
DURATIONS = {"ru-callout.ogg": 8.529, "en-callout.ogg": 6.92, "es-callout.ogg": 5.277}  # ffprobe

UNDERSTOOD = {
    "ru-callout.ogg": {"language": "ru", "absent": True, "start_times": ["06:00", "07:00"], "reason": "illness",
                       "summary_en": "Marina has a 38 °C fever and will not come in: a lane at 6 and a lesson for little ones at 7."},
    "en-callout.ogg": {"language": "en", "absent": True, "start_times": ["06:00"], "reason": "illness",
                       "summary_en": "Jake woke up with a fever and cannot make the 6 a.m. shift today."},
    "es-callout.ogg": {"language": "es", "absent": True, "start_times": ["06:00"], "reason": "illness",
                       "summary_en": "Diego has a fever and cannot come to the 6 o'clock shift."},
}


def write(name: str, purpose: str, model: str, key: dict, response: dict, ms: int, synthetic: bool, source: str) -> None:
    record = {
        "purpose": purpose,
        "model": model,
        "input": key,
        "input_sha256": canonical_key(key),
        "synthetic": synthetic,
        "source": source,
        "duration_ms": ms,
        "response": response,
    }
    (OUT / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def chat(model: str, content: dict, usage: dict) -> dict:
    return {"model": model, "usage": usage, "choices": [{"message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}]}


def main(spike_path: str) -> int:
    spike = json.loads(Path(spike_path).read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        if not old.name.startswith("synthetic-"):
            old.unlink()

    for filename, asr in spike["asr"].items():
        audio = (FIXTURES / "audio" / filename).read_bytes()
        response = {"text": asr["text"], "model": SPEECH_MODEL, "duration": DURATIONS[filename]}
        write(f"transcribe-{filename[:2]}", "transcribe", SPEECH_MODEL, {"audio_sha256": sha256_bytes(audio)},
              response, int(asr["sec"] * 1000), False,
              f"{SPIKE}, asr {filename}; the check kept only the text, \"model\" is added and must be verified live")
        transcript = asr["text"].strip()
        tokens = {"prompt_tokens": 410 + len(transcript) // 3, "completion_tokens": 70}
        write(f"extract-{filename[:2]}", "extract_callout", TEXT_MODEL, {"transcript": transcript},
              chat(TEXT_MODEL, UNDERSTOOD[filename], tokens), 1400, True,
              "synthetic: written to the extract_callout schema for the recorded transcript")

    for key, result in spike["certs"].items():
        model, sample = key.split("|")
        if model not in (TEXT_MODEL, SECOND_READER_MODEL):
            continue
        ext = "png" if sample.endswith("scan") else "jpg"
        image = (FIXTURES / "cards" / f"{sample}.{ext}").read_bytes()
        assert result["model_answered"] == model
        short = "muse" if model == TEXT_MODEL else "omni"
        write(f"card-{sample}-{short}", "read_card", model, {"image_sha256": sha256_bytes(image)},
              chat(model, result["got"], result["usage"]), int(result["sec"] * 1000), False, f"{SPIKE}, {key}")

    # a pair of readings that disagree, for a rotated copy of card c2 (as if the date had been edited)
    image = (FIXTURES / "cards" / "c5-photo.jpg").read_bytes()
    base = spike["certs"][f"{TEXT_MODEL}|c2-photo"]["got"]
    for model, short, exp in ((TEXT_MODEL, "muse", "03/15/2027"), (SECOND_READER_MODEL, "omni", "03/15/2026")):
        usage = spike["certs"][f"{model}|c2-photo"]["usage"]
        write(f"card-c5-photo-{short}", "read_card", model, {"image_sha256": sha256_bytes(image)},
              chat(model, {**base, "exp": exp}, usage), 1500, True,
              "synthetic: the two readers disagree on the valid-through date")
    print(f"wrote {len(list(OUT.glob('*.json')))} recordings to {OUT}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
