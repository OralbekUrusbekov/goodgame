from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class LLMCache:
    def __init__(self, directory: str | Path = ".cache/llm", enabled: bool = True):
        self.directory = Path(directory)
        self.enabled = enabled
        if enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(provider: str, model: str, system: str, user: str, schema: dict | None) -> str:
        payload = json.dumps(
            {"provider": provider, "model": model, "system": system, "user": user, "schema": schema},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self.directory / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
