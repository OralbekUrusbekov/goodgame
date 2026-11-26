from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import subprocess

from . import __version__
from .loader import Document
from .models import Finding, Requirement, RequirementCheck, SetCheck
from .verifier import dedupe_findings


CRITERION_RU = {
    "necessary": "Необходимость", "appropriate": "Уместность", "unambiguous": "Однозначность",
    "complete": "Полнота", "singular": "Единичность", "feasible": "Реализуемость",
    "verifiable": "Проверяемость", "correct": "Корректность", "conforming": "Соответствие форме",
    "consistent": "Согласованность", "comprehensible": "Понятность",
    "able_to_be_validated": "Валидируемость", "ranked_importance_stability": "Приоритет и стабильность",
    "modifiable": "Модифицируемость", "traceable": "Трассируемость",
}
VERDICT_RU = {"ok": "OK", "violation": "Нарушение", "n.a.": "Н/П"}
SEVERITY_RU = {"critical": "критический", "major": "существенный", "minor": "незначительный"}


def agent_version(root: Path | None = None) -> str:
    try:
        cwd = root or Path.cwd()
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip())
        return f"{__version__}+{commit}{'-dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        return f"{__version__}+unknown"


def git_tag(root: Path | None = None) -> str:
    try:
        return subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=root or Path.cwd(),
            capture_output=True, text=True, check=True,
        ).stdout.strip() or "—"
    except (OSError, subprocess.SubprocessError):
        return "—"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _finding_block(finding: Finding) -> list[str]:
    ids = f" ({', '.join(finding.requirement_ids)})" if finding.requirement_ids else ""
    lines = [f"- **{SEVERITY_RU.get(finding.severity, finding.severity).capitalize()} — {CRITERION_RU.get(finding.criterion, finding.criterion)}{ids}.** {finding.explanation}",
             f"  - Цитата: «{finding.quote}»", f"  - Место: {finding.location}"]
    if finding.related_quotes:
        lines.append("  - Связанные цитаты: " + "; ".join(f"«{q}»" for q in finding.related_quotes))
    lines.append(f"  - Исправление: {finding.fix}")
    lines.append(f"  - Проверка цитаты: {'подтверждена' if finding.quote_verified else 'цитата не подтверждена'}")
    if finding.confidence:
        lines.append(f"  - Уверенность: {finding.confidence}")
    if finding.expert_confirmation:
        lines.append("  - Статус: требует подтверждения экспертом")
    return lines


def render_report(document: Document, requirements: list[Requirement], checks: list[RequirementCheck], set_check: SetCheck,
                  provider: str, model: str, source_path: str, run_at: datetime | None = None,
                  coverage: dict | None = None, metadata: dict | None = None,
                  context_result: dict | None = None, context_used: bool = False,
                  test_cases: list[dict] | None = None, diff_result: dict | None = None) -> str:
    timestamp = run_at or datetime.now().astimezone()
    metadata = metadata or {}
    all_findings = dedupe_findings(
        [finding for check in checks for finding in check.findings] + set_check.findings
    )
    severity = Counter(item.severity for item in all_findings)
    criteria = Counter(item.criterion for item in all_findings)
    coverage_text = f"{coverage['percent']}%" if coverage and "percent" in coverage else "не рассчитано"
    lines = [f"# Отчёт о проверке требований: {document.title}", "", "## 1. Паспорт прогона", "",
             f"- Название документа: {document.title}", f"- Путь: `{source_path}`",
             f"- Дата и время: {timestamp.isoformat(timespec='seconds')}", f"- Провайдер и модель: {provider} / {model}",
             f"- Версия агента: {metadata.get('agent_version', agent_version())}",
             f"- Git tag: {metadata.get('git_tag', git_tag())}",
             f"- Покрытие исходных фрагментов: {coverage_text}",
             f"- LLM-вызовов: {metadata.get('llm_calls', '—')}; cache hits: {metadata.get('cache_hits', '—')}",
             f"- Длительность: {metadata.get('duration_seconds', '—')} с",
             f"- SHA-256 входного файла: `{metadata.get('input_sha256', '—')}`",
             f"- Модели по шагам: {json.dumps(metadata.get('models_by_step', {}), ensure_ascii=False)}",
             "", "## 2. Как агент нарезал требования", "",
             "| ID в отчёте | ID в документе | Раздел | Строка | Краткая формулировка | Цитата |",
             "|---|---|---|---:|---|---|"]
    for req in requirements:
        quote = req.quote + (" [цитата сегмента не подтверждена]" if not req.quote_verified else "")
        lines.append(f"| {req.report_id} | {_escape(req.source_id)} | {_escape(req.section)} | {req.line_number or '—'} | {_escape(req.short_text)} | «{_escape(quote)}» |")
    lines.extend(["", "## 3. Проверка каждого требования", ""])
    by_id = {item.report_id: item for item in requirements}
    for check in checks:
        req = by_id[check.requirement_id]
        lines.extend([f"### {check.requirement_id} — {req.short_text}", "", "| Критерий | Вердикт |", "|---|---|"])
        lines.extend(f"| {CRITERION_RU.get(key, key)} | {VERDICT_RU.get(value, value)} |" for key, value in check.verdicts.items())
        lines.extend(["", "**Нарушения**", ""])
        if check.findings:
            for finding in check.findings:
                lines.extend(_finding_block(finding))
        else:
            lines.append("Нарушений не обнаружено.")
        if check.rewrite:
            lines.extend(["", "**Предлагаемая формулировка**", "", check.rewrite])
        lines.append("")
    lines.extend(["## 4. Проверка всего набора требований", "", "| Критерий | Вердикт |", "|---|---|"])
    lines.extend(f"| {CRITERION_RU.get(key, key)} | {VERDICT_RU.get(value, value)} |" for key, value in set_check.verdicts.items())
    lines.extend(["", f"Трассируемость: {set_check.traceability or 'Оценка не предоставлена.'}", ""])
    if set_check.missing_sections:
        lines.append("Отмеченные отсутствующие разделы: " + ", ".join(set_check.missing_sections) + ".")
        lines.append("")
    if set_check.findings:
        for finding in set_check.findings:
            lines.extend(_finding_block(finding))
    else:
        lines.append("Нарушений на уровне набора не обнаружено.")
    summary = report_summary(requirements, checks, set_check)
    lines.extend(["", "## 5. Сводка", "", f"- Требований выделено: {len(requirements)}.", f"- Нарушений: {len(all_findings)}.",
                  f"- По серьёзности: critical — {severity['critical']}, major — {severity['major']}, minor — {severity['minor']}.",
                  "- По критериям: " + (", ".join(f"{CRITERION_RU.get(k, k)} — {v}" for k, v in sorted(criteria.items())) or "нет") + ".",
                  "", "### Критические дефекты", ""])
    for title, key in (("Непроверяемые", "unverifiable"), ("Противоречивые", "contradictory"),
                       ("Неполные", "incomplete"), ("Прочие критические", "other")):
        lines.extend([f"#### {title}", ""])
        group = summary["critical_groups"][key]
        if group:
            lines.extend(
                f"- {', '.join(item['requirement_ids']) or 'набор'} — {item['explanation']} ({item['location']})"
                for item in group
            )
        else:
            lines.append("Не выявлены.")
        lines.append("")
    decision, justification = summary["decision"], summary["justification"]
    lines.extend(["## 6. Итог", "", f"**Можно ли начинать разработку и тестирование: {decision}.** {justification}", "", "### Что исправить в первую очередь", ""])
    if summary["top_fixes"]:
        lines.extend(f"{index}. {item['fix']}" for index, item in enumerate(summary["top_fixes"], 1))
    else:
        lines.append("1. Дополнительные исправления по результатам этого прогона не требуются.")
    lines.extend(["", "## 7. Соответствие контексту проекта и трассируемость", ""])
    if context_used and context_result:
        if context_result.get("findings"):
            for item in context_result["findings"]:
                lines.extend([
                    f"- **{item.get('type', 'context')} / {item.get('severity', 'major')}** — {item.get('explanation', '')}",
                    f"  - Требование: «{item.get('requirement_quote', '')}» ({Path(source_path).name}, строка {item.get('requirement_line') or '—'}; "
                    f"{'подтверждена' if item.get('requirement_quote_verified') else 'не подтверждена'})",
                    f"  - Контекст: «{item.get('context_quote', '')}» ({item.get('context_file', '')}, строка {item.get('context_line') or '—'})",
                    f"  - Исправление: {item.get('fix', '')}",
                    f"  - Уверенность: {item.get('confidence', '—')}; контекстная цитата: "
                    f"{'подтверждена' if item.get('context_quote_verified') else 'не подтверждена'}",
                ])
        else:
            lines.append("Конфликты с контекстом не выявлены.")
        matrix = context_result.get("traceability_matrix", [])
    else:
        lines.append("Внешний контекст проекта не передан; показана внутридокументная трассируемость.")
        matrix = set_check.traceability_matrix
    lines.extend(["", "| Цель | Требования | Критерии приёмки | API | Статус |", "|---|---|---|---|---|"])
    for row in matrix:
        lines.append(
            f"| {_escape(str(row.get('goal', '')))} | {', '.join(row.get('requirement_ids', [])) or '—'} | "
            f"{', '.join(row.get('acceptance_ids', [])) or '—'} | {', '.join(row.get('api_ids', [])) or '—'} | {row.get('status', '—')} |"
        )
    if test_cases is not None:
        lines.extend(["", "## 8. Черновик тест-кейсов", ""])
        for case in test_cases:
            lines.extend([
                f"### {case.get('id', 'TC')} — {', '.join(case.get('requirement_ids', []))}", "",
                "**Предусловия:** " + "; ".join(case.get("preconditions", [])),
                "", "**Шаги:**", *[f"{i}. {step}" for i, step in enumerate(case.get("steps", []), 1)],
                "", f"**Ожидаемый результат:** {case.get('expected_result', '')}",
                f"**Граничные значения:** {'; '.join(case.get('boundary_values', []))}", "",
            ])
    if diff_result is not None:
        lines.extend(["", "## 9. Изменения относительно прошлого прогона", ""])
        for title, key in (("Исправлено", "fixed"), ("Новые", "new"), ("Остались", "remaining")):
            lines.extend([f"### {title}", ""])
            items = diff_result.get(key, [])
            lines.extend([f"- {item.get('criterion', '—')}: {item.get('explanation', '')}" for item in items] or ["Нет."])
    return "\n".join(lines) + "\n"


def report_summary(
    requirements: list[Requirement], checks: list[RequirementCheck], set_check: SetCheck
) -> dict:
    findings = dedupe_findings([finding for check in checks for finding in check.findings] + set_check.findings)
    severity = Counter(item.severity for item in findings)
    criteria = Counter(item.criterion for item in findings)
    groups = {"unverifiable": [], "contradictory": [], "incomplete": [], "other": []}
    for item in findings:
        key = (
            "unverifiable" if item.criterion == "verifiable" else
            "contradictory" if item.criterion == "consistent" else
            "incomplete" if item.criterion == "complete" else
            "other" if item.severity == "critical" else None
        )
        if key:
            groups[key].append(asdict(item))
    decision = "Нет" if severity["critical"] else ("С оговорками" if severity["major"] else "Да")
    justification = (
        "есть критические дефекты" if severity["critical"]
        else "существенные дефекты требуют исправления" if severity["major"]
        else "выявленных блокирующих дефектов нет"
    )
    fixes: dict[str, dict] = {}
    for item in findings:
        key = " ".join(item.fix.casefold().split())
        entry = fixes.setdefault(key, {"fix": item.fix, "severity": item.severity, "requirement_ids": []})
        entry["requirement_ids"] = list(dict.fromkeys([*entry["requirement_ids"], *item.requirement_ids]))
        if {"critical": 3, "major": 2, "minor": 1}.get(item.severity, 0) > {"critical": 3, "major": 2, "minor": 1}.get(entry["severity"], 0):
            entry["severity"] = item.severity
    prioritized = sorted(
        fixes.values(), key=lambda item: (
            {"critical": 0, "major": 1, "minor": 2}.get(item["severity"], 3),
            -len(item["requirement_ids"]), item["fix"],
        )
    )
    justification = (
        f"Выявлено {len(findings)} дефектов: critical — {severity['critical']}, major — {severity['major']}, minor — {severity['minor']}. "
        + ("Есть блокирующие нарушения." if severity["critical"] else "Блокирующих нарушений нет.")
    )
    return {
        "requirement_count": len(requirements),
        "finding_count": len(findings),
        "by_severity": {key: severity[key] for key in ("critical", "major", "minor")},
        "by_criterion": dict(sorted(criteria.items())),
        "critical_defects": [item for group in groups.values() for item in group],
        "critical_groups": groups,
        "decision": decision,
        "justification": justification,
        "prioritized_fixes": [item["fix"] for item in prioritized],
        "top_fixes": prioritized[:7],
    }


def build_payload(
    metadata: dict,
    requirements: list[Requirement],
    checks: list[RequirementCheck],
    set_check: SetCheck,
) -> dict:
    full_metadata = {**metadata, "agent_version": metadata.get("agent_version") or agent_version()}
    return {
        "metadata": full_metadata,
        "requirements": [asdict(item) for item in requirements],
        "requirement_checks": [asdict(item) for item in checks],
        "set_check": asdict(set_check),
        "summary": report_summary(requirements, checks, set_check),
    }


def write_outputs(report_path: str | Path, report: str, metadata: dict, requirements: list[Requirement],
                  checks: list[RequirementCheck], set_check: SetCheck,
                  payload: dict | None = None) -> tuple[Path, Path]:
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    json_path = path.with_suffix(".json")
    payload = payload or build_payload(metadata, requirements, checks, set_check)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, json_path
