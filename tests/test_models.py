"""Model client: allowlist, the answering model, ledger with cost, replay recordings, live mode without network."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coachcover.models import (
    ALLOWED_MODELS,
    SECOND_READER_MODEL,
    TEXT_MODEL,
    BadAnswer,
    MemoryLedger,
    ModelClient,
    ModelError,
    ModelNotAllowed,
    ModelSubstituted,
    NoRecording,
    Recordings,
    canonical_key,
)

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 11, 9, 10, 12, tzinfo=UTC)
SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}, "additionalProperties": False}


def _client(tmp_path=None, recordings=None):
    ledger = MemoryLedger()
    directory = FIXTURES / "models"
    if recordings is not None:
        directory = tmp_path / "rec"
        directory.mkdir()
        for i, rec in enumerate(recordings):
            (directory / f"r{i}.json").write_text(json.dumps(rec))
    return ModelClient(ledger, mode="replay", recordings_dir=directory), ledger


def _recording(key, response, model=TEXT_MODEL, purpose="probe"):
    return {"purpose": purpose, "model": model, "input": key, "input_sha256": canonical_key(key), "synthetic": True,
            "source": "test", "duration_ms": 10, "response": response}


def _chat(model, content, usage=None):
    return {"model": model, "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [{"message": {"content": json.dumps(content)}}]}


def test_a_typo_in_the_model_name_is_an_error_before_anything_is_sent():
    client, ledger = _client()
    with pytest.raises(ModelNotAllowed):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key={"x": 1}, at=AT, model="muse-glimer-30b")
    (call,) = ledger.calls
    assert (call.ok, call.model_requested, call.model_returned) == (False, "muse-glimer-30b", None)
    assert "ModelNotAllowed" in call.error


def test_an_answer_from_another_model_is_thrown_away(tmp_path):
    key = {"x": 1}
    client, ledger = _client(tmp_path, [_recording(key, _chat("some-other-model", {"ok": True}))])
    with pytest.raises(ModelSubstituted):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key=key, at=AT)
    assert ledger.calls[0].ok is False and ledger.calls[0].model_returned == "some-other-model"


def test_an_allowed_but_different_model_is_also_a_substitution(tmp_path):
    key = {"x": 2}
    client, _ = _client(tmp_path, [_recording(key, _chat("raon-1-mini-preview", {"ok": True}))])
    with pytest.raises(ModelSubstituted):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key=key, at=AT)


def test_an_answer_that_does_not_name_its_model_is_thrown_away(tmp_path):
    key = {"x": 3}
    response = _chat(TEXT_MODEL, {"ok": True})
    del response["model"]
    client, ledger = _client(tmp_path, [_recording(key, response)])
    with pytest.raises(BadAnswer, match="name its model"):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key=key, at=AT)
    assert ledger.calls[0].ok is False


def test_an_answer_outside_the_schema_is_an_error(tmp_path):
    key = {"x": 4}
    client, ledger = _client(tmp_path, [_recording(key, _chat(TEXT_MODEL, {"ok": "yes", "extra": 1}))])
    with pytest.raises(BadAnswer):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key=key, at=AT)
    assert ledger.calls[0].ok is False and ledger.calls[0].model_returned == TEXT_MODEL


def test_no_recording_is_an_error_in_the_ledger():
    client, ledger = _client()
    with pytest.raises(NoRecording):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key={"never": "recorded"}, at=AT)
    assert ledger.calls[0].ok is False


def test_replayed_transcript_with_cost_from_the_price_table():
    client, ledger = _client()
    answer = client.transcribe((FIXTURES / "audio" / "ru-callout.ogg").read_bytes(), "ru-callout.ogg", at=AT)
    assert answer.data["text"].startswith("Привет, это Марина")
    call = ledger.calls[0]
    assert (call.ok, call.source, call.model_returned, call.duration_ms) == (True, "recorded", "whisper-large-v3-turbo", 1050)
    assert call.cost_usd == pytest.approx(0.04 * 8.529 / 3600, abs=1e-6)


def test_replayed_card_reading_with_token_cost():
    client, ledger = _client()
    image = (FIXTURES / "cards" / "c1-scan.png").read_bytes()
    answer = client.read_card(image, "image/png", model=TEXT_MODEL, at=AT)
    truth = json.loads((FIXTURES / "cards" / "certs-truth.json").read_text())["c1"]
    assert answer.data == truth
    call = ledger.calls[0]
    assert (call.input_tokens, call.output_tokens) == (1013, 450)
    assert call.cost_usd == round(1013 * 0.25 / 1e6 + 450 * 1.00 / 1e6, 6)  # stored at 6 decimals
    omni = client.read_card(image, "image/png", model=SECOND_READER_MODEL, at=AT)
    assert omni.data == truth and ledger.calls[1].model_returned == SECOND_READER_MODEL


def test_every_recording_is_consistent_and_names_an_allowed_model():
    recordings = Recordings(FIXTURES / "models")
    assert len(recordings._index) >= 24
    for path in sorted((FIXTURES / "models").glob("*.json")):
        raw = json.loads(path.read_text())
        assert raw["input_sha256"] == canonical_key(raw["input"]), path.name
        assert raw["model"] in ALLOWED_MODELS and raw["response"]["model"] == raw["model"], path.name
        assert isinstance(raw["synthetic"], bool) and raw["source"], path.name


def test_two_recordings_for_one_input_are_refused(tmp_path):
    rec = _recording({"x": 5}, _chat(TEXT_MODEL, {"ok": True}))
    with pytest.raises(ValueError, match="two recordings"):
        _client(tmp_path, [rec, rec])


def test_live_mode_needs_a_key_from_the_environment(monkeypatch):
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    with pytest.raises(ModelError, match="VULTR_INFERENCE_API_KEY"):
        ModelClient(MemoryLedger(), mode="live")
    with pytest.raises(ValueError):
        ModelClient(MemoryLedger(), mode="sometimes")


def test_live_mode_checks_answers_the_same_way_without_network():
    sent = []

    def fake_transport(path, request):
        sent.append((path, request["json"]["model"], request["json"]["response_format"]["type"]))
        return _chat("qwen-something", {"ok": True}), 42

    ledger = MemoryLedger()
    client = ModelClient(ledger, mode="live", api_key="test-only", transport=fake_transport)
    with pytest.raises(ModelSubstituted):
        client.chat_json(purpose="probe", system="s", user="u", schema=SCHEMA, key={"x": 6}, at=AT)
    assert sent == [("/chat/completions", TEXT_MODEL, "json_schema")]
    assert ledger.calls[0].source == "live" and ledger.calls[0].ok is False


def test_live_transcript_asks_for_plain_json_and_prices_by_the_audio_length():
    # Vultr now rejects response_format=verbose_json (400 "use json or text") and json carries no duration,
    # so the length comes from the Ogg file itself (ffprobe: ru-callout.ogg 8.529 s).
    sent = []

    def transport(path, request):
        sent.append((path, request))
        return answers.pop(0), 900

    # the live answer of 07.11 (probe on ru-callout.ogg): no model name, usage in billed seconds
    answers = [{"text": "Привет, это Марина", "usage": {"type": "duration", "seconds": 9}},
               {"text": "Привет, это Марина"},
               {"text": "Привет", "model": "some-other-model"}]
    ledger = MemoryLedger()
    client = ModelClient(ledger, mode="live", api_key="test-key", transport=transport)
    audio = (FIXTURES / "audio" / "ru-callout.ogg").read_bytes()
    answer = client.transcribe(audio, "ru-callout.ogg", at=AT)
    assert sent[0][1]["multipart"]["fields"]["response_format"] == "json"
    assert answer.data["text"] == "Привет, это Марина" and answer.data["duration"] == 9
    call = ledger.calls[0]
    assert (call.ok, call.model_returned) == (True, "whisper-large-v3-turbo")
    assert call.cost_usd == pytest.approx(0.04 * 9 / 3600, abs=1e-6)
    assert answer.record.ref["model_named_by"] == "request"
    no_usage = client.transcribe(audio, "ru-callout.ogg", at=AT)  # no usage either: the Ogg length prices it
    assert no_usage.data["duration"] == pytest.approx(8.529, abs=0.01)
    assert ledger.calls[1].cost_usd == pytest.approx(0.04 * 8.529 / 3600, abs=1e-6)
    with pytest.raises(ModelSubstituted):  # naming another model still throws the answer away
        client.transcribe(audio, "ru-callout.ogg", at=AT)


@pytest.mark.parametrize("name, seconds", [("en-callout.ogg", 6.920), ("es-callout.ogg", 5.277),
                                           ("ru-callout-0930.ogg", 9.226), ("whatif-ru.ogg", 2.012)])
def test_ogg_length_matches_ffprobe(name, seconds):
    from coachcover.models import ogg_seconds
    assert ogg_seconds((FIXTURES / "audio" / name).read_bytes()) == pytest.approx(seconds, abs=0.01)
    assert ogg_seconds(b"not an ogg file") is None
