from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from tests.pdf_helpers import UnicodePdfWriter
from validator.criteria import CRITERIA, GROUPS
from web import app as web_app_module
from web.app import app
from web.runs import store


ROOT = Path(__file__).parents[1]
CLIENT = TestClient(app)
TEXT = "# Сервис настроек\n\n## Функции\n* Система сохраняет настройку пользователя.\n"


@pytest.fixture(autouse=True)
def _clear_in_memory_runs() -> None:
    store.uploads.clear()
    store.runs.clear()


def _files(tmp_path: Path) -> dict[str, Path]:
    markdown = tmp_path / "requirements.md"
    markdown.write_text(TEXT, encoding="utf-8")

    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "requirements.pdf"
    writer = UnicodePdfWriter(fitz)
    page = writer.new_page()
    for index, line in enumerate(TEXT.splitlines(), 1):
        writer.insert_text(page, (40, 40 + index * 18), line, fontsize=11)
    writer.save(pdf)

    docx = pytest.importorskip("docx")
    word = docx.Document()
    word.add_heading("Сервис настроек", level=1)
    word.add_heading("Функции", level=2)
    word.add_paragraph("Система сохраняет настройку пользователя.", style="List Bullet")
    docx_path = tmp_path / "requirements.docx"
    word.save(docx_path)
    return {"md": markdown, "pdf": pdf, "docx": docx_path}


def _wait_done(run_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = CLIENT.get(f"/api/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"done", "error", "cancelled"}:
            return run
        time.sleep(0.02)
    pytest.fail(f"run {run_id} did not finish")


def _start(path: Path, multipart: bool) -> tuple[str, dict]:
    with path.open("rb") as stream:
        upload = CLIENT.post(
            "/api/runs/upload" if multipart else "/api/documents",
            files={"file": (path.name, stream, "application/octet-stream")},
        )
    assert upload.status_code == 201 if multipart else upload.status_code == 200
    data = upload.json()
    if multipart:
        return data["run_id"], data
    create = CLIENT.post("/api/runs", json={"upload_id": data["upload_id"]})
    assert create.status_code == 201
    return create.json()["run_id"], data


def _stable_report(value: str) -> str:
    ignored = (
        "- Путь:", "- Дата и время:", "- Длительность:",
        "- SHA-256 входного файла:",
    )
    return "\n".join(line for line in value.splitlines() if not line.startswith(ignored))


def test_meta_reports_agent_and_validator_catalog() -> None:
    response = CLIENT.get("/api/meta")

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "agent"
    assert data["groups"] == GROUPS
    assert data["criteria"] == CRITERIA


@pytest.mark.parametrize(
    ("kind", "multipart"), [("md", False), ("pdf", True), ("docx", True)]
)
def test_upload_run_result_and_report_match_cli(
    kind: str, multipart: bool, tmp_path: Path
) -> None:
    source = _files(tmp_path)[kind]
    run_id, upload = _start(source, multipart)
    if not multipart:
        assert upload["preview"]["format"] == kind
    run = _wait_done(run_id)
    assert run["status"] == "done", run.get("error")

    result_response = CLIENT.get(f"/api/runs/{run_id}/result")
    report_response = CLIENT.get(f"/api/runs/{run_id}/report.md")
    assert result_response.status_code == report_response.status_code == 200
    result = result_response.json()
    assert result["contract_version"] == "1.0"
    assert result["document"]["text"]
    assert result["requirements"]
    assert {"passport", "findings", "summary", "verdict", "questions"} <= result.keys()

    cli_report = tmp_path / f"cli-{kind}.md"
    env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, "main.py", str(source), str(cli_report)], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert _stable_report(report_response.text) == _stable_report(cli_report.read_text(encoding="utf-8"))


def test_cancel_stops_running_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "slow.md"
    source.write_text(TEXT, encoding="utf-8")

    def slow_pipeline(doc_path: str, on_progress=None, **kwargs):
        for done in range(500):
            on_progress("checking_requirements", {"done": done, "total": 500})
            time.sleep(0.002)
        raise AssertionError("cancellation callback did not stop the pipeline")

    monkeypatch.setattr("web.runs.load_pipeline", lambda: (slow_pipeline, "agent"))
    run_id, _ = _start(source, multipart=True)
    response = CLIENT.post(f"/api/runs/{run_id}/cancel")
    assert response.status_code == 200
    assert _wait_done(run_id)["status"] == "cancelled"


def test_upload_errors_for_empty_unsupported_and_oversized_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = CLIENT.post("/api/documents", files={"file": ("empty.md", b"", "text/markdown")})
    unsupported = CLIENT.post("/api/documents", files={"file": ("data.csv", b"a,b\n", "text/csv")})
    monkeypatch.setattr(web_app_module, "MAX_BYTES", 10)
    oversized = CLIENT.post("/api/documents", files={"file": ("large.md", b"# x\n" * 20, "text/markdown")})

    assert empty.status_code == 422 and empty.json()["code"] == "empty"
    assert unsupported.status_code == 415 and unsupported.json()["code"] == "format"
    assert oversized.status_code == 413 and oversized.json()["code"] == "size"


def test_application_upload_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app_module, "MAX_BYTES", 20)
    at_limit = CLIENT.post(
        "/api/documents", files={"file": ("limit.txt", b"valid requirement ok", "text/plain")}
    )
    over_limit = CLIENT.post(
        "/api/documents", files={"file": ("large.txt", b"valid requirement ok!", "text/plain")}
    )

    assert at_limit.status_code == 200
    assert over_limit.status_code == 413
    assert over_limit.json()["message"] == "Файл больше 20 МБ"


def test_model_allowlist_is_exposed_and_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ALLOWED_MODELS", "model-a,model-b")
    meta = CLIENT.get("/api/meta")
    response = CLIENT.post("/api/runs", json={"upload_id": "missing", "model": "model-x"})

    assert meta.json()["allowed_models"] == ["model-a", "model-b"]
    assert response.status_code == 400
    assert "не разрешена" in response.json()["detail"]
