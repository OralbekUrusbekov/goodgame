from __future__ import annotations

from pathlib import Path

from validator.loader import load_document
from validator.models import Requirement
from validator.placeholders import find_placeholder_findings
from validator.set_checker import check_set


def test_placeholder_scan_groups_exact_quotes_by_section_and_severity(tmp_path: Path) -> None:
    source = tmp_path / "template.md"
    source.write_text(
        "# Спецификация\n\n"
        "## Версия документа\n"
        "| Поле | Значение |\n|---|---|\n| Версия | — |\n\n"
        "## Тестирование\n"
        "Заполняется после передачи в тестирование.\n\n"
        "## Согласование\n"
        "| Роль | Имя | Подпись |\n|---|---|---|\n| | | |\n\n"
        "## Дополнительные сведения\n"
        "| Риск | TBD |\n"
        "Проверка выполнена: Да / Нет\n",
        encoding="utf-8",
    )
    document = load_document(source)

    findings = find_placeholder_findings(document)

    assert [finding.location.split(", строка", 1)[0] for finding in findings] == [
        "Версия документа", "Тестирование", "Согласование", "Дополнительные сведения"
    ]
    assert [finding.severity for finding in findings] == ["critical", "critical", "critical", "major"]
    assert all(finding.criterion == "complete" for finding in findings)
    assert all(finding.quote in document.text for finding in findings)
    assert all(
        quote in document.text
        for finding in findings for quote in finding.related_quotes
    )
    extra = findings[-1]
    assert extra.quote == "| Риск | TBD |"
    assert extra.related_quotes == ["Проверка выполнена: Да / Нет"]


class _DuplicateProvider:
    name = "mock"
    model = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, system, user, schema=None, operation="llm"):
        self.calls += 1
        finding = {
            "criterion": "complete", "severity": "major", "quote": "Статус: TBD",
            "location": "Описание", "explanation": "Поле не заполнено.",
            "fix": "Заполнить поле.",
        }
        return {
            "verdicts": {key: "ok" for key in (
                "complete", "consistent", "feasible", "comprehensible",
                "able_to_be_validated", "ranked_importance_stability", "modifiable", "traceable",
            )},
            "findings": [finding] if self.calls == 1 else [],
            "missing_sections": [], "traceability": "", "traceability_matrix": [],
        }


def test_set_checker_merges_deterministic_and_llm_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("# Документ\n\n## Описание\nСтатус: TBD\n", encoding="utf-8")
    document = load_document(source)
    requirement = Requirement("R-01", "—", "Описание", "Статус", "Статус: TBD")

    result = check_set(document, [requirement], _DuplicateProvider())

    matching = [finding for finding in result.findings if finding.quote == "Статус: TBD"]
    assert len(matching) == 1
    assert result.verdicts["complete"] == "violation"


def test_empty_mandatory_section_is_critical(tmp_path: Path) -> None:
    source = tmp_path / "empty-section.md"
    source.write_text(
        "# Документ\n\n## Согласование\n\n## Описание\nТекст требования.\n",
        encoding="utf-8",
    )

    findings = find_placeholder_findings(load_document(source))

    assert len(findings) == 1
    assert findings[0].severity == "critical"
    assert findings[0].quote == "## Согласование"
    assert findings[0].location == "Согласование, строка 3"
