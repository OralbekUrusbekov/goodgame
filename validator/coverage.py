from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .loader import Document
from .models import Requirement
from .verifier import normalize, quote_exists


@dataclass(frozen=True)
class SourceUnit:
    text: str
    section: str
    position: int


@dataclass(frozen=True)
class SkippedUnit:
    text: str
    section: str
    reason: str


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[\w]+", normalize(value)))


def _template_row(text: str) -> bool:
    cells = [re.sub(r"[*`]", "", item).strip() for item in text.strip("|").split("|")]
    nonempty = [item for item in cells if item and item != "—"]
    if not nonempty:
        return True
    option = re.compile(r"^(?:да|нет|не требуется)(?:\s*/\s*(?:да|нет|не требуется))*$", re.I)
    return len(cells) > 1 and all(not item or item == "—" or option.fullmatch(item) for item in cells[1:])


def extract_units(document: Document) -> tuple[list[SourceUnit], list[SkippedUnit]]:
    lines = document.text.splitlines(keepends=True)
    units: list[SourceUnit] = []
    skipped: list[SkippedUnit] = []
    section = "Начало документа"
    position = 0
    paragraph: list[str] = []
    paragraph_start = 0
    in_fence = False
    fence: list[str] = []
    fence_start = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        text = " ".join(item.strip() for item in paragraph).strip()
        if text:
            units.append(SourceUnit(text, section, paragraph_start))
        paragraph = []

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if stripped.startswith("```"):
            flush_paragraph()
            if not in_fence:
                in_fence, fence, fence_start = True, [], position
            else:
                content = "\n".join(fence).strip()
                if content:
                    units.append(SourceUnit(content, section, fence_start))
                in_fence = False
            position += len(raw)
            continue
        if in_fence:
            fence.append(raw.rstrip("\r\n"))
            position += len(raw)
            continue
        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            section = heading.group(1).strip()
        elif not stripped:
            flush_paragraph()
        elif stripped.startswith("|"):
            flush_paragraph()
            if re.match(r"^\|?\s*:?-+", stripped) or re.match(r"^\|?\s*:?-+", next_line):
                pass
            elif _template_row(stripped):
                skipped.append(SkippedUnit(stripped, section, "пустая или шаблонная строка таблицы"))
            else:
                units.append(SourceUnit(stripped, section, position))
        elif re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", stripped):
            flush_paragraph()
            text = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped).strip()
            if text:
                units.append(SourceUnit(text, section, position))
        else:
            if not paragraph:
                paragraph_start = position
            paragraph.append(stripped)
        position += len(raw)
    flush_paragraph()
    if in_fence and fence:
        units.append(SourceUnit("\n".join(fence).strip(), section, fence_start))
    return units, skipped


def unit_is_covered(unit: SourceUnit, requirements: list[Requirement]) -> bool:
    unit_tokens = _tokens(unit.text)
    for requirement in requirements:
        if quote_exists(requirement.quote, unit.text) or quote_exists(unit.text, requirement.quote):
            return True
        quote_tokens = _tokens(requirement.quote)
        if unit_tokens and len(unit_tokens & quote_tokens) / len(unit_tokens) >= 0.6:
            return True
    return False


def uncovered_units(units: list[SourceUnit], requirements: list[Requirement]) -> list[SourceUnit]:
    return [unit for unit in units if not unit_is_covered(unit, requirements)]


def coverage_prompt(units: list[SourceUnit]) -> tuple[str, str]:
    system = """Ты доизвлекаешь пропущенные атомарные требования из фрагментов спецификации.
Для каждого фрагмента реши, содержит ли он нормативное правило, цель, условие, действие, ожидаемый результат,
ограничение, интерфейсный контракт, пример поведения или проверяемое утверждение. Если да — верни одно или
несколько требований с точной цитатой до 200 символов. Если это только метаданные, пояснение без нормы,
заголовок или пустой шаблон — добавь фрагмент в skipped с краткой причиной.
Не придумывай сведения вне фрагментов. Отвечай только валидным JSON."""
    payload = [
        {"fragment_id": f"U-{index:03d}", "section": unit.section, "text": unit.text}
        for index, unit in enumerate(units, 1)
    ]
    import json
    return system, "ОПЕРАЦИЯ: COVERAGE\nФРАГМЕНТЫ:\n" + json.dumps(payload, ensure_ascii=False)


COVERAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"}, "section": {"type": "string"},
                    "short_text": {"type": "string"}, "quote": {"type": "string"},
                },
                "required": ["source_id", "section", "short_text", "quote"],
            },
        },
        "skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"quote": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["quote", "reason"],
            },
        },
    },
    "required": ["requirements", "skipped"],
}
