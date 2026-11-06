"""Model client for Vultr Serverless Inference (OpenAI-compatible), with recorded replay.

Rules for every call:
- the requested model must be on the allowlist (a typo fails before anything is sent);
- the `model` field of the answer must be the requested model: Vultr answers a
  misspelled name with some other model instead of an error, so an answer from
  another model is an error and is thrown away;
- every call, failed or not, goes to the ledger: model from the answer, tokens,
  milliseconds and cost from the price table.

Modes: `replay` (default) answers from recordings in tests/fixtures/models;
`live` needs COACHCOVER_MODEL_MODE=live and VULTR_INFERENCE_API_KEY in the
environment. Keys are never read from files.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import psycopg
from psycopg.types.json import Jsonb

ALLOWED_MODELS = (
    "whisper-large-v3-turbo",
    "muse-glimmer-30b",
    "nemotron-3-nano-omni-30b-a3b-reasoning",
    "raon-1-mini-preview",
    "nemotron-3.5-content-safety",
)
SPEECH_MODEL = "whisper-large-v3-turbo"
TEXT_MODEL = "muse-glimmer-30b"
SECOND_READER_MODEL = "nemotron-3-nano-omni-30b-a3b-reasoning"

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
PRICES_FILE = Path(__file__).parent / "model_prices.json"
DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "models"


class ModelError(RuntimeError):
    """A model call that produced no usable answer."""


class ModelNotAllowed(ModelError):
    pass


class ModelSubstituted(ModelError):
    pass


class NoRecording(ModelError):
    pass


class BadAnswer(ModelError):
    pass


def canonical_key(obj: Any) -> str:
    """sha256 of the canonical JSON of the call's input; recordings are found by it."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- ledger -----------------------------------------------------------------


@dataclass(frozen=True)
class CallRecord:
    at: datetime
    purpose: str
    model_requested: str
    model_returned: str | None
    source: str  # "live" | "recorded"
    ok: bool
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    cost_usd: float | None
    case_id: int | None
    ref: dict


class Ledger(Protocol):
    def record(self, call: CallRecord) -> None: ...


@dataclass
class MemoryLedger:
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, call: CallRecord) -> None:
        self.calls.append(call)


@dataclass
class DbLedger:
    conn: psycopg.Connection

    def record(self, call: CallRecord) -> None:
        self.conn.execute(
            "INSERT INTO model_call (at, purpose, model_requested, model_returned, source, ok, error,"
            " input_tokens, output_tokens, duration_ms, cost_usd, case_id, ref)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                call.at,
                call.purpose,
                call.model_requested,
                call.model_returned,
                call.source,
                call.ok,
                call.error,
                call.input_tokens,
                call.output_tokens,
                call.duration_ms,
                call.cost_usd,
                call.case_id,
                Jsonb(call.ref),
            ),
        )


# --- prices -----------------------------------------------------------------


def load_prices(path: Path = PRICES_FILE) -> dict[str, dict[str, float]]:
    return json.loads(path.read_text(encoding="utf-8"))["models"]


def call_cost(prices: dict, model: str, input_tokens: int | None, output_tokens: int | None, audio_seconds: float | None) -> float | None:
    price = prices.get(model)
    if price is None:
        return None
    if "per_audio_hour" in price:
        return None if audio_seconds is None else round(price["per_audio_hour"] * audio_seconds / 3600, 6)
    if input_tokens is None or output_tokens is None:
        return None
    return round(
        input_tokens * price["input_per_1m_tokens"] / 1e6 + output_tokens * price["output_per_1m_tokens"] / 1e6, 6
    )


# --- recordings -------------------------------------------------------------


@dataclass(frozen=True)
class Recording:
    purpose: str
    model: str
    input_sha256: str
    response: dict
    duration_ms: int
    synthetic: bool
    source: str


class Recordings:
    """Recorded answers, found by (purpose, model, input key)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._index: dict[tuple[str, str, str], Recording] = {}
        for path in sorted(self.directory.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            rec = Recording(
                purpose=raw["purpose"],
                model=raw["model"],
                input_sha256=raw["input_sha256"],
                response=raw["response"],
                duration_ms=int(raw["duration_ms"]),
                synthetic=bool(raw["synthetic"]),
                source=raw["source"],
            )
            key = (rec.purpose, rec.model, rec.input_sha256)
            if key in self._index:
                raise ValueError(f"two recordings for {key}: {path.name}")
            self._index[key] = rec

    def find(self, purpose: str, model: str, input_sha256: str) -> Recording:
        rec = self._index.get((purpose, model, input_sha256))
        if rec is None:
            raise NoRecording(f"no recorded answer for {purpose} / {model} / {input_sha256[:12]}")
        return rec


# --- schema check (the subset of JSON Schema the prompts use) ---------------

_TYPES = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
    "null": type(None),
}


def check_schema(value: Any, schema: dict, path: str = "$") -> None:
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        ok = any(
            isinstance(value, _TYPES[k]) and not (k in ("integer", "number") and isinstance(value, bool)) for k in kinds
        )
        if not ok:
            raise BadAnswer(f"{path}: expected {kinds}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise BadAnswer(f"{path}: {value!r} not in {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.fullmatch(schema["pattern"], value):
        raise BadAnswer(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = [k for k in schema.get("required", []) if k not in value]
        if missing:
            raise BadAnswer(f"{path}: missing {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise BadAnswer(f"{path}: unexpected {extra}")
        for k, v in value.items():
            if k in props:
                check_schema(v, props[k], f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            check_schema(item, schema["items"], f"{path}[{i}]")


def _json_from_content(content: str) -> Any:
    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise BadAnswer("answer is not JSON") from None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise BadAnswer(f"answer is not JSON: {exc}") from None


# --- the client -------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    data: Any
    record: CallRecord
    synthetic: bool


Transport = Callable[[str, dict], tuple[dict, int]]  # (path, request) -> (response body, duration ms)


class ModelClient:
    def __init__(
        self,
        ledger: Ledger,
        *,
        mode: str | None = None,
        recordings_dir: Path | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: Transport | None = None,
    ):
        self.ledger = ledger
        self.mode = mode or os.environ.get("COACHCOVER_MODEL_MODE", "replay")
        if self.mode not in ("replay", "live"):
            raise ValueError("model mode must be 'replay' or 'live'")
        self.prices = load_prices()
        self.recordings: Recordings | None = None
        if self.mode == "replay":
            directory = recordings_dir or Path(os.environ.get("COACHCOVER_RECORDINGS_DIR", DEFAULT_RECORDINGS_DIR))
            self.recordings = Recordings(directory)
        else:
            self._key = api_key or os.environ.get("VULTR_INFERENCE_API_KEY")
            if not self._key:
                raise ModelError("live mode needs VULTR_INFERENCE_API_KEY in the environment")
            self._base_url = (base_url or os.environ.get("COACHCOVER_INFERENCE_URL") or DEFAULT_BASE_URL).rstrip("/")
            self._transport = transport or self._http

    # live HTTP ---------------------------------------------------------------

    def _http(self, path: str, request: dict) -> tuple[dict, int]:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Bearer {self._key}"}
        if "multipart" in request:
            boundary = f"----coachcover{uuid.uuid4().hex}"
            body = b""
            for name, value in request["multipart"]["fields"].items():
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            filename, content, mime = request["multipart"]["file"]
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
            body += content + f"\r\n--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            body = json.dumps(request["json"]).encode()
            headers["Content-Type"] = "application/json"
        started = time.monotonic()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise BadAnswer(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ModelError(f"network error: {exc}") from None
        return payload, int((time.monotonic() - started) * 1000)

    # common path ---------------------------------------------------------------

    def _call(
        self,
        *,
        purpose: str,
        model: str,
        input_sha256: str,
        request: tuple[str, dict],
        at: datetime,
        case_id: int | None,
        ref: dict,
        parse: Callable[[dict], Any],
        audio_seconds: Callable[[dict], float | None] = lambda body: None,
        model_optional: bool = False,
    ) -> Answer:
        source = "recorded" if self.mode == "replay" else "live"
        returned = None
        tokens_in = tokens_out = duration = None
        cost = None
        synthetic = False
        call_ref = {**ref, "input_sha256": input_sha256}
        try:
            if model not in ALLOWED_MODELS:
                raise ModelNotAllowed(f"model {model!r} is not on the allowlist")
            if self.mode == "replay":
                rec = self.recordings.find(purpose, model, input_sha256)
                body, duration, synthetic = rec.response, rec.duration_ms, rec.synthetic
                if synthetic:
                    call_ref["synthetic"] = True
            else:
                body, duration = self._transport(*request)
            returned = body.get("model")
            if returned is None and model_optional:
                call_ref["model_unverified"] = True  # the speech endpoint may not name the model
            elif returned not in ALLOWED_MODELS or returned != model:
                raise ModelSubstituted(f"asked {model!r}, answered by {returned!r}; answer thrown away")
            usage = body.get("usage") or {}
            tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
            cost = call_cost(self.prices, model, tokens_in, tokens_out, audio_seconds(body))
            data = parse(body)
        except ModelError as exc:
            self.ledger.record(
                CallRecord(at, purpose, model, returned, source, False, f"{type(exc).__name__}: {exc}",
                           tokens_in, tokens_out, duration, cost, case_id, call_ref)
            )
            raise
        record = CallRecord(at, purpose, model, returned if returned else None, source, True, None,
                            tokens_in, tokens_out, duration, cost, case_id, call_ref)
        self.ledger.record(record)
        return Answer(data=data, record=record, synthetic=synthetic)

    def transcribe(self, audio: bytes, filename: str, *, at: datetime, case_id: int | None = None) -> Answer:
        """Speech to text. `data` = {"text": ..., "language": ... or None}."""
        mime = "audio/ogg" if filename.endswith((".ogg", ".oga", ".opus")) else "application/octet-stream"
        request = ("/audio/transcriptions", {"multipart": {
            "fields": {"model": SPEECH_MODEL, "response_format": "verbose_json"},
            "file": (filename, audio, mime),
        }})

        def parse(body: dict) -> dict:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise BadAnswer("empty transcript")
            return {"text": text.strip(), "language": body.get("language")}

        return self._call(
            purpose="transcribe",
            model=SPEECH_MODEL,
            input_sha256=canonical_key({"audio_sha256": sha256_bytes(audio)}),
            request=request,
            at=at,
            case_id=case_id,
            ref={"filename": filename},
            parse=parse,
            audio_seconds=lambda body: body.get("duration"),
            model_optional=True,
        )

    def chat_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str | list,
        schema: dict,
        key: Any,
        at: datetime,
        model: str = TEXT_MODEL,
        case_id: int | None = None,
        ref: dict | None = None,
    ) -> Answer:
        """A structured answer that must match `schema`. `key` identifies the input for replay."""
        request = ("/chat/completions", {"json": {
            "model": model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": purpose, "schema": schema, "strict": True}},
        }})

        def parse(body: dict) -> Any:
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise BadAnswer("no message in the answer") from None
            data = _json_from_content(content or "")
            check_schema(data, schema)
            return data

        return self._call(
            purpose=purpose,
            model=model,
            input_sha256=canonical_key(key),
            request=request,
            at=at,
            case_id=case_id,
            ref=ref or {},
            parse=parse,
        )

    def read_card(self, image: bytes, mime: str, *, model: str, at: datetime, case_id: int | None = None) -> Answer:
        """Fields of a certificate card from a photo or scan."""
        b64 = base64.b64encode(image).decode()
        user = [
            {"type": "text", "text": CARD_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
        return self.chat_json(
            purpose="read_card",
            system="You read certificate cards. Answer with JSON only.",
            user=user,
            schema=CARD_SCHEMA,
            key={"image_sha256": sha256_bytes(image)},
            at=at,
            model=model,
            case_id=case_id,
        )


CARD_PROMPT = (
    "Read this certificate card. Return JSON with keys: name, title, issued (MM/DD/YYYY), "
    "exp (MM/DD/YYYY, the valid-through date), id. Use null for an unreadable field."
)

CARD_SCHEMA = {
    "type": "object",
    "required": ["name", "title", "issued", "exp", "id"],
    "properties": {
        "name": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "issued": {"type": ["string", "null"]},
        "exp": {"type": ["string", "null"]},
        "id": {"type": ["string", "null"]},
    },
}


def now_utc() -> datetime:
    return datetime.now(UTC)
