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
from .reporter import agent_version, build_payload, git_tag, render_report
from .segmenter import segment
from .set_checker import check_set
from .test_generator import generate_test_cases
from .verifier import findings_match, verify_findings


@dataclass(frozen=True)
class PipelineResult:
    report_path: Path
    json_path: Path
    requirements_count: int
    findings_count: int
    warnings: tuple[str, ...]
    payload: dict[str, Any]
    extra_paths: tuple[Path, ...] = ()


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

    checks = check_requirements(
        document, requirements, provider, batch_size=batch_size, progress_callback=batch_progress
    )
    _progress("4/5 Проверка набора и верификация цитат")
    _notify(progress_callback, 4, "Проверка набора и цитат", 75)
    set_check = check_set(document, requirements, provider, domain_check=domain_check)
    for check in checks:
        check.findings = verify_findings(document, check.findings)
    set_check.findings = verify_findings(document, set_check.findings)
    unique_entries: list[tuple[str, Any]] = []
    for origin, finding in [
        *((check.requirement_id, finding) for check in checks for finding in check.findings),
        *(("set", finding) for finding in set_check.findings),
    ]:
        if origin != "set" and origin not in finding.requirement_ids:
            finding.requirement_ids.insert(0, origin)
        duplicate = next((entry for entry in unique_entries if findings_match(entry[1], finding)), None)
        if duplicate:
            duplicate[1].requirement_ids = list(dict.fromkeys([
                *duplicate[1].requirement_ids, *finding.requirement_ids
            ]))
        else:
            unique_entries.append((origin, finding))
    for check in checks:
        check.findings = [finding for origin, finding in unique_entries if origin == check.requirement_id]
    set_check.findings = [finding for origin, finding in unique_entries if origin == "set"]
    findings_count = len(unique_entries)
    if context_paths:
        _notify(progress_callback, 4, "Проверка по контексту проекта", 82, context=True)
    context_bundle = extract_context(context_paths, provider) if context_paths else None
    context_result = check_context(context_bundle, requirements, provider, document) if context_bundle else None
    test_cases = generate_test_cases(requirements, provider) if generate_tests else None
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
    diff_result = compare_with_previous(payload, previous_report) if previous_report else None
    payload["diff"] = diff_result or {}
    report = render_report(
        document, requirements, checks, set_check, provider.name, provider.model, source, run_at,
        coverage=coverage, metadata=metadata, context_result=context_result,
        context_used=bool(context_paths), test_cases=test_cases, diff_result=diff_result,
    )
    report_path, json_path, extras = write_artifacts(destination, report, payload, also_pdf=also_pdf)
    _progress(f"кэш: попадание {provider.cache_hits}/{provider.requests}")
    _notify(progress_callback, 5, "Отчёт готов", 100)
    _progress(f"Готово: {report_path} и {json_path}")
    return PipelineResult(
        report_path, json_path, len(requirements), findings_count, tuple(warnings), payload,
        tuple(extras),
    )
