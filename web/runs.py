"""Запуски проверок: хранение в памяти, фоновое выполнение, прогресс, отмена.

Конвейер берётся из validator.pipeline.run_pipeline (код напарника). Если его ещё нет
или VALIDATOR_MOCK=1 — из web.mock_pipeline. Веб и CLI вызывают одну и ту же функцию.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import re
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

DECISION_LABELS = {"confirmed": "Подтверждено", "rejected": "Отклонено", "duplicate": "Дубль", "fixed": "Исправлено"}


def _with_reviewer_decisions(markdown: str, decisions: dict[str, dict]) -> str:
    marker = "\n## Решения ревьюера\n"
    base = markdown.split(marker, 1)[0].rstrip()
    if not decisions:
        return base + "\n"
    lines = [marker.strip(), ""]
    for fid, item in sorted(decisions.items()):
        comment = f" — {item.get('comment')}" if item.get("comment") else ""
        lines.append(f"- **{fid}**: {DECISION_LABELS.get(item.get('decision'), item.get('decision', ''))}{comment}")
    return base + "\n\n" + "\n".join(lines) + "\n"


def _with_changes(markdown: str, changes: dict) -> str:
    if not changes.get("previous_run_id"):
        return markdown
    marker = "\n## Изменения с предыдущей проверки\n"
    base = markdown.split(marker, 1)[0].rstrip()
    lines = [marker.strip(), "",
             f"- Исправлено: {', '.join(changes.get('fixed', [])) or 'нет'}",
             f"- Новые: {', '.join(changes.get('new', [])) or 'нет'}",
             f"- Остались: {', '.join(changes.get('remaining', [])) or 'нет'}"]
    return base + "\n\n" + "\n".join(lines) + "\n"


def _web_finding_key(item: dict) -> tuple:
    return (norm(item.get("quote", "")), tuple(sorted(item.get("criteria") or [item.get("criterion", "")])),
            tuple(sorted(item.get("req_ids") or ([item.get("req_id")] if item.get("req_id") else []))))


def _add_comparison(result: dict, run: "Run", runs: Any) -> None:
    current_hash = result.get("passport", {}).get("input_sha256")
    candidates = [old for old in runs if old.id != run.id and old.status == "done" and old.result and
                  (old.title == run.title or (current_hash and old.result.get("passport", {}).get("input_sha256") == current_hash))]
    if not candidates:
        result.setdefault("changes", {"fixed": [], "new": [], "remaining": []})
        return
    previous = max(candidates, key=lambda old: old.finished_at or old.created_at)
    old_findings = previous.result.get("findings", [])
    new_findings = result.get("findings", [])
    old_by_key = {_web_finding_key(item): item for item in old_findings}
    new_by_key = {_web_finding_key(item): item for item in new_findings}
    result["changes"] = {
        "fixed": [item.get("id") for key, item in old_by_key.items() if key not in new_by_key],
        "new": [item.get("id") for key, item in new_by_key.items() if key not in old_by_key],
        "remaining": [item.get("id") for key, item in new_by_key.items() if key in old_by_key],
        "previous_run_id": previous.id,
    }

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
        from validator.contract import run_pipeline  # type: ignore  # адаптер агента под контракт веба
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
    stage_started_at: float = field(default_factory=time.time)
    stage_info: dict = field(default_factory=dict)
    progress: int = 0
    stages_done: list[str] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    error: dict | None = None
    result: dict | None = field(default=None, repr=False)
    report_md: str | None = field(default=None, repr=False)
    finished_at: float | None = None
    mode: str = "demo"
    decisions: dict[str, dict] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        s = (self.result or {}).get("summary", {})
        v = (self.result or {}).get("verdict", {})
        timeout = int(os.getenv("WEB_STAGE_TIMEOUT", "900"))
        stage_slow = self.status == "running" and time.time() - self.stage_started_at > timeout
        decision_counts = {key: sum(1 for item in self.decisions.values() if item.get("decision") == key)
                           for key in ("confirmed", "rejected", "duplicate", "fixed")}
        return {
            "id": self.id, "upload_id": self.upload_id, "title": self.title,
            "file_name": self.file_name, "options": self.options,
            "gitlab": self.gitlab_project, "created_at": self.created_at, "finished_at": self.finished_at,
            "status": self.status, "stage": self.stage, "stage_info": self.stage_info, "progress": self.progress,
            "stage_started_at": self.stage_started_at, "stage_slow": stage_slow,
            "stages_done": self.stages_done, "warnings": self.warnings, "error": self.error, "mode": self.mode,
            "verdict": v.get("status"), "by_severity": s.get("by_severity"),
            "requirements_total": s.get("requirements_total"), "partial": (self.result or {}).get("partial", False),
            "decision_counts": decision_counts,
        }


class RunStore:
    TERMINAL_STATUSES = {"done", "error", "cancelled"}

    def __init__(self, data_dir: str | Path | None = None):
        self.uploads: dict[str, Upload] = {}
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._deleted: set[str] = set()
        configured_dir = data_dir if data_dir is not None else os.getenv("WEB_DATA_DIR", "./data/web")
        self.data_dir = Path(configured_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_runs()
        self._pool = ThreadPoolExecutor(max_workers=int(os.getenv("WEB_WORKERS", "2")), thread_name_prefix="run")

    def close(self) -> None:
        """Освободить рабочие потоки (в основном полезно изолированным тестам)."""
        self._pool.shutdown(wait=True)

    def _run_path(self, run_id: str) -> Path:
        return self.data_dir / f"{run_id}.json"

    @staticmethod
    def _from_snapshot(data: dict) -> Run:
        return Run(
            id=str(data["id"]),
            upload_id=str(data.get("upload_id") or ""),
            title=str(data.get("title") or ""),
            file_name=str(data.get("file_name") or "document"),
            options=dict(data.get("options") or {}),
            gitlab_project=data.get("gitlab"),
            created_at=float(data.get("created_at") or time.time()),
            status=str(data.get("status") or "error"),
            stage=str(data.get("stage") or "error"),
            stage_started_at=float(data.get("stage_started_at") or data.get("created_at") or time.time()),
            stage_info=dict(data.get("stage_info") or {}),
            progress=int(data.get("progress") or 0),
            stages_done=list(data.get("stages_done") or []),
            warnings=list(data.get("warnings") or []),
            error=data.get("error"),
            result=data.get("result"),
            report_md=data.get("report_md"),
            finished_at=data.get("finished_at"),
            mode=str(data.get("mode") or "demo"),
            decisions=dict(data.get("decisions") or {}),
        )

    def _load_runs(self) -> None:
        interrupted: list[Run] = []
        for path in sorted(self.data_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                run = self._from_snapshot(data)
                if run.id != path.stem:
                    raise ValueError("ID прогона не совпадает с именем файла")
            except Exception:  # noqa: BLE001
                log.warning("Не удалось восстановить прогон из %s", path, exc_info=True)
                continue
            if run.status in {"queued", "running"}:
                run.status = "error"
                run.stage = "error"
                run.finished_at = time.time()
                run.error = {
                    "stage": str(data.get("stage") or run.stage),
                    "code": "server_restarted",
                    "message": "прогон прерван перезапуском сервера",
                    "hint": "Загрузите исходный документ заново и повторите проверку.",
                }
                interrupted.append(run)
            self.runs[run.id] = run
        for run in interrupted:
            self.persist(run)

    def persist(self, run: Run) -> None:
        """Атомарно сохранить текущее состояние прогона в одном JSON-снимке."""
        payload = {**run.public(), "result": run.result, "report_md": run.report_md, "decisions": run.decisions}
        path = self._run_path(run.id)
        temporary = path.with_suffix(".json.tmp")
        with self._lock:
            if run.id in self._deleted:
                return
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

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
        self.persist(run)
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
                self.persist(run)
        return run

    def delete(self, run_id: str) -> None:
        with self._lock:
            run = self.runs.pop(run_id, None)
            self._deleted.add(run_id)
        if run:
            run.cancel_event.set()
        self._run_path(run_id).unlink(missing_ok=True)

    def decide(self, run_id: str, finding_id: str, decision: str, comment: str = "") -> Run:
        run = self.runs.get(run_id)
        if not run or run.status != "done" or not run.result:
            raise KeyError("run")
        valid_ids = {item.get("id") for item in [*run.result.get("findings", []), *run.result.get("unverified_findings", [])]}
        if finding_id not in valid_ids:
            raise ValueError("finding")
        run.decisions[finding_id] = {"decision": decision, "comment": comment.strip(), "updated_at": time.time()}
        run.result["reviewer_decisions"] = dict(run.decisions)
        run.report_md = _with_reviewer_decisions(run.report_md or report_md.render(run.result), run.decisions)
        self.persist(run)
        return run

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
            run.stage_started_at = time.time()
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
        self.persist(run)
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
            _add_comparison(result, run, self.runs.values())
            run.result = result
            base_report = result.get("report_markdown") or _agent_report(result) or report_md.render(result)
            run.report_md = _with_reviewer_decisions(_with_changes(base_report, result.get("changes", {})), run.decisions)
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
            hint = getattr(e, "hint", "")
            if code == "llm_429":
                delay = re.search(r"(?:retry(?:Delay)?|повтор)[^0-9]{0,20}(\d+)\s*s", msg, re.I)
                availability = f"Повторите примерно через {delay.group(1)} с. " if delay else "Повторите позже. "
                hint = availability + "Можно выбрать другую модель в расширенных настройках новой проверки."
            run.error = {"stage": run.stage, "code": str(code), "message": msg, "hint": hint,
                         "trace": traceback.format_exc(limit=3)}
            run.status = "error"
        finally:
            run.finished_at = time.time()
            if run.status in self.TERMINAL_STATUSES:
                self.persist(run)


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
    result.setdefault("unverified_findings", [])
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
    v = result.setdefault("verdict", {})
    v.setdefault("status", "not_ready" if by["critical"] else ("ready_with_notes" if by["major"] else "ready"))
    v.setdefault("ready", v["status"] != "not_ready")
    v.setdefault("text", "")
    v.setdefault("fix_first", summ["critical_ids"][:5])
    result.setdefault("warnings", [])
    result.setdefault("partial", False)
    result.setdefault("reviewer_decisions", dict(run.decisions))
    result.setdefault("traceability_matrix", [])
    result.setdefault("test_cases", [])
    return result


store = RunStore()
