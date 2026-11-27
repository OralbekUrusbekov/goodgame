from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    check_batch_size: int = 12
    gemini_api_key: str | None = None
    gemini_api_version: str = "v1beta"
    gemini_max_output_tokens: int = 65536
    gemini_thinking: bool = True
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    cache_enabled: bool = True
    llm_fallback: str = ""
    llm_concurrency: int = 2


def _batch_size(value: int | str | None) -> int:
    try:
        size = int(value if value is not None else os.getenv("CHECK_BATCH_SIZE", "12"))
    except (TypeError, ValueError) as error:
        raise ValueError("CHECK_BATCH_SIZE должен быть целым положительным числом") from error
    if size < 1:
        raise ValueError("CHECK_BATCH_SIZE должен быть целым положительным числом")
    return size


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} должен быть целым положительным числом") from error
    if value < 1:
        raise ValueError(f"{name} должен быть целым положительным числом")
    return value


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} должен быть on/off или true/false")


def load_config(
    provider: str | None = None, model: str | None = None, batch_size: int | None = None,
    cache_enabled: bool | None = None,
) -> Settings:
    # An explicitly configured environment (notably the offline mock test mode)
    # is complete on its own and must not inspect a local .env file.
    selected_value = provider or os.getenv("LLM_PROVIDER")
    if selected_value is None:
        load_dotenv()
        selected_value = os.getenv("LLM_PROVIDER", "gemini")
    selected = selected_value.lower()
    if selected not in {"gemini", "openai", "mock"}:
        raise ValueError("LLM_PROVIDER должен быть одним из: gemini, openai, mock")
    default_model = {
        "gemini": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "mock": "mock-deterministic",
    }[selected]
    selected_model = model or default_model
    fallback = os.getenv("LLM_FALLBACK")
    if fallback is None:
        fallback = (
            "gemini:gemini-3.5-flash-lite,gemini:gemini-2.5-flash"
            if selected == "gemini"
            else ""
        )
    return Settings(
        provider=selected,
        model=selected_model,
        check_batch_size=_batch_size(batch_size),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gemini_api_version=os.getenv("GEMINI_API_VERSION", "v1beta") or "v1beta",
        gemini_max_output_tokens=_positive_int_env("GEMINI_MAX_OUTPUT_TOKENS", 65536),
        gemini_thinking=_bool_env("GEMINI_THINKING", True),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_base_url=os.getenv("OPENAI_BASE_URL"),
        cache_enabled=_bool_env("LLM_CACHE", True) if cache_enabled is None else cache_enabled,
        llm_fallback=fallback,
        llm_concurrency=_positive_int_env("LLM_CONCURRENCY", 2),
    )
