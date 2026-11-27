import pytest

from validator.cache import LLMCache
from validator.llm import LLMProvider, ProviderRouter, _retry, extract_json, parse_retry_delay


def test_parse_retry_delay_from_retry_info() -> None:
    message = '429 RESOURCE_EXHAUSTED details: {"retryDelay": "37s"}'

    assert parse_retry_delay(message) == 37.0


def test_parse_retry_delay_returns_none_when_absent() -> None:
    assert parse_retry_delay("429 Too Many Requests") is None


class RateLimitError(Exception):
    status_code = 429


def test_retry_delay_is_capped_and_log_is_sanitized(monkeypatch, capsys) -> None:
    sleeps = []
    calls = 0
    secret = "private-api-key-value"

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RateLimitError(f'quota api_key={secret} {{"retryDelay": "120s"}}')
        return '{"ok": true}'

    monkeypatch.setenv("LLM_MAX_RETRY_DELAY", "90")
    monkeypatch.setattr("validator.llm.time.sleep", sleeps.append)

    assert _retry(operation, secrets=(secret,)) == {"ok": True}
    assert sleeps == [90.0]
    log = capsys.readouterr().err
    assert "HTTP 429" in log
    assert "через 90.0 с" in log
    assert secret not in log


def test_daily_quota_delay_fails_immediately(monkeypatch) -> None:
    sleeps = []

    def operation() -> str:
        raise RateLimitError('{"retryDelay": "63464s"}')

    monkeypatch.setattr("validator.llm.time.sleep", sleeps.append)

    with pytest.raises(RuntimeError, match=r"Исчерпана квота Gemini.*17\.6 ч"):
        _retry(operation)
    assert sleeps == []


def test_json_repair_removes_trailing_commas() -> None:
    raw = '```json\n{"requirements":[{"quote":"точно",}],}\n```'

    assert extract_json(raw) == {"requirements": [{"quote": "точно"}]}


def test_json_repair_drops_last_truncated_array_item() -> None:
    raw = '{"requirements":[{"quote":"готово"},{"quote":"оборвано'

    assert extract_json(raw) == {"requirements": [{"quote": "готово"}]}


def test_all_fallback_quotas_report_models_and_wait(tmp_path) -> None:
    class Exhausted(LLMProvider):
        name = "gemini"

        def __init__(self, model: str, delay: int):
            self.model = model
            self.delay = delay

        def generate_json(self, system, user, schema=None, operation="llm"):
            error = RuntimeError(f'HTTP 429 {{"retryDelay": "{self.delay}s"}}')
            error.status_code = 429
            raise error

    router = ProviderRouter(
        [Exhausted("first", 20), Exhausted("second", 45)],
        LLMCache(tmp_path / "off", enabled=False),
    )

    with pytest.raises(RuntimeError, match=r"Квота исчерпана у всех моделей.*first.*second.*45 с"):
        router.generate_json("system", "user")


def test_404_model_falls_back_and_is_skipped_for_later_router(tmp_path) -> None:
    calls = {"gone": 0}

    class Gone(LLMProvider):
        name, model = "gemini", "gone-regression-model"
        def generate_json(self, system, user, schema=None, operation="llm"):
            calls["gone"] += 1
            error = RuntimeError("404 NOT_FOUND model no longer available")
            error.status_code = 404
            raise error

    class Working(LLMProvider):
        name, model = "gemini", "working-regression-model"
        def generate_json(self, system, user, schema=None, operation="llm"):
            return {"ok": True}

    first = ProviderRouter([Gone(), Working()], LLMCache(tmp_path / "one", enabled=False))
    second = ProviderRouter([Gone(), Working()], LLMCache(tmp_path / "two", enabled=False))
    assert first.generate_json("s", "u", operation="segment") == {"ok": True}
    assert second.generate_json("s", "u", operation="check") == {"ok": True}
    assert calls["gone"] == 1
    assert first.model_trace[0]["reason"] == "модель недоступна"


def test_fallback_failure_lists_every_model_and_reason(tmp_path) -> None:
    class Gone(LLMProvider):
        name = "gemini"
        def __init__(self, model): self.model = model
        def generate_json(self, system, user, schema=None, operation="llm"):
            error = RuntimeError("404 not found")
            error.status_code = 404
            raise error

    router = ProviderRouter([Gone("gone-a"), Gone("gone-b")], LLMCache(tmp_path, enabled=False))
    with pytest.raises(RuntimeError, match=r"gone-a — модель недоступна.*gone-b — модель недоступна"):
        router.generate_json("s", "u")
