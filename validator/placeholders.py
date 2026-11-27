"""Deterministic detection of unfilled template fragments.

The scanner deliberately uses generic document-template patterns only.  It does
not consume eval fixtures and does not depend on an LLM.
"""
from __future__ import annotations

from collections import OrderedDict
import re

from .loader import Document, SourceLine
from .models import Finding


_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("маркер ожидания", re.compile(r"(?<![\w-])(?:TBD|TODO|FIXME)(?![\w-])", re.IGNORECASE)),
    ("указание заполнить позднее", re.compile(
        r"\b(?:заполня(?:ется|ются)|будет\s+заполн(?:ен[аоы]?|яться)|to\s+be\s+(?:filled|completed))\b",
        re.IGNORECASE,
    )),
    ("явно незаполненное поле", re.compile(
        r"\b(?:не\s+заполн(?:ен|ена|ено|ены|яется|яются)|not\s+(?:filled|completed))\b",
        re.IGNORECASE,
    )),
    ("неотмеченный выбор", re.compile(r"\b(?:да|yes)\s*/\s*(?:нет|no)\b", re.IGNORECASE)),
)

_MANDATORY_PART = re.compile(
    r"(?:тестирован|испытан|test(?:ing)?|согласован|согласующ|подпис|approv|"
    r"\bверси(?:я|и|ю|онн)|\bversion\b|\brevision\b)",
    re.IGNORECASE,
)
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
_TESTING = re.compile(r"тестирован|испытан|test(?:ing)?", re.IGNORECASE)


def _table_cells(value: str) -> list[str] | None:
    if "|" not in value or _TABLE_SEPARATOR.match(value):
        return None
    stripped = value.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _placeholder_kinds(source_line: SourceLine) -> list[str]:
    line = source_line.text.strip()
    kinds = [label for label, pattern in _TEXT_PATTERNS if pattern.search(line)]
    cells = _table_cells(line)
    if cells is not None:
        if len(cells) >= 2 and all(not cell for cell in cells):
            kinds.append("пустая строка таблицы")
        elif (len(cells) == 2 or _is_mandatory(source_line)) and any(
            cell in {"—", "-"} for cell in cells
        ) and any(
            cell not in {"", "—", "-"} for cell in cells
        ):
            kinds.append("пустое значение таблицы")
    return list(dict.fromkeys(kinds))


def _is_mandatory(line: SourceLine) -> bool:
    return bool(_MANDATORY_PART.search(f"{line.section}\n{line.text}"))


def find_placeholder_findings(document: Document) -> list[Finding]:
    """Return one exact-quote completeness finding per affected section."""
    grouped: OrderedDict[str, list[tuple[SourceLine, list[str]]]] = OrderedDict()
    section_lines: OrderedDict[str, list[SourceLine]] = OrderedDict()
    seen: set[tuple[str, str]] = set()
    for line in document.lines:
        section_lines.setdefault(line.section, []).append(line)
        quote = line.text.strip()
        if not quote:
            continue
        kinds = _placeholder_kinds(line)
        key = (line.section, quote)
        if kinds and key not in seen:
            seen.add(key)
            grouped.setdefault(line.section, []).append((line, kinds))

    for section, lines in section_lines.items():
        if not _MANDATORY_PART.search(section):
            continue
        heading_line = next((line for line in lines if line.text.strip()), None)
        if heading_line is None:
            continue
        heading_match = re.match(r"^(#{1,6})\s+", heading_line.text.strip())
        if heading_match:
            next_nonempty = next(
                (line for line in document.lines
                 if line.number > heading_line.number and line.text.strip()),
                None,
            )
            child_match = re.match(r"^(#{1,6})\s+", next_nonempty.text.strip()) if next_nonempty else None
            if child_match and len(child_match.group(1)) > len(heading_match.group(1)):
                continue
        meaningful = [
            line for line in lines
            if line.text.strip()
            and not re.match(r"^#{1,6}\s+", line.text.strip())
            and line.text.strip().casefold() != section.strip().casefold()
            and not _TABLE_SEPARATOR.match(line.text.strip())
        ]
        if not meaningful:
            quote = heading_line.text.strip()
            key = (section, quote)
            if key not in seen:
                seen.add(key)
                grouped.setdefault(section, []).append((heading_line, ["пустой обязательный раздел"]))

    findings: list[Finding] = []
    for section, entries in grouped.items():
        primary = entries[0][0]
        quotes = [line.text.strip() for line, _ in entries]
        kinds = list(dict.fromkeys(kind for _, labels in entries for kind in labels))
        # An empty testing section blocks verification of implementation.
        # Version/approval/review checklists are important process metadata,
        # but do not by themselves block a concrete product scenario.
        critical = bool(_TESTING.search(section)) and any(
            "пустой обязательный раздел" in labels or any(
                marker in label
                for label in labels
                for marker in ("заполнить позднее", "незаполненное")
            )
            for _, labels in entries
        )
        severity = "critical" if critical else "major"
        explanation = (
            f"Раздел содержит незаполненные элементы шаблона ({', '.join(kinds)}); "
            "из-за этого спецификация неполна."
        )
        fix = (
            "Заполнить обязательную часть документа конкретными проверяемыми данными и убрать плейсхолдеры."
            if critical else
            "Заменить плейсхолдеры конкретными значениями либо явно обосновать неприменимость полей."
        )
        findings.append(Finding(
            criterion="complete",
            severity=severity,
            explanation=explanation,
            fix=fix,
            quote=quotes[0],
            related_quotes=quotes[1:],
            location=f"{section}, строка {primary.number}",
            quote_verified=True,
        ))
    return findings
