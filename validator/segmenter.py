from __future__ import annotations

from dataclasses import replace
import math
import re
from typing import Any

from .coverage import (
    COVERAGE_SCHEMA, SkippedUnit, coverage_prompt, extract_units, uncovered_units, unit_is_covered
)
from .llm import LLMProvider
from .loader import Document
from .models import Requirement
from .prompts import SEGMENT_SCHEMA, segmentation_prompt
from .verifier import normalize as fuzzy_normalize, quote_exists

def split_for_segmentation(
    document: Document, threshold: int = 6000, max_chunks: int = 3
) -> list[str]:
    text = document.text
    if len(text) <= threshold:
        return [text]
    headings = list(re.finditer(r"^##\s+.+$", text, flags=re.MULTILINE))
    if not headings:
        paragraphs = re.split(r"(?<=\n)\s*\n", text)
        if len(paragraphs) == 1:
            lines = text.splitlines(keepends=True)
            size = max(1, math.ceil(len(lines) / min(max_chunks, math.ceil(len(text) / threshold))))
            paragraphs = ["".join(lines[index : index + size]) for index in range(0, len(lines), size)]
        units = paragraphs
    else:
        units = []
        preamble = text[: headings[0].start()]
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            unit = text[heading.start() : end]
            if index == 0:
                unit = preamble + unit
            units.append(unit)
    desired = min(max_chunks, max(2, math.ceil(len(text) / threshold)), len(units))
    if desired <= 1:
        return [text]
    chunks: list[str] = []
    current = ""
    for index, unit in enumerate(units):
        remaining_units = len(units) - index
        remaining_groups = desired - len(chunks)
        target = max(1, (len(text) - sum(len(item) for item in chunks)) // remaining_groups)
        if (
            current and len(chunks) < desired - 1 and len(current) + len(unit) > target
            and remaining_units >= remaining_groups - 1
        ):
            chunks.append(current)
            current = unit
        else:
            current += unit
    if current:
        chunks.append(current)
    return chunks


def _requirements_from_raw(raw_items: list[dict[str, Any]]) -> list[Requirement]:
    result: list[Requirement] = []
    seen: set[str] = set()
    for raw in raw_items:
        requirement = Requirement.from_dict(raw, len(result) + 1)
        key = fuzzy_normalize(requirement.quote)
        if key and key not in seen:
            seen.add(key)
            result.append(requirement)
    return result


def _quote_position(document: Document, quote: str) -> int:
    direct = document.text.casefold().find(quote.casefold())
    if direct >= 0:
        return direct
    position = 0
    for line in document.text.splitlines(keepends=True):
        if quote_exists(quote, line):
            return position
        position += len(line)
    return len(document.text) + 1


def segment(
    document: Document, provider: LLMProvider, include_coverage: bool = False
) -> tuple[list[Requirement], list[str]] | tuple[list[Requirement], list[str], dict[str, Any]]:
    raw_items: list[dict] = []
    chunks = split_for_segmentation(document)
    for chunk_index, chunk in enumerate(chunks, 1):
        system, user = segmentation_prompt(replace(document, text=chunk))
        response = provider.generate_json(
            system, user, schema=SEGMENT_SCHEMA, operation=f"segment_{chunk_index}"
        )
        items = response.get("requirements", [])
        if isinstance(items, list):
            raw_items.extend(item for item in items if isinstance(item, dict))
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Модель не выделила ни одного требования")
    initial_requirements = _requirements_from_raw(raw_items)
    units, deterministic_skipped = extract_units(document)
    uncovered = uncovered_units(units, initial_requirements)
    llm_skipped: list[SkippedUnit] = []
    if uncovered:
        system, user = coverage_prompt(uncovered)
        response = provider.generate_json(
            system, user, schema=COVERAGE_SCHEMA, operation="coverage"
        )
        raw_items.extend(
            item for item in response.get("requirements", []) if isinstance(item, dict)
        )
        skipped_by_text = {unit.text: unit for unit in uncovered}
        for item in response.get("skipped", []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("quote", ""))
            source_unit = skipped_by_text.get(text)
            llm_skipped.append(SkippedUnit(
                text=text,
                section=source_unit.section if source_unit else "Раздел не определён",
                reason=str(item.get("reason", "не нормативный фрагмент")),
            ))
    requirements = _requirements_from_raw(raw_items)
    requirements.sort(key=lambda item: _quote_position(document, item.quote))
    for index, requirement in enumerate(requirements, 1):
        requirement.report_id = f"R-{index:02d}"
    warnings: list[str] = []
    for requirement in requirements:
        requirement.quote_verified = bool(requirement.quote and quote_exists(requirement.quote, document.text))
        requirement.line_number = document.line_for(requirement.quote)
        if not requirement.quote_verified:
            warnings.append(f"{requirement.report_id}: цитата сегмента не найдена в источнике")
        if not requirement.section or requirement.section == "Раздел не определён":
            requirement.section = document.location_for(requirement.quote).split(", строка", 1)[0]
    if not requirements:
        raise ValueError("Ответ сегментации не содержит корректных объектов")
    deliberately_skipped = [*deterministic_skipped, *llm_skipped]
    skipped_text = {fuzzy_normalize(item.text) for item in deliberately_skipped}
    measurable_units = [unit for unit in units if fuzzy_normalize(unit.text) not in skipped_text]
    covered_count = sum(unit_is_covered(unit, requirements) for unit in measurable_units)
    coverage = {
        "percent": round(100 * covered_count / len(measurable_units), 1) if measurable_units else 100.0,
        "covered_units": covered_count,
        "total_units": len(measurable_units),
        "skipped_units": [
            {"text": item.text, "section": item.section, "reason": item.reason}
            for item in deliberately_skipped
        ],
    }
    if include_coverage:
        return requirements, warnings, coverage
    return requirements, warnings
