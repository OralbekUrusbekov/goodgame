"""Запуски проверок: хранение в памяти, фоновое выполнение, прогресс, отмена.

Конвейер берётся из validator.pipeline.run_pipeline (код напарника). Если его ещё нет
или VALIDATOR_MOCK=1 — из web.mock_pipeline. Веб и CLI вызывают одну и ту же функцию.
"""
from __future__ import annotations

import inspect
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import report_md
from .textutil import norm

log = logging.getLogger("web.runs")

UPLOAD_DIR = Path(os.getenv("WEB_UPLOAD_DIR") or Path(tempfile.gettempdir()) / "bcc_spec_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

STAGES = ["reading", "splitting", "checking_requirements", "checking_set", "checking_gitlab",
          "verifying_quotes", "summary"]
STAGE_PCT = {"reading": (0, 8), "splitting": (8, 15), "checking_requirements": (15, 65), "checking_set": (65, 78),
             "checking_gitlab": (78, 88), "verifying_quotes": (88, 95), "summary": (95, 99)}


class RunCancelled(Exception):
    pass


def load_pipeline() -> tuple[Callable[..., dict], str]:
    if os.getenv("VALIDATOR_MOCK") == "1":
        from .mock_pipeline import run_pipeline
        return run_pipeline, "demo"
    try:
        from validator.pipeline import run_pipeline  # type: ignore
        return run_pipeline, "agent"
    except ImportError:
        from .mock_pipeline import run_pipeline
        return run_pipeline, "demo"
    except Exception:  # конвейер есть, но падает при импорте — показываем это, а не тихо подменяем
        log.exception("validator.pipeline не импортируется")
        raise


def pipeline_mode() -> str:
    try:
        return load_pipeline()[1]
    except Exception:  # noqa: BLE001
        return "broken"


@dataclass
class Upload:
    id: str
    path: Path
    file_name: str
    size: int
    preview: dict
    created_at: float = field(default_factory=time.time)


@dataclass
class Run:
    id: str
    upload_id: str
    title: str
    file_name: str
    options: dict
    gitlab_project: dict | None  # {"id", "path", "ref"}
    created_at: float = field(default_factory=time.time)
    status: str = "queued"  # queued | running | done | error | cancelled
    stage: str = "queued"
    stage_info: dict = field(default_factory=dict)
    progress: int = 0
    stages_done: list[str] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    error: dict | None = None
    result: dict | None = field(default=None, repr=False)
    report_md: str | None = field(default=None, repr=False)
    finished_at: float | None = None
    mode: str = "demo"
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        s = (self.result or {}).get("summary", {})
        v = (self.result or {}).get("verdict", {})
        return {
            "id": self.id, "title": self.title, "file_name": self.file_name, "options": self.options,
            "gitlab": self.gitlab_project, "created_at": self.created_at, "finished_at": self.finished_at,
            "status": self.status, "stage": self.stage, "stage_info": self.stage_info, "progress": self.progress,
            "stages_done": self.stages_done, "warnings": self.warnings, "error": self.error, "mode": self.mode,
            "verdict": v.get("status"), "by_severity": s.get("by_severity"),
            "requirements_total": s.get("requirements_total"), "partial": (self.result or {}).get("partial", False),
        }


class RunStore:
    def __init__(self):
        self.uploads: dict[str, Upload] = {}
        self.runs: dict[str, Run] = {}
        self._pool = ThreadPoolExecutor(max_workers=int(os.getenv("WEB_WORKERS", "2")), thread_name_prefix="run")
        self._lock = threading.Lock()

    # --- загрузки
    def save_upload(self, file_name: str, data: bytes, preview: dict) -> Upload:
        uid = uuid.uuid4().hex[:12]
        folder = UPLOAD_DIR / uid
        folder.mkdir(parents=True, exist_ok=True)
        safe = Path(file_name).name or "document"
        path = folder / safe
        path.write_bytes(data)
        up = Upload(id=uid, path=path, file_name=safe, size=len(data), preview=preview)
        with self._lock:
            self.uploads[uid] = up
        return up

    def temp_path(self, file_name: str, data: bytes) -> Path:
        folder = Path(tempfile.mkdtemp(dir=UPLOAD_DIR))
        path = folder / (Path(file_name).name or "document")
        path.write_bytes(data)
        return path

    def drop_temp(self, path: Path) -> None:
        shutil.rmtree(path.parent, ignore_errors=True)

    # --- запуски
    def create(self, upload_id: str, title: str, options: dict, gitlab_project: dict | None,
               gitlab_ctx_factory: Callable[[], Any] | None) -> Run:
        up = self.uploads.get(upload_id)
        if not up:
            raise KeyError("upload")
        run = Run(id=uuid.uuid4().hex[:10], upload_id=upload_id, title=title or up.preview.get("title") or up.file_name,
                  file_name=up.file_name, options=options, gitlab_project=gitlab_project)
        with self._lock:
            self.runs[run.id] = run
        self._pool.submit(self._execute, run, up.path, gitlab_ctx_factory)
        return run

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def list(self) -> list[Run]:
        return sorted(self.runs.values(), key=lambda r: r.created_at, reverse=True)

    def cancel(self, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        if run and run.status in ("queued", "running"):
            run.cancel_event.set()
            if run.status == "queued":
                run.status, run.stage, run.finished_at = "cancelled", "cancelled", time.time()
        return run

    def delete(self, run_id: str) -> None:
        run = self.runs.pop(run_id, None)
        if run:
            run.cancel_event.set()

    # --- выполнение
    def _on_progress(self, run: Run, stage: str, info: dict | None = None) -> None:
        if run.cancel_event.is_set():
            raise RunCancelled()
        info = dict(info or {})
        if stage == "warning":
            run.warnings.append({"stage": run.stage, **info})
            return
        if stage not in STAGE_PCT:
            run.stage_info = {**run.stage_info, **info}
            return
        if stage != run.stage:
            for s in STAGES[: STAGES.index(stage)]:
                if s not in run.stages_done:
                    run.stages_done.append(s)
            run.stage_info = {}
        run.stage = stage
        run.stage_info = {**run.stage_info, **info}
        lo, hi = STAGE_PCT[stage]
        pct = lo
        if info.get("total"):
            pct = lo + (hi - lo) * min(1.0, info.get("done", 0) / max(1, info["total"]))
        run.progress = max(run.progress, int(pct))

    def _execute(self, run: Run, path: Path, ctx_factory) -> None:
        if run.cancel_event.is_set():
            return
        run.status, run.stage = "running", "reading"
        try:
            fn, run.mode = load_pipeline()
            gitlab_ctx = None
            if ctx_factory is not None:
                try:
                    gitlab_ctx = ctx_factory()
                except Exception as e:  # noqa: BLE001
                    run.warnings.append({"stage": "checking_gitlab", "code": "gitlab_error",
                                         "message": getattr(e, "message", str(e))})
            kwargs = {"on_progress": lambda stage, info=None: self._on_progress(run, stage, info),
                      "gitlab": gitlab_ctx, "options": {**run.options, "title": run.title}}
            params = inspect.signature(fn).parameters
            accepts_kw = any(p.kind == p.VAR_KEYWORD for p in params.values())
            kwargs = {k: v for k, v in kwargs.items() if accepts_kw or k in params}
            result = fn(str(path), **kwargs)
            if run.cancel_event.is_set():
                raise RunCancelled()
            result = normalize_result(result, path, run)
            run.result = result
            agent_md = result.get("report_markdown") or _agent_report(result)
            run.report_md = agent_md or report_md.render(result)
            if agent_md and result["partial"]:
                # отчёт агента не должен выглядеть законченным, если проверка не завершена
                run.report_md = report_md.incomplete_banner(result) + run.report_md
            run.warnings.extend(w for w in result.get("warnings", []) if w not in run.warnings)
            run.stages_done = list(STAGES)
            run.status, run.stage, run.progress = "done", "done", 100
        except RunCancelled:
            run.status, run.stage = "cancelled", "cancelled"
        except Exception as e:  # noqa: BLE001
            log.exception("Прогон %s упал", run.id)
            code = getattr(e, "code", None) or e.__class__.__name__
            msg = getattr(e, "message", None) or str(e) or e.__class__.__name__
            status = getattr(e, "status_code", None) or getattr(e, "code", None)
            if "429" in str(status) or "429" in msg or "quota" in msg.lower() or "rate" in msg.lower():
                code = "llm_429"
            run.error = {"stage": run.stage, "code": str(code), "message": msg, "hint": getattr(e, "hint", ""),
                         "trace": traceback.format_exc(limit=3)}
            run.status = "error"
        finally:
            run.finished_at = time.time()


def _agent_report(result: dict) -> str | None:
    try:
        from validator.report import render_markdown  # type: ignore
        return render_markdown(result)
    except Exception:  # noqa: BLE001
        return None


def normalize_result(result: dict, path: Path, run: Run) -> dict:
    """Достраивает необязательные поля контракта, чтобы фронтенд не падал на неполном ответе агента."""
    result = dict(result or {})
    result.setdefault("contract_version", "1.0")
    p = result.setdefault("passport", {})
    p.setdefault("document_title", run.title)
    p.setdefault("file_name", run.file_name)
    doc = result.setdefault("document", {})
    if not doc.get("text"):
        try:
            from .reader import read_document
            doc["text"] = read_document(path).text
        except Exception:  # noqa: BLE001
            doc["text"] = ""
    reqs = result.setdefault("requirements", [])
    finds = result.setdefault("findings", [])
    finds[:] = [f for f in finds if f.get("verdict", "fail") == "fail"]
    for i, f in enumerate(finds, 1):
        f.setdefault("id", f"F-{i:02d}")
        f.setdefault("severity", "major")
        f.setdefault("scope", "requirement" if f.get("req_id") else "set")
        f.setdefault("criterion_label", f.get("criterion", ""))
        f.setdefault("title", (f.get("defect") or "")[:90])
        f.setdefault("quotes", [])
    result.setdefault("code_checks", [])
    if "questions" not in result:
        seen, qs = set(), []
        for f in finds:
            q = (f.get("question") or "").strip()
            if q and norm(q) not in seen:
                seen.add(norm(q))
                qs.append({"id": f"Q-{len(qs) + 1:02d}", "text": q, "finding_ids": [f["id"]],
                           "req_ids": [f["req_id"]] if f.get("req_id") else []})
        result["questions"] = qs
    by = {s: sum(1 for f in finds if f["severity"] == s) for s in ("critical", "major", "minor")}
    summ = result.setdefault("summary", {})
    summ.setdefault("requirements_total", len(reqs))
    summ.setdefault("findings_total", len(finds))
    summ.setdefault("by_severity", by)
    summ.setdefault("critical_ids", [f["id"] for f in finds if f["severity"] == "critical"])
    result.setdefault("warnings", [])
    result.setdefault("partial", False)

    # --- полнота проверки: «готово» допустимо только после всех обязательных этапов
    failed = [s for s, st in (result.get("stages") or {}).items() if s in MANDATORY_STAGES and st != "done"]
    reasons = []
    if result["partial"]:
        reasons.append("прогон помечен как неполный")
    if failed:
        result["partial"] = True  # упавший этап = неполный прогон, даже если агент не поставил флаг
    checked_total, missing = mark_checked(result)
    if failed:
        reasons.append("не завершены этапы: " + ", ".join(STAGE_RU.get(s, s) for s in failed))
    if not reqs:
        reasons.append("в документе не выделено ни одного требования")
    if missing:
        reasons.append(f"проверено {checked_total} из {len(reqs)} требований")
    summ["requirements_checked"] = checked_total
    complete = not reasons

    v = result.setdefault("verdict", {})
    v.setdefault("status", "not_ready" if by["critical"] else ("ready_with_notes" if by["major"] else "ready"))
    v.setdefault("text", "")
    v.setdefault("fix_first", summ["critical_ids"][:5])
    if not complete:
        result["partial"] = True
        v["incomplete_reasons"] = reasons
        if v["status"] != "not_ready":  # критичные дефекты остаются правдой и при неполной проверке
            if v["status"] in ("ready", "ready_with_notes"):
                v["overridden_from"] = v["status"]
            v["status"] = "incomplete"
            v["text"] = ("Проверка не завершена: " + "; ".join(reasons) +
                         ". Вывод о готовности к разработке делать нельзя — запустите проверку заново.")
    v["ready"] = complete and v["status"] in ("ready", "ready_with_notes")
    return result


MANDATORY_STAGES = ("reading", "splitting", "checking_requirements", "checking_set", "verifying_quotes")
STAGE_RU = {"reading": "чтение", "splitting": "нарезка", "checking_requirements": "проверка требований",
            "checking_set": "проверка набора", "verifying_quotes": "проверка цитат"}


def mark_checked(result: dict) -> tuple[int, int]:
    """Проставляет каждому требованию check_status: checked | not_checked. Возвращает (проверено, не проверено).

    Откуда берётся: поле requirements[].checked или список result["checked_ids"]. Если агент ничего не сообщил,
    полный прогон считается проверившим всё, а неполный — только требования, по которым есть находки.
    """
    reqs = result["requirements"]
    ids = result.get("checked_ids")
    with_findings = {f.get("req_id") for f in result["findings"]}
    for r in reqs:
        if "checked" in r:
            ok = bool(r["checked"])
        elif ids is not None:
            ok = r.get("id") in ids
        else:
            ok = not result["partial"] or r.get("id") in with_findings
        r["check_status"] = "checked" if ok else "not_checked"
    done = sum(1 for r in reqs if r["check_status"] == "checked")
    return done, len(reqs) - done


store = RunStore()
