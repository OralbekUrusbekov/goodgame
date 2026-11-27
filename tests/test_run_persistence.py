from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web.app import app
from web.runs import Run, RunStore


def _terminal_run(run_id: str = "saved-run", status: str = "done") -> Run:
    run = Run(
        id=run_id,
        upload_id="missing-upload",
        title="Сохранённый прогон",
        file_name="requirements.md",
        options={"work_type": "Новый функционал", "criteria": []},
        gitlab_project=None,
        status=status,
        stage=status,
        progress=100 if status == "done" else 12,
        finished_at=time.time() if status in {"done", "error", "cancelled"} else None,
        mode="agent",
    )
    if status == "done":
        run.result = {"summary": {"requirements_total": 1, "by_severity": {}}, "verdict": {"status": "ready"}}
        run.report_md = "# Сохранённый отчёт\n"
    return run


def _write_snapshot(folder: Path, run: Run) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run.id}.json"
    path.write_text(
        json.dumps({**run.public(), "result": run.result, "report_md": run.report_md}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_terminal_run_is_saved_and_reloaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "web"
    first = RunStore(data_dir=folder)
    upload = first.save_upload(
        "requirements.md", b"# Requirements\n\n* The system stores a setting.\n", {"title": "Requirements"}
    )

    def fake_pipeline(path: str, **kwargs) -> dict:
        return {
            "summary": {"requirements_total": 1, "by_severity": {}},
            "verdict": {"status": "ready"},
            "report_markdown": "# Сохранённый отчёт\n",
        }

    monkeypatch.setattr("web.runs.load_pipeline", lambda: (fake_pipeline, "agent"))
    run = first.create(upload.id, "Сохранённый прогон", {}, None, None)
    deadline = time.monotonic() + 3
    while run.status not in RunStore.TERMINAL_STATUSES and time.monotonic() < deadline:
        time.sleep(0.01)
    assert run.status == "done", run.error

    stored = folder / f"{run.id}.json"
    assert stored.exists()
    assert not list(folder.glob("*.tmp"))
    second = RunStore(data_dir=folder)
    restored = second.get(run.id)
    assert restored is not None
    assert restored.public() == run.public()
    assert restored.result == run.result
    assert restored.report_md == run.report_md
    first.close()
    second.close()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_interrupted_run_becomes_error_on_reload(tmp_path: Path, status: str) -> None:
    folder = tmp_path / "web"
    run = _terminal_run(run_id=f"interrupted-{status}", status=status)
    _write_snapshot(folder, run)

    restored_store = RunStore(data_dir=folder)
    restored = restored_store.get(run.id)

    assert restored is not None
    assert restored.status == "error"
    assert restored.stage == "error"
    assert restored.finished_at is not None
    assert restored.error is not None
    assert restored.error["message"] == "прогон прерван перезапуском сервера"
    saved = json.loads((folder / f"{run.id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "error"
    restored_store.close()


def test_delete_removes_persisted_run(tmp_path: Path) -> None:
    folder = tmp_path / "web"
    store = RunStore(data_dir=folder)
    run = _terminal_run()
    store.runs[run.id] = run
    store.persist(run)

    store.delete(run.id)

    assert store.get(run.id) is None
    assert not (folder / f"{run.id}.json").exists()
    store.close()


def test_partial_run_remains_distinct_after_reload(tmp_path: Path) -> None:
    folder = tmp_path / "web"
    first = RunStore(data_dir=folder)
    run = _terminal_run(status="partial")
    run.finished_at = time.time()
    run.result = {"partial": True, "summary": {"requirements_total": 1, "by_severity": {}},
                  "verdict": {"status": "incomplete", "ready": False}}
    run.report_md = "# Частичный отчёт\n\nПроверка не завершена.\n"
    first.runs[run.id] = run
    first.persist(run)
    second = RunStore(data_dir=folder)
    restored = second.get(run.id)
    assert restored is not None
    assert restored.status == "partial"
    assert restored.public()["verdict"] == "incomplete"
    assert restored.public()["partial"] is True
    first.close()
    second.close()


def test_retry_of_restored_run_without_upload_returns_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "web"
    _write_snapshot(folder, _terminal_run())
    restored_store = RunStore(data_dir=folder)
    monkeypatch.setattr("web.app.runs", restored_store)

    response = TestClient(app).post("/api/runs/saved-run/retry")

    assert response.status_code == 409
    assert "исходный файл" in response.json()["detail"].lower()
    assert "загруз" in response.json()["detail"].lower()
    restored_store.close()


def test_reviewer_decision_is_persisted_and_added_to_report(tmp_path: Path) -> None:
    folder = tmp_path / "web"
    first = RunStore(data_dir=folder)
    run = _terminal_run()
    run.result["findings"] = [{"id": "F-01", "quote": "Точная цитата"}]
    first.runs[run.id] = run

    first.decide(run.id, "F-01", "fixed", "Исправлено в версии 2")
    second = RunStore(data_dir=folder)
    restored = second.get(run.id)

    assert restored is not None
    assert restored.decisions["F-01"]["decision"] == "fixed"
    assert "## Решения ревьюера" in restored.report_md
    assert "Исправлено в версии 2" in restored.report_md
    first.close()
    second.close()


def test_legacy_partial_snapshot_is_migrated_to_incomplete(tmp_path: Path) -> None:
    run = _terminal_run("legacy-partial")
    run.result = {
        "partial": True,
        "summary": {"requirements_total": 2, "by_severity": {}},
        "verdict": {"status": "ready", "ready": True, "text": "Документ готов к разработке."},
        "requirements": [{"id": "R-01"}, {"id": "R-02", "status": "pass"}],
    }
    run.report_md = "# Отчёт\n\nДокумент готов к разработке.\n"
    _write_snapshot(tmp_path, run)

    store = RunStore(tmp_path)
    try:
        loaded = store.get("legacy-partial")
        assert loaded is not None
        assert loaded.status == "partial"
        assert loaded.result["verdict"]["ready"] is False
        assert loaded.result["verdict"]["status"] == "incomplete"
        assert {r["status"] for r in loaded.result["requirements"]} == {"not_checked"}
        assert "Документ готов к разработке" not in loaded.report_md
        saved = json.loads((tmp_path / "legacy-partial.json").read_text(encoding="utf-8"))
        assert saved["status"] == "partial"
    finally:
        store.close()
