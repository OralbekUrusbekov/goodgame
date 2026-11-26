from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from .verifier import quote_exists


def _findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = [
        finding for check in payload.get("requirement_checks", [])
        for finding in check.get("findings", []) if isinstance(finding, dict)
    ]
    result.extend(
        finding for finding in payload.get("set_check", {}).get("findings", [])
        if isinstance(finding, dict)
    )
    return result


def compare_with_previous(current: dict[str, Any], previous_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    previous = json.loads(Path(previous_path).read_text(encoding="utf-8"))
    old_findings, new_findings = _findings(previous), _findings(current)

    def match(left: dict, right: dict) -> bool:
        return left.get("criterion") == right.get("criterion") and (
            quote_exists(str(left.get("quote", "")), str(right.get("quote", "")), threshold=0.8)
            or quote_exists(str(right.get("quote", "")), str(left.get("quote", "")), threshold=0.8)
        )

    remaining = [item for item in new_findings if any(match(item, old) for old in old_findings)]
    new = [item for item in new_findings if not any(match(item, old) for old in old_findings)]
    fixed = [item for item in old_findings if not any(match(item, now) for now in new_findings)]
    return {"fixed": fixed, "new": new, "remaining": remaining}
