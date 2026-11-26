from __future__ import annotations

from collections.abc import Callable

from .llm import LLMProvider
from .loader import Document
from .models import CRITERIA, Finding, Requirement, RequirementCheck
from .prompts import CHECK_SCHEMA, check_prompt


def check_requirements(
    document: Document,
    requirements: list[Requirement],
    provider: LLMProvider,
    batch_size: int = 12,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[RequirementCheck]:
    result: list[RequirementCheck] = []
    total_batches = max(1, (len(requirements) + batch_size - 1) // batch_size)
    for batch_index, start in enumerate(range(0, len(requirements), batch_size), 1):
        batch = requirements[start : start + batch_size]
        indexed = _request_checks(document, batch, provider)
        missing = [item for item in batch if item.report_id not in indexed]
        if missing:
            indexed.update(_request_checks(document, missing, provider))
        still_missing = [item.report_id for item in batch if item.report_id not in indexed]
        if still_missing:
            raise ValueError(
                "Модель не вернула проверку требований после повторной попытки: "
                + ", ".join(still_missing)
            )
        for requirement in batch:
            raw = indexed[requirement.report_id]
            supplied = raw.get("verdicts", {}) if isinstance(raw.get("verdicts", {}), dict) else {}
            verdicts = {criterion: supplied.get(criterion, "n.a.") for criterion in CRITERIA}
            findings = [Finding.from_dict(item, requirement.report_id) for item in raw.get("findings", []) if isinstance(item, dict)]
            rewrite = str(raw.get("rewrite", "")).strip() if findings else ""
            result.append(RequirementCheck(requirement.report_id, verdicts, findings, rewrite))
        if progress_callback:
            progress_callback(batch_index, total_batches)
    return result


def _request_checks(
    document: Document, requirements: list[Requirement], provider: LLMProvider
) -> dict[str, dict]:
    system, user = check_prompt(document, requirements)
    response = provider.generate_json(system, user, schema=CHECK_SCHEMA, operation="check")
    return {
        str(item.get("requirement_id")): item
        for item in response.get("checks", [])
        if isinstance(item, dict) and item.get("requirement_id")
    }
