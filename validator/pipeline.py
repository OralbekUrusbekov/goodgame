from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Callable
import hashlib
import sys
import time
from typing import Any

from .checker import check_requirements
from .config import Settings
from .context import check_context, extract_context
from .diff import compare_with_previous
from .formats import write_artifacts
from .llm import create_provider
from .loader import load_document
from .models import SetCheck
from .reporter import agent_version, build_payload, git_tag, render_report
from .segmenter import segment
from .set_checker import check_set
from .test_generator import generate_test_cases
from .verifier import cluster_findings, verify_findings


@dataclass(frozen=True)
class PipelineResult:
    report_path: Path
    json_path: Path
    requirements_count: int
    findings_count: int
    warnings: tuple[str, ...]
    payload: dict[str, Any]
    extra_paths: tuple[Path, ...] = ()


class PartialResultError(Exception):
    """Expose a stable partial report when a post-segmentation stage fails.

    ``payload`` follows :func:`build_payload`, ``report_markdown`` is ready for
    display/download, ``stage`` identifies the failed stage, and ``cause`` is
    the original exception. Prepared requirements/checks are preserved; a set
    check that has not run yet is represented by an empty ``SetCheck``.
    """

    def __init__(self, stage: str, cause: Exception, payload: dict[str, Any], report_markdown: str):
        self.payload = payload
        self.report_markdown = report_markdown
        self.stage = stage
        self.cause = cause
        super().__init__(f"Частичный результат: этап {stage} не выполнен: {cause}")


ProgressCallback = Callable[[dict[str, Any]], None]


def _progress(message: str) -> None:
    print(f"[validator] {message}", file=sys.stderr, flush=True)


def _notify(callback: ProgressCallback | None, step: int, label: str, percent: int, **detail: Any) -> None:
    if callback:
        callback({"step": step, "label": label, "progress": percent, **detail})


def run_pipeline(
    source: str,
    destination: str,
    settings: Settings,
    verbose: bool = False,
    progress_callback: ProgressCallback | None = None,
    context_paths: list[str] | None = None,
    domain_check: bool = True,
    generate_tests: bool = False,
    previous_report: str | None = None,
    also_pdf: bool = False,
) -> PipelineResult:
    started_monotonic = time.monotonic()
    _progress("1/5 Загрузка документа")
    _notify(progress_callback, 1, "Загрузка", 5)
    document = load_document(source)
    provider = create_provider(settings, verbose=verbose)
    _progress(f"2/5 Сегментация требований ({provider.name}/{provider.model})")
    _notify(progress_callback, 2, "Сегментация", 15)
    requirements, warnings, coverage = segment(document, provider, include_coverage=True)
    checks = []
    set_check = SetCheck({}, [])

    def partial(stage: str, cause: Exception) -> PartialResultError:
        checked_ids = {check.requirement_id for check in checks}
        partial_metadata = {
            "title": document.title, "source_path": source,
            "provider": provider.name, "model": provider.model,
            "segmentation_warnings": warnings,
            "coverage_percent": coverage["percent"],
            "coverage_units": {"covered": coverage["covered_units"], "total": coverage["total_units"]},
            "coverage_skipped_units": coverage["skipped_units"],
            "partial": True, "failed_stage": stage,
            "completed_stages": {
                "segmentation": True,
                "requirements": len(checked_ids) == len(requirements),
                "set_check": bool(set_check.verdicts),
                "verification": False,
            },
            "requested_model": getattr(provider, "requested_model", f"{provider.name}:{provider.model}"),
            "models_by_step": provider.models_by_step,
            "model_trace": getattr(provider, "model_trace", []),
        }
        partial_payload = build_payload(partial_metadata, requirements, checks, set_check)
        markdown = (
            f"# ЧАСТИЧНЫЙ ОТЧЁТ: этап {stage} не выполнен\n\n"
            f"- Документ: {document.title}\n"
            f"- Выделено требований: {len(requirements)}\n"
            "- Готово проверок требований: — (этап не завершён)\n"
            "- Итог: **Проверка не завершена**. Результат не является итоговым вердиктом.\n"
        )
        return PartialResultError(stage, cause, partial_payload, markdown)
    if verbose:
        _progress(f"Выделено требований: {len(requirements)}")
        for warning in warnings:
            _progress(f"Предупреждение: {warning}")
    batch_size = settings.check_batch_size
    batches = max(1, (len(requirements) + batch_size - 1) // batch_size)
    _progress(f"3/5 Проверка отдельных требований пакетами по {batch_size}")
    _notify(progress_callback, 3, "Проверка требований", 35, batch=0, batches=batches)

    def batch_progress(batch: int, batches: int) -> None:
        percent = 35 + round(35 * batch / batches)
        _notify(progress_callback, 3, f"Проверка требований (пакет {batch}/{batches})", percent,
                batch=batch, batches=batches)

    try:
        checks = check_requirements(
            document, requirements, provider, batch_size=batch_size,
            concurrency=settings.llm_concurrency, progress_callback=batch_progress,
        )
    except Exception as error:
        raise partial("проверка требований", error) from error
    _progress("4/5 Проверка набора и верификация цитат")
    _notify(progress_callback, 4, "Проверка набора и цитат", 75)
    try:
        set_check = check_set(document, requirements, provider, domain_check=domain_check)
        for check in checks:
            for finding in check.findings:
                if check.requirement_id not in finding.requirement_ids:
                    finding.requirement_ids.insert(0, check.requirement_id)
            check.findings = verify_findings(document, check.findings)
            for criterion, verdict in list(check.verdicts.items()):
                relevant = [item for item in check.findings if criterion in [item.criterion, *item.criteria]]
                if verdict == "violation" and relevant and not any(item.quote_verified for item in relevant):
                    check.verdicts[criterion] = "ok"
        set_check.findings = verify_findings(document, set_check.findings)
        for criterion, verdict in list(set_check.verdicts.items()):
            relevant = [item for item in set_check.findings if criterion in [item.criterion, *item.criteria]]
            if verdict == "violation" and relevant and not any(item.quote_verified for item in relevant):
                set_check.verdicts[criterion] = "ok"

        clustered = cluster_findings([
            *[finding for check in checks for finding in check.findings],
            *set_check.findings,
        ])
        by_id = {check.requirement_id: check for check in checks}
        for check in checks:
            check.findings = []
        set_check.findings = []
        for finding in clustered:
            owner = next((item for item in finding.requirement_ids if item in by_id), None)
            (by_id[owner].findings if owner else set_check.findings).append(finding)
        findings_count = sum(item.quote_verified and not item.unverified for item in clustered)
    except Exception as error:
        raise partial("проверка набора и верификация", error) from error
    if context_paths:
        _notify(progress_callback, 4, "Проверка по контексту проекта", 82, context=True)
    try:
        context_bundle = extract_context(context_paths, provider) if context_paths else None
        context_result = check_context(context_bundle, requirements, provider, document) if context_bundle else None
        test_cases = generate_test_cases(requirements, provider) if generate_tests else None
    except Exception as error:
        raise partial("контекст и тест-кейсы", error) from error
    _progress("5/5 Запись Markdown и JSON")
    _notify(progress_callback, 5, "Отчёт", 92)
    run_at = datetime.now().astimezone()
    input_sha256 = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    duration = round(time.monotonic() - started_monotonic, 3)
    metadata = {
        "title": document.title, "source_path": source, "run_at": run_at.isoformat(),
        "provider": provider.name, "model": provider.model, "segmentation_warnings": warnings,
        "coverage_percent": coverage["percent"],
        "coverage_units": {"covered": coverage["covered_units"], "total": coverage["total_units"]},
        "coverage_skipped_units": coverage["skipped_units"],
        "llm_calls": provider.calls,
        "cache_hits": provider.cache_hits,
        "llm_requests": provider.requests,
        "models_by_step": provider.models_by_step,
        "requested_model": provider.requested_model,
        "model_trace": provider.model_trace,
        "completed_stages": {"segmentation": True, "requirements": True, "set_check": True, "verification": True},
        "duration_seconds": duration,
        "input_sha256": input_sha256,
        "agent_version": agent_version(),
        "git_tag": git_tag(),
        "context_used": bool(context_paths),
    }
    payload = build_payload(metadata, requirements, checks, set_check)
    payload["context"] = {
        "used": bool(context_paths),
        "extracted": context_bundle.extracted if context_bundle else {},
        **(context_result or {"findings": [], "traceability_matrix": set_check.traceability_matrix}),
    }
    payload["test_cases"] = test_cases or []
    try:
        diff_result = compare_with_previous(payload, previous_report) if previous_report else None
        payload["diff"] = diff_result or {}
        report = render_report(
            document, requirements, checks, set_check, provider.name, provider.model, source, run_at,
            coverage=coverage, metadata=metadata, context_result=context_result,
            context_used=bool(context_paths), test_cases=test_cases, diff_result=diff_result,
        )
    except Exception as error:
        raise partial("формирование отчёта", error) from error
    try:
        report_path, json_path, extras = write_artifacts(destination, report, payload, also_pdf=also_pdf)
    except Exception as error:
        raise PartialResultError("запись отчёта", error, payload, report) from error
    _progress(f"кэш: попадание {provider.cache_hits}/{provider.requests}")
    _notify(progress_callback, 5, "Отчёт готов", 100)
    _progress(f"Готово: {report_path} и {json_path}")
    return PipelineResult(
        report_path, json_path, len(requirements), findings_count, tuple(warnings), payload,
        tuple(extras),
    )
