from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import random
import re
import sys
import time
from typing import Any, Callable

from .config import Settings
from .cache import LLMCache
from .models import CRITERIA


JSONSchema = dict[str, Any]
INVALID_JSON_NOTE = "Предыдущий ответ был невалидным JSON; верни строго валидный JSON, короче."


def _close_unbalanced(value: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            stack.append(character)
        elif character in "]}" and stack:
            expected = "[" if character == "]" else "{"
            if stack[-1] == expected:
                stack.pop()
    result = value.rstrip()
    if in_string:
        result += '"'
    result = re.sub(r",\s*$", "", result)
    result += "".join("]" if item == "[" else "}" for item in reversed(stack))
    return result


def _drop_incomplete_array_item(value: str) -> str | None:
    stack: list[dict[str, Any]] = []
    in_string = False
    escaped = False
    for index, character in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            stack.append({"kind": character, "position": index, "last_comma": None})
        elif character in "]}" and stack:
            expected = "[" if character == "]" else "{"
            if stack[-1]["kind"] == expected:
                stack.pop()
        elif character == "," and stack and stack[-1]["kind"] == "[":
            stack[-1]["last_comma"] = index
    arrays = [item for item in stack if item["kind"] == "["]
    if not arrays:
        return None
    target = arrays[-1]
    cut = target["last_comma"] if target["last_comma"] is not None else target["position"] + 1
    return _close_unbalanced(value[:cut])


def repair_json(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    start = value.find("{")
    if start < 0:
        raise ValueError("Модель не вернула JSON-объект")
    value = value[start:]
    end = value.rfind("}")
    cropped = value[: end + 1] if end >= 0 else value
    candidates = [
        cropped,
        re.sub(r",\s*([}\]])", r"\1", cropped),
        _drop_incomplete_array_item(value),
        _close_unbalanced(value),
    ]
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return candidate
        except json.JSONDecodeError as error:
            last_error = error
    if last_error:
        raise last_error
    raise ValueError("Не удалось восстановить JSON-ответ модели")


def extract_json(raw: str) -> dict[str, Any]:
    value = raw.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = json.loads(repair_json(value))
    if not isinstance(parsed, dict):
        raise ValueError("Ожидался JSON-объект")
    return parsed


def parse_retry_delay(message: str) -> float | None:
    """Извлекает секунды из RetryInfo, например ``"retryDelay": "37s"``."""
    match = re.search(
        r"retry[_ ]?delay[\"']?\s*[:=]\s*[\"']?([0-9]+(?:\.[0-9]+)?)\s*s\b",
        message,
        flags=re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def _max_retry_delay() -> float:
    try:
        return max(0.0, float(os.getenv("LLM_MAX_RETRY_DELAY", "90")))
    except ValueError:
        return 90.0


def _sanitize_error(error: Exception, secrets: tuple[str, ...]) -> str:
    summary = mask_secrets(" ".join(str(error).split()), secrets)
    return (summary or type(error).__name__)[:200]


def mask_secrets(message: str, secrets: tuple[str, ...] = ()) -> str:
    summary = message
    for secret in secrets:
        if secret:
            summary = summary.replace(secret, "[секрет скрыт]")
    summary = re.sub(r"AIza[0-9A-Za-z_-]+", "[секрет скрыт]", summary)
    summary = re.sub(r"AQ\.[A-Za-z0-9_-]{20,}", "[секрет скрыт]", summary)
    summary = re.sub(r"sk-[0-9A-Za-z_-]+", "[секрет скрыт]", summary)
    summary = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer)(\s*[:=]\s*|\s+)[^\s,;}]+",
        r"\1\2[секрет скрыт]",
        summary,
    )
    return summary


def _http_status(error: Exception) -> str:
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    if hasattr(status, "value"):
        status = status.value
    return str(status or "неизвестен")


def _retry(
    operation: Callable[[], str], attempts: int = 6, secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            return extract_json(operation())
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            retryable = status == 429 or (isinstance(status, int) and status >= 500)
            message = str(error).lower()
            retryable = (
                retryable
                or "429" in message
                or bool(re.search(r"\b5\d\d\b", message))
                or "rate limit" in message
                or "resource_exhausted" in message
            )
            if isinstance(error, (json.JSONDecodeError, ValueError)) and status is None:
                retryable = False
            if not retryable or attempt == attempts - 1:
                raise
            requested_delay = parse_retry_delay(str(error))
            if requested_delay is not None and requested_delay > 300:
                hours = requested_delay / 3600
                raise RuntimeError(
                    f"Исчерпана квота Gemini (сервер просит ждать ~{hours:.1f} ч). "
                    "Попробуйте позже, другую модель (--model) или другой ключ."
                ) from error
            uncapped_delay = (
                requested_delay + 1.0
                if requested_delay is not None
                else (2 ** (attempt + 2)) + random.random()
            )
            delay = min(_max_retry_delay(), uncapped_delay)
            summary = _sanitize_error(error, secrets)
            print(
                f"[validator] LLM временно недоступна (HTTP {_http_status(error)}: {summary}); "
                f"повтор {attempt + 2}/{attempts} через {delay:.1f} с",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("Исчерпаны попытки обращения к модели")


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate_json(
        self, system: str, user: str, schema: JSONSchema | None = None, operation: str = "llm"
    ) -> dict[str, Any]: ...


def _save_raw_response(enabled: bool, operation: str, raw: str) -> None:
    if not enabled:
        return
    directory = Path("runs") / "debug"
    directory.mkdir(parents=True, exist_ok=True)
    safe_operation = re.sub(r"[^a-zA-Z0-9_-]+", "_", operation)[:60]
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    (directory / f"{timestamp}_{safe_operation}.txt").write_text(raw, encoding="utf-8")


def _json_request(
    call: Callable[[str], str], user: str, secrets: tuple[str, ...]
) -> dict[str, Any]:
    try:
        return _retry(lambda: call(user), secrets=secrets)
    except (json.JSONDecodeError, ValueError):
        corrected_user = user + "\n\n" + INVALID_JSON_NOTE
        try:
            return _retry(lambda: call(corrected_user), secrets=secrets)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("Модель дважды вернула невалидный JSON") from error


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, settings: Settings, verbose: bool = False):
        if not settings.gemini_api_key:
            raise ValueError("Для Gemini задайте GEMINI_API_KEY в .env")
        from google import genai
        from google.genai import types

        self.model = settings.model
        self.max_output_tokens = settings.gemini_max_output_tokens
        self.verbose = verbose
        self._thinking_config = None
        if settings.gemini_thinking:
            try:
                self._thinking_config = (
                    types.ThinkingConfig(thinking_level="HIGH")
                    if "gemini-3" in self.model.lower()
                    else types.ThinkingConfig(thinking_budget=-1)
                )
            except (AttributeError, TypeError, ValueError):
                self._thinking_config = None
        self._secrets = (settings.gemini_api_key,)
        self.client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(api_version=settings.gemini_api_version),
        )

    def generate_json(
        self, system: str, user: str, schema: JSONSchema | None = None, operation: str = "llm"
    ) -> dict[str, Any]:
        from google.genai import types

        def call(current_user: str) -> str:
            def invoke(thinking_config: Any) -> Any:
                config_kwargs = dict(
                    system_instruction=system,
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=schema,
                    max_output_tokens=self.max_output_tokens,
                )
                if thinking_config is not None:
                    config_kwargs["thinking_config"] = thinking_config
                return self.client.models.generate_content(
                    model=self.model,
                    contents=current_user,
                    config=types.GenerateContentConfig(**config_kwargs),
                )

            try:
                response = invoke(self._thinking_config)
            except Exception as error:
                message = str(error).lower()
                unsupported = isinstance(error, TypeError) or any(
                    token in message for token in ("thinking", "unsupported", "unknown field")
                )
                if self._thinking_config is None or not unsupported:
                    raise
                print(
                    "[validator] ThinkingConfig не поддерживается; повтор без режима thinking",
                    file=sys.stderr,
                    flush=True,
                )
                self._thinking_config = None
                response = invoke(None)
            raw = response.text or "{}"
            _save_raw_response(self.verbose, operation, raw)
            candidate = response.candidates[0] if getattr(response, "candidates", None) else None
            finish_reason = getattr(candidate, "finish_reason", None)
            reason = getattr(finish_reason, "name", str(finish_reason or "")).upper()
            if "MAX_TOKENS" in reason:
                raise RuntimeError(
                    "Ответ Gemini обрезан из-за лимита токенов. "
                    "Увеличьте GEMINI_MAX_OUTPUT_TOKENS или уменьшите размер входа."
                )
            return raw

        return _json_request(call, user, self._secrets)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings, verbose: bool = False):
        if not settings.openai_api_key and not settings.openai_base_url:
            raise ValueError("Для OpenAI задайте OPENAI_API_KEY или OPENAI_BASE_URL")
        from openai import OpenAI

        self.model = settings.model
        self.verbose = verbose
        self._secrets = (settings.openai_api_key or "",)
        self.client = OpenAI(api_key=settings.openai_api_key or "local", base_url=settings.openai_base_url)

    def generate_json(
        self, system: str, user: str, schema: JSONSchema | None = None, operation: str = "llm"
    ) -> dict[str, Any]:
        def call(current_user: str) -> str:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": current_user}],
            )
            raw = response.choices[0].message.content or "{}"
            _save_raw_response(self.verbose, operation, raw)
            finish_reason = str(getattr(response.choices[0], "finish_reason", "") or "").lower()
            if finish_reason in {"length", "max_tokens"}:
                raise RuntimeError("Ответ модели обрезан из-за лимита токенов.")
            return raw

        return _json_request(call, user, self._secrets)


class MockProvider(LLMProvider):
    """Детерминированный офлайн-провайдер для тестов конвейера."""

    name = "mock"
    model = "mock-deterministic"

    def __init__(self, _: Settings, verbose: bool = False):
        self.operations: list[str] = []

    def generate_json(
        self, system: str, user: str, schema: JSONSchema | None = None, operation: str = "llm"
    ) -> dict[str, Any]:
        self.operations.append(operation)
        if user.startswith("ОПЕРАЦИЯ: SEGMENT"):
            return self._segment(user.partition("ДОКУМЕНТ:\n")[2])
        if user.startswith("ОПЕРАЦИЯ: CHECK"):
            raw = user.partition("ТРЕБОВАНИЯ:\n")[2].partition("\nПОЛНЫЙ ДОКУМЕНТ:")[0]
            requirements = json.loads(raw)
            checks = []
            for index, item in enumerate(requirements):
                verdicts = {key: "ok" for key in CRITERIA}
                findings = []
                rewrite = ""
                if index == 0:
                    verdicts["complete"] = "violation"
                    quote = item.get("quote", "")
                    findings = [{
                        "criterion": "complete", "severity": "major", "quote": quote,
                        "location": item.get("section", "Раздел не определён"),
                        "explanation": "В mock-режиме отмечена неполнота проверяемого условия.",
                        "fix": "Добавить измеримые условия, границы и ожидаемый результат.",
                    }]
                    rewrite = f"Уточнить проверяемые условия для требования: {quote}"
                checks.append({
                    "requirement_id": item["report_id"], "verdicts": verdicts,
                    "findings": findings, "rewrite": rewrite,
                })
            return {"checks": checks}
        if user.startswith("ОПЕРАЦИЯ: COVERAGE"):
            fragments = json.loads(user.partition("ФРАГМЕНТЫ:\n")[2])
            return {
                "requirements": [
                    {
                        "source_id": "—", "section": item.get("section", "Раздел не определён"),
                        "short_text": item.get("text", "")[:180], "quote": item.get("text", "")[:200],
                    }
                    for item in fragments if len(item.get("text", "").strip()) >= 15
                ],
                "skipped": [
                    {"quote": item.get("text", ""), "reason": "короткий ненормативный фрагмент"}
                    for item in fragments if len(item.get("text", "").strip()) < 15
                ],
            }
        if user.startswith("ОПЕРАЦИЯ: CONTEXT_EXTRACT"):
            documents = json.loads(user.partition("ДОКУМЕНТЫ:\n")[2])
            items = []
            for document in documents:
                quote = next((line.strip() for line in document.get("text", "").splitlines() if len(line.strip()) > 10), "")
                if quote:
                    items.append({"text": quote[:160], "quote": quote[:200], "source_file": document["source_file"]})
            return {key: (items if key == "goals" else []) for key in ("goals", "scope_in", "scope_out", "constraints", "glossary", "systems", "policies")}
        if user.startswith("ОПЕРАЦИЯ: CONTEXT_CHECK"):
            data = json.loads(user.partition("ДАННЫЕ:\n")[2])
            goals = data.get("context", {}).get("goals", [])
            requirements = data.get("requirements", [])
            return {"findings": [], "traceability_matrix": [
                {"goal": goal.get("text", ""), "goal_quote": goal.get("quote", ""),
                 "requirement_ids": [item["report_id"] for item in requirements[:1]],
                 "acceptance_ids": [], "api_ids": [], "status": "partial"}
                for goal in goals
            ]}
        if user.startswith("ОПЕРАЦИЯ: TEST_CASES"):
            requirements = json.loads(user.partition("ТРЕБОВАНИЯ:\n")[2])
            return {"test_cases": [
                {"id": f"TC-{index:02d}", "requirement_ids": [item["report_id"]],
                 "preconditions": ["Система доступна"], "steps": ["Выполнить действие"],
                 "expected_result": item.get("short_text", "Ожидаемый результат достигнут"),
                 "boundary_values": []}
                for index, item in enumerate(requirements[:3], 1)
            ]}
        return {"verdicts": {key: "ok" for key in ("complete", "consistent", "feasible", "comprehensible", "able_to_be_validated", "ranked_importance_stability", "modifiable", "traceable")}, "findings": [], "missing_sections": [], "traceability": "Явных дефектов в mock-режиме не сформировано.", "traceability_matrix": []}

    @staticmethod
    def _segment(text: str) -> dict[str, Any]:
        items: list[dict[str, str]] = []
        section = "Начало документа"
        source_pattern = re.compile(r"(?:\*\*)?([A-Za-zА-Яа-я]+-\d+)(?:\.\*\*)?")
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped.startswith("#"):
                section = stripped.lstrip("#").strip()
                continue
            candidate = re.sub(r"^[-*]\s+", "", stripped)
            candidate = re.sub(r"^\d+\.\s+", "", candidate)
            is_table_row = stripped.startswith("|") and not re.match(r"^\|?\s*:?-+", stripped)
            is_requirement = bool(re.match(r"^(?:[-*]\s+|\d+\.\s+)", stripped)) or is_table_row
            if not is_requirement or len(candidate) < 15 or "---" in candidate:
                continue
            match = source_pattern.search(candidate)
            quote = candidate.strip(" |")
            items.append({"source_id": match.group(1) if match else "—", "section": section, "short_text": re.sub(r"[*`|]", "", quote)[:180], "quote": quote})
        if not items:
            quote = next((line.strip() for line in text.splitlines() if len(line.strip()) > 15), text.strip()[:200])
            items.append({"source_id": "—", "section": section, "short_text": quote[:180], "quote": quote})
        return {"requirements": items}


class ProviderRouter(LLMProvider):
    def __init__(self, providers: list[LLMProvider], cache: LLMCache):
        if not providers:
            raise ValueError("Не удалось настроить ни одного LLM-провайдера")
        self.providers = providers
        self.cache = cache
        self.current = 0
        self.name = providers[0].name
        self.model = providers[0].model
        self.calls = 0
        self.cache_hits = 0
        self.requests = 0
        self.models_by_step: dict[str, list[str]] = {}

    @staticmethod
    def _fallback_error(error: Exception) -> bool:
        message = str(error).lower()
        status = getattr(error, "status_code", None) or getattr(error, "code", None)
        return (
            "исчерпана квота" in message
            or status == 429
            or (isinstance(status, int) and status >= 500)
            or "429" in message
            or bool(re.search(r"\b5\d\d\b", message))
        )

    def generate_json(
        self, system: str, user: str, schema: JSONSchema | None = None, operation: str = "llm"
    ) -> dict[str, Any]:
        self.requests += 1
        last_error: Exception | None = None
        for index in range(self.current, len(self.providers)):
            provider = self.providers[index]
            key = self.cache.key(provider.name, provider.model, system, user, schema)
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                self.current = index
                self.name, self.model = provider.name, provider.model
                self.models_by_step.setdefault(operation, []).append(f"{provider.name}:{provider.model} (кэш)")
                return cached
            try:
                self.calls += 1
                result = provider.generate_json(system, user, schema=schema, operation=operation)
                self.cache.put(key, result)
                self.current = index
                self.name, self.model = provider.name, provider.model
                self.models_by_step.setdefault(operation, []).append(f"{provider.name}:{provider.model}")
                return result
            except Exception as error:
                last_error = error
                if not self._fallback_error(error) or index + 1 >= len(self.providers):
                    raise
                next_provider = self.providers[index + 1]
                print(
                    f"[validator] fallback: {provider.name}:{provider.model} → "
                    f"{next_provider.name}:{next_provider.model}",
                    file=sys.stderr,
                    flush=True,
                )
                self.current = index + 1
        if last_error:
            raise last_error
        raise RuntimeError("LLM-провайдеры недоступны")


def _raw_provider(settings: Settings, verbose: bool) -> LLMProvider:
    provider_class = {"gemini": GeminiProvider, "openai": OpenAIProvider, "mock": MockProvider}[
        settings.provider
    ]
    return provider_class(settings, verbose)


def create_provider(settings: Settings, verbose: bool = False) -> ProviderRouter:
    providers = [_raw_provider(settings, verbose)]
    for item in settings.llm_fallback.split(","):
        item = item.strip()
        if not item:
            continue
        provider_name, separator, model = item.partition(":")
        if not separator or provider_name not in {"gemini", "openai", "mock"} or not model:
            raise ValueError("LLM_FALLBACK: ожидается список provider:model")
        if provider_name == settings.provider and model == settings.model:
            continue
        fallback_settings = replace(settings, provider=provider_name, model=model)
        try:
            providers.append(_raw_provider(fallback_settings, verbose))
        except ValueError as error:
            print(f"[validator] fallback пропущен: {mask_secrets(str(error))}", file=sys.stderr)
    return ProviderRouter(providers, LLMCache(enabled=settings.cache_enabled))
