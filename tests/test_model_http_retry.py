"""A model call that hangs is tried once more inside the same 90 s budget; an HTTP error answer is not retried."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from coachcover import models
from coachcover.models import BadAnswer, MemoryLedger, ModelClient, ModelError


class _Reply:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._data


def _client() -> ModelClient:
    return ModelClient(MemoryLedger(), mode="live", api_key="test-key", base_url="http://inference.invalid")


def _fake_urlopen(script: list, seen: list):
    """Each call takes the next step of the script: an exception to raise or a payload to answer with."""

    def urlopen(req, timeout):
        seen.append(timeout)
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return _Reply(step)

    return urlopen


def test_budget_is_two_tries_of_45_seconds():
    assert models.HTTP_TRIES == 2
    assert models.HTTP_TRY_SECONDS == 45
    assert models.HTTP_TRIES * models.HTTP_TRY_SECONDS == 90


def test_timeout_then_answer_is_retried_and_succeeds(monkeypatch):
    seen: list = []
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        _fake_urlopen([TimeoutError("The read operation timed out"), {"ok": 1}], seen))
    payload, ms = _client()._http("/chat/completions", {"json": {"q": 1}})
    assert payload == {"ok": 1}
    assert seen == [45, 45]
    assert ms >= 0


def test_network_error_then_answer_is_retried(monkeypatch):
    seen: list = []
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        _fake_urlopen([urllib.error.URLError("connection reset"), {"ok": 2}], seen))
    payload, _ = _client()._http("/chat/completions", {"json": {"q": 1}})
    assert payload == {"ok": 2}
    assert len(seen) == 2


def test_two_timeouts_end_as_a_network_error(monkeypatch):
    seen: list = []
    monkeypatch.setattr(models.urllib.request, "urlopen",
                        _fake_urlopen([TimeoutError("timed out"), TimeoutError("timed out")], seen))
    with pytest.raises(ModelError, match="network error after 2 tries"):
        _client()._http("/chat/completions", {"json": {"q": 1}})
    assert seen == [45, 45]


def test_http_error_answer_is_not_retried(monkeypatch):
    seen: list = []
    err = urllib.error.HTTPError("http://inference.invalid", 500, "boom", {}, io.BytesIO(b"server error"))
    monkeypatch.setattr(models.urllib.request, "urlopen", _fake_urlopen([err, {"ok": 3}], seen))
    with pytest.raises(BadAnswer, match="HTTP 500"):
        _client()._http("/chat/completions", {"json": {"q": 1}})
    assert len(seen) == 1
