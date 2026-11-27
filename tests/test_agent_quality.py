from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from eval.matcher import evaluate, load_cases
from validator.checker import check_requirements
from validator.config import Settings
from validator.coverage import extract_units
from validator.loader import load_document
from validator.models import CRITERIA, Finding, Requirement, RequirementCheck, SetCheck
from validator.pipeline import PartialResultError, run_pipeline
from validator.prompts import FINDING_SCHEMA, check_prompt, completeness_prompt
from validator.reporter import build_payload, render_report, report_summary
from validator.set_checker import build_traceability_matrix
from validator.verifier import cluster_findings, verify_findings


def _finding(**overrides) -> Finding:
    values = {
        "criterion": "complete", "severity": "critical",
        "explanation": "Не описано сохранение значения в сценарии изменения настройки.",
        "fix": "Уточнить правило.", "quote": "Система сохраняет значение.",
        "location": "Правила, строка 3", "requirement_ids": ["R-01"],
    }
    values.update(overrides)
    return Finding(**values)


def test_unverified_finding_is_reported_but_does_not_affect_summary(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("# Правила\nСистема сохраняет значение.\n", encoding="utf-8")
    document = load_document(source)
    finding = _finding(quote="Несуществующая цитата")
    verify_findings(document, [finding])
    check = RequirementCheck("R-01", {**{key: "ok" for key in CRITERIA}, "complete": "violation"}, [finding])
    requirement = Requirement("R-01", "—", "Правила", "Сохранение", "Система сохраняет значение.")
    set_check = SetCheck({"complete": "ok"}, [])

    payload = build_payload({}, [requirement], [check], set_check)
    report = render_report(document, [requirement], [check], set_check, "mock", "mock", str(source))

    assert payload["summary"]["finding_count"] == 0
    assert payload["summary"]["decision"] == "Да"
    assert payload["metadata"]["unverified_findings"] == 1
    serialized = payload["requirement_checks"][0]["findings"][0]
    assert serialized["unverified"] is True
    assert "Неподтверждённые выводы (не влияют на итог)" in report


def test_flattened_table_cells_and_lines_become_exact_related_quotes(tmp_path: Path) -> None:
    source = tmp_path / "table.md"
    source.write_text(
        "# Контракт\n| Метод | PATCH |\n| Режим | пакетный |\nПервая часть правила.\nВторая часть правила.\n",
        encoding="utf-8",
    )
    document = load_document(source)
    table = _finding(quote="Метод PATCH")
    lines = _finding(quote="Первая часть правила Вторая часть правила")

    verified = verify_findings(document, [table, lines])

    assert verified[0].quote == "Метод"
    assert verified[0].related_quotes == ["PATCH"]
    assert verified[0].quote_verified
    assert verified[1].quote == "Первая часть правила."
    assert verified[1].related_quotes == ["Вторая часть правила."]


def test_cluster_merges_same_defect_but_not_different_meaning() -> None:
    common = "Операция изменяет сохранённое значение."
    first = _finding(criterion="correct", severity="major", quote=common,
                     explanation="Метод не соответствует изменению сохранённого значения.")
    second = _finding(criterion="appropriate", severity="critical", quote=common,
                      explanation="Выбранный метод не соответствует изменению сохранённого значения.",
                      requirement_ids=["R-02"])
    different = _finding(criterion="complete", severity="major", quote=common,
                         explanation="Не определены права доступа для пользователя.")

    clustered = cluster_findings([first, second, different])

    assert len(clustered) == 2
    assert clustered[0].severity == "critical"
    assert clustered[0].criteria == ["appropriate", "correct"]
    assert clustered[0].requirement_ids == ["R-01", "R-02"]
    assert clustered[1].explanation.startswith("Не определены права")


def test_prompts_require_safe_fixes_severity_and_acceptance_preconditions(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Правило\nСистема возвращает результат.\n", encoding="utf-8")
    document = load_document(source)
    requirement = Requirement("R-01", "—", "Правило", "Результат", "Система возвращает результат.")
    check_system, _ = check_prompt(document, [requirement])
    complete_system, _ = completeness_prompt(document, [requirement])

    assert FINDING_SCHEMA["properties"]["fix_kind"]["enum"] == ["edit", "decision"]
    assert "включительность границ" in check_system
    assert "fix_kind=decision" in check_system
    assert "конкретного сценария" in check_system
    assert "предусловия, действие и наблюдаемый результат" in check_system
    assert "проверь все связанные" in complete_system


def test_process_templates_are_excluded_and_short_quote_uses_context(tmp_path: Path) -> None:
    source = tmp_path / "locations.md"
    source.write_text(
        "# Список\n- Выбрать значение или оставить пустым.\n"
        "# JSON\n```json\n{\"mode\": \"или\"}\n```\n"
        "# Таблица\n| Поле | Значение или значение по умолчанию |\n"
        "# Ревью\n| Проверка | Да / Нет / Не требуется |\n| Согласовал | Имя |\n",
        encoding="utf-8",
    )
    document = load_document(source)
    units, skipped = extract_units(document)

    assert all("Да / Нет / Не требуется" not in unit.text for unit in units)
    assert any("процессные метаданные" in item.reason for item in skipped)
    assert document.line_for("или", context='{"mode": "или"}', section="JSON") == 5
    assert document.location_for("или", context="Значение или значение", section="Таблица") == "Таблица, строка 8"


def test_deterministic_traceability_lists_requirements_without_acceptance() -> None:
    requirements = [
        Requirement("R-01", "G-1", "Цель", "Экспорт", "Цель — экспортировать итоговый файл."),
        Requirement("R-02", "F-1", "Функции", "Экспорт", "Система экспортирует итоговый файл."),
        Requirement("R-03", "F-1", "Критерии приёмки", "Экспорт", "Когда выбран экспорт, файл загружен."),
        Requirement("R-04", "F-1", "API", "Экспорт", "POST /exports создаёт итоговый файл."),
        Requirement("R-05", "F-2", "Функции", "Удаление", "Система удаляет черновик."),
    ]

    matrix = build_traceability_matrix(requirements)
    by_requirement = {row["requirement_ids"][0]: row for row in matrix}

    assert by_requirement["R-02"]["status"] == "covered"
    assert by_requirement["R-02"]["acceptance_ids"] == ["R-03"]
    assert by_requirement["R-02"]["api_ids"] == ["R-04"]
    assert by_requirement["R-05"]["status"] == "missing"


def test_partial_result_error_preserves_segmented_requirements(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Правила\n- Система сохраняет значение.\n", encoding="utf-8")

    class Provider:
        name = "mock"
        model = "broken"
        calls = cache_hits = requests = 0
        models_by_step = {}

    requirement = Requirement("R-01", "—", "Правила", "Сохранение", "Система сохраняет значение.")
    monkeypatch.setattr("validator.pipeline.create_provider", lambda settings, verbose=False: Provider())
    monkeypatch.setattr("validator.pipeline.segment", lambda *args, **kwargs: (
        [requirement], [], {"percent": 100.0, "covered_units": 1, "total_units": 1, "skipped_units": []}
    ))
    monkeypatch.setattr("validator.pipeline.check_requirements", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(PartialResultError) as caught:
        run_pipeline(str(source), str(tmp_path / "report.md"), Settings("mock", "mock", cache_enabled=False))

    error = caught.value
    assert error.stage == "проверка требований"
    assert error.payload["requirements"][0]["report_id"] == "R-01"
    assert error.payload["requirement_checks"] == []
    assert error.payload["set_check"]["findings"] == []
    assert error.payload["requirements"][0]["status"] == "not_checked"
    assert error.payload["verdict"]["status"] == "incomplete"
    assert error.payload["verdict"]["ready"] is False
    assert "ЧАСТИЧНЫЙ ОТЧЁТ" in error.report_markdown
    assert "Проверка не завершена" in error.report_markdown


def test_requirement_batches_run_concurrently_but_return_source_order(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Правила\nПервое правило.\nВторое правило.\n", encoding="utf-8")
    document = load_document(source)
    requirements = [
        Requirement("R-01", "—", "Правила", "Первое", "Первое правило."),
        Requirement("R-02", "—", "Правила", "Второе", "Второе правило."),
    ]

    class ConcurrentProvider:
        barrier = threading.Barrier(2)

        def generate_json(self, system, user, schema=None, operation="llm"):
            batch = json.loads(user.partition("ТРЕБОВАНИЯ:\n")[2].partition("\nПОЛНЫЙ ДОКУМЕНТ:")[0])
            self.barrier.wait(timeout=2)
            return {"checks": [{
                "requirement_id": item["report_id"],
                "verdicts": {key: "ok" for key in CRITERIA},
                "findings": [], "rewrite": "",
            } for item in batch]}

    checks = check_requirements(
        document, requirements, ConcurrentProvider(), batch_size=1, concurrency=2
    )

    assert [item.requirement_id for item in checks] == ["R-01", "R-02"]


def test_control_eval_negative_case_and_all_anchors_exist() -> None:
    root = Path(__file__).parents[1]
    cases = load_cases(root / "eval/cases/control.yaml")
    source = (root / "eval/fixtures/control.md").read_text(encoding="utf-8")
    assert all(anchor in source for case in cases for anchor in case["anchor_quotes"])

    report = {
        "requirement_checks": [{"findings": [{
            "criterion": "consistent", "quote": cases[-1]["anchor_quotes"][0],
            "related_quotes": [], "quote_verified": True,
        }]}],
        "set_check": {"findings": []},
    }
    result = evaluate(report, [cases[-1]])
    assert result["strict_recall"] == 0.0
    assert result["negative_false_positives"] == 1


def test_external_eval_anchors_are_literal_document_quotes() -> None:
    root = Path(__file__).parents[1]
    source = (root / "docs/requirements.md").read_text(encoding="utf-8")
    cases = load_cases(root / "eval/cases/requirements_card_alerts.yaml")
    assert all(anchor in source for case in cases for anchor in case["anchor_quotes"])
