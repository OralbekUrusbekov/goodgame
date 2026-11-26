import pytest

from validator.llm import _retry, extract_json, parse_retry_delay


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
