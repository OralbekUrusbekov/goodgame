"""Адаптер агента под контракт веба (docs/web-contract.md).

Веб вызывает run_pipeline(doc_path, on_progress, gitlab, options) и получает dict по схеме контракта.
Внутри работает тот же validator.pipeline.run_pipeline, что и CLI (main.py), поэтому report.md совпадает.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .criteria import BY_CODE, CONTEXT_CODE, REQUIREMENT_CODE, SET_CODE
from .loader import load_document
from .pipeline import run_pipeline as _run_agent
from .verifier import exact_quote

CONTRACT_VERSION = "1.0"
DECISION = {"Нет": "not_ready", "С оговорками": "ready_with_notes", "Да": "ready"}
SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}
STEP_STAGE = {1: "reading", 2: "splitting", 3: "checking_requirements", 4: "checking_set", 5: "summary"}


def _stage_emitter(on_progress: Callable[..., Any] | None, has_gitlab: bool):
    emitted: set[str] = set()

    def emit(stage: str, info: dict | None = None) -> None:
        if on_progress:
            on_progress(stage, info or {})
        emitted.add(stage)

    def callback(event: dict) -> None:
        step, detail = event.get("step"), event
        if detail.get("context"):
            emit("checking_gitlab", {"message": event.get("label")})
            return
        stage = STEP_STAGE.get(step)
        if not stage:
            return
        if stage == "checking_requirements" and detail.get("batches"):
            emit(stage, {"done": detail.get("batch", 0), "total": detail["batches"], "message": event.get("label")})
            return
        if stage == "summary":
            if "checking_gitlab" not in emitted:
                emit("checking_gitlab", {"skipped": True} if not has_gitlab else {})
            if "verifying_quotes" not in emitted:
                emit("verifying_quotes", {})
        if stage not in emitted or stage == "summary":
            emit(stage, {"message": event.get("label")})

    return emit, callback


def _safe_name(path: str) -> str:
    return re.sub(r"[^\w.\-]+", "__", path).strip("_") + ".txt"


def _gitlab_context_dir(gitlab: Any) -> tuple[Path | None, dict[str, str]]:
    """Выгружает выбранные файлы GitLab во временную папку, чтобы агент сверил с ними ТЗ как с контекстом."""
    if gitlab is None:
        return None, {}
    files = gitlab.collect()
    if not files:
        return None, {}
    folder = Path(tempfile.mkdtemp(prefix="gitlab_ctx_"))
    names: dict[str, str] = {}
    for item in files:
        name = _safe_name(item["path"])
        names[name] = item["path"]
        (folder / name).write_text(f"# {item['path']}\n\n{item.get('content') or ''}", encoding="utf-8")
    return folder, names


def _title(text: str) -> str:
    first = re.split(r"(?<=[.!?])\s", (text or "").replace(" [цитата не подтверждена]", "").strip(), maxsplit=1)[0]
    return first if len(first) <= 110 else first[:107].rstrip() + "…"


def _question(item: dict) -> str:
    if item.get("severity") == "minor" or not item.get("fix"):
        return ""
    fix = item["fix"].rstrip(".")
    return f"Подтвердите, как должно быть: {fix[:1].lower()}{fix[1:]}?"


def _finding(item: dict, scope: str, origin: str | None, code: str) -> dict:
    criterion = BY_CODE.get(code, {})
    req_ids = list(item.get("requirement_ids") or ([] if origin in (None, "set") else [origin]))
    return {
        "req_id": req_ids[0] if req_ids else None,
        "req_ids": req_ids,
        "scope": scope,
        "criterion": code,
        "criterion_label": criterion.get("label", code),
        "source": criterion.get("source", ""),
        "verdict": "fail",
        "severity": item.get("severity", "major"),
        "title": _title(item.get("explanation", "")),
        "quote": item.get("quote", ""),
        "quotes": list(item.get("related_quotes") or []),
        "location": item.get("location", ""),
        "defect": item.get("explanation", ""),
        "fix": item.get("fix", ""),
        "question": _question(item),
        "quote_verified": bool(item.get("quote_verified", True)),
        "confidence": item.get("confidence"),
        "code_ref": None,
    }


def _text_location(text: str, quote: str) -> str:
    """Locate an exact quote in plain/Markdown text while tracking headings."""
    section = "Начало документа"
    for number, line in enumerate(text.splitlines(), 1):
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line.strip())
        if heading:
            section = heading.group(1).strip()
        if quote == line.strip() or quote in line:
            return f"{section}, строка {number}"
    return "Раздел не определён"


def _anchor_finding(finding: dict, document_text: str) -> None:
    literal = exact_quote(finding.get("quote", ""), document_text)
    if literal:
        finding["quote"] = literal
        finding["location"] = _text_location(document_text, literal)
    anchored_related = []
    related_verified = True
    for quote in finding.get("quotes", []):
        anchored = exact_quote(quote, document_text)
        related_verified = related_verified and anchored is not None
        anchored_related.append(anchored or quote)
    finding["quotes"] = anchored_related
    finding["quote_verified"] = literal is not None and related_verified


def to_contract(payload: dict, report_md: str, document_text: str, file_name: str, options: dict,
                gitlab: Any = None, gitlab_names: dict[str, str] | None = None, doc_format: str = "") -> dict:
    meta, summary = payload.get("metadata", {}), payload.get("summary", {})
    gitlab_names = gitlab_names or {}
    findings: list[dict] = []
    for check in payload.get("requirement_checks", []):
        for item in check.get("findings", []):
            code = REQUIREMENT_CODE.get(item["criterion"]) or SET_CODE.get(item["criterion"]) or item["criterion"]
            findings.append(_finding(item, "requirement", check.get("requirement_id"), code))
    for item in payload.get("set_check", {}).get("findings", []):
        code = SET_CODE.get(item["criterion"]) or REQUIREMENT_CODE.get(item["criterion"]) or item["criterion"]
        findings.append(_finding(item, "set", None, code))

    code_checks: list[dict] = []
    for item in payload.get("context", {}).get("findings", []):
        code = CONTEXT_CODE.get(item.get("type", ""), f"context.{item.get('type', 'other')}")
        repo_path = gitlab_names.get(item.get("context_file", ""))
        finding = _finding({
            **item, "quote": item.get("requirement_quote", ""), "related_quotes": [],
            "quote_verified": item.get("requirement_quote_verified", False),
            "location": item.get("context_file", ""),
        }, "code" if repo_path else "set", None, code)
        finding["context_quote"] = item.get("context_quote", "")
        finding["context_file"] = repo_path or item.get("context_file", "")
        if repo_path and gitlab is not None:
            ref = {"path": repo_path, "line": item.get("context_line"),
                   "url": gitlab.web_url(repo_path, item.get("context_line"))}
            finding["code_ref"] = ref
            code_checks.append({"item": item.get("requirement_quote", ""), "kind": item.get("type", ""),
                                "status": "mismatch", "doc_quote": item.get("requirement_quote", ""),
                                "note": item.get("explanation", ""), "code_ref": ref})
        findings.append(finding)

    for finding in findings:
        _anchor_finding(finding, document_text)

    findings.sort(key=lambda f: (SEV_ORDER.get(f["severity"], 3), f["req_id"] or "~"))
    for index, finding in enumerate(findings, 1):
        finding["id"] = f"F-{index:02d}"

    by_severity = {s: sum(1 for f in findings if f["severity"] == s) for s in SEV_ORDER}
    critical_ids = [f["id"] for f in findings if f["severity"] == "critical"]
    status = DECISION.get(summary.get("decision", ""),
                          "not_ready" if by_severity["critical"] else "ready_with_notes" if by_severity["major"] else "ready")
    verdict_text = {
        "not_ready": "Начинать разработку нельзя: есть критические дефекты.",
        "ready_with_notes": "Можно начинать после исправления существенных замечаний.",
        "ready": "Документ готов к разработке.",
    }[status] + (f" Замечаний: {len(findings)} — критичных {by_severity['critical']}, "
                 f"существенных {by_severity['major']}, мелких {by_severity['minor']}.")

    questions, by_question = [], {}
    for f in findings:
        key = " ".join(f["question"].casefold().split())
        if not f["question"]:
            continue
        if key in by_question:
            question = by_question[key]
            question["finding_ids"] = list(dict.fromkeys([*question["finding_ids"], f["id"]]))
            question["req_ids"] = list(dict.fromkeys([*question["req_ids"], *f["req_ids"]]))
            continue
        question = {"id": f"Q-{len(questions) + 1:02d}", "text": f["question"],
                    "finding_ids": [f["id"]], "req_ids": f["req_ids"]}
        by_question[key] = question
        questions.append(question)

    warnings = [{"stage": "splitting", "code": "segmentation", "message": w} for w in meta.get("segmentation_warnings", [])]
    unverified = sum(1 for f in findings if not f["quote_verified"])
    if unverified:
        warnings.append({"stage": "verifying_quotes", "code": "quote_unverified",
                         "message": f"Цитата не подтверждена дословно у {unverified} замечаний — они помечены."})

    return {
        "contract_version": CONTRACT_VERSION,
        "passport": {
            "document_title": options.get("title") or meta.get("title") or file_name,
            "file_name": file_name,
            "date": meta.get("run_at"),
            "model": f"{meta.get('provider', '')}/{meta.get('model', '')}".strip("/"),
            "agent_version": meta.get("agent_version"),
            "criteria": ["iso29148", "ieee830"] + (["context"] if meta.get("context_used") else []),
            "gitlab": {"project": gitlab.project, "ref": gitlab.ref} if gitlab is not None else None,
            "dropped_findings": 0,
            "coverage_percent": meta.get("coverage_percent"),
            "llm_calls": meta.get("llm_calls"),
            "cache_hits": meta.get("cache_hits"),
            "duration_seconds": meta.get("duration_seconds"),
            "input_sha256": meta.get("input_sha256"),
        },
        "document": {"text": document_text, "format": doc_format},
        "requirements": [
            {"id": r["report_id"], "doc_id": r.get("source_id") if r.get("source_id") not in (None, "—") else None,
             "section": r.get("section", ""), "summary": r.get("short_text", ""), "quote": r.get("quote", ""),
             "quote_verified": r.get("quote_verified", True), "line": r.get("line_number")}
            for r in payload.get("requirements", [])
        ],
        "findings": findings,
        "code_checks": code_checks,
        "questions": questions,
        "summary": {
            "requirements_total": len(payload.get("requirements", [])),
            "findings_total": len(findings),
            "by_severity": by_severity,
            "critical_ids": critical_ids,
            "top_fixes": summary.get("top_fixes", []),
        },
        "verdict": {"status": status, "ready": status != "not_ready", "text": verdict_text.strip(),
                    "fix_first": critical_ids[:5] or [f["id"] for f in findings[:3]]},
        "traceability_matrix": payload.get("context", {}).get("traceability_matrix", []),
        "test_cases": payload.get("test_cases", []),
        "warnings": warnings,
        "partial": False,
        "report_markdown": report_md,
    }


def run_pipeline(doc_path: str, on_progress: Callable[..., Any] | None = None, gitlab: Any = None,
                 options: dict | None = None) -> dict:
    options = dict(options or {})
    emit, callback = _stage_emitter(on_progress, gitlab is not None)
    emit("reading", {"message": "Чтение документа"})
    context_dir, gitlab_names = _gitlab_context_dir(gitlab)
    out_dir = Path(tempfile.mkdtemp(prefix="validator_run_"))
    try:
        result = _run_agent(
            doc_path, str(out_dir / "report.md"), load_config(), progress_callback=callback,
            context_paths=[str(context_dir)] if context_dir else None,
            generate_tests=bool(options.get("generate_tests")),
        )
        report_md = result.report_path.read_text(encoding="utf-8")
        document = load_document(doc_path)
        return to_contract(result.payload, report_md, document.text, Path(doc_path).name, options,
                           gitlab, gitlab_names, Path(doc_path).suffix.lstrip(".").lower())
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        if context_dir:
            shutil.rmtree(context_dir, ignore_errors=True)
