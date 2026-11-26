from datetime import datetime, timezone
from pathlib import Path

from validator.loader import load_document
from validator.models import CRITERIA, Requirement, RequirementCheck, SetCheck
from validator.reporter import render_report


def test_report_contains_six_required_sections(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Тест\nСистема отвечает за секунду.\n", encoding="utf-8")
    document = load_document(source)
    requirement = Requirement("R-01", "—", "Тест", "Ответ за секунду", "Система отвечает за секунду.")
    check = RequirementCheck("R-01", {key: "ok" for key in CRITERIA}, [])
    set_check = SetCheck({"complete": "ok"}, [], [], "Связи читаемы.")

    report = render_report(document, [requirement], [check], set_check, "mock", "mock-deterministic", str(source), datetime(2026, 1, 1, tzinfo=timezone.utc))

    for number, title in enumerate(("Паспорт прогона", "Как агент нарезал требования", "Проверка каждого требования", "Проверка всего набора требований", "Сводка", "Итог"), 1):
        assert f"## {number}. {title}" in report
    assert "ID в отчёте | ID в документе | Раздел | Строка | Краткая формулировка | Цитата" in report
