from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest

from tests.pdf_helpers import UnicodePdfWriter
from validator.loader import load_document
from validator.verifier import normalize


ROOT = Path(__file__).parents[1]


def _offline_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({"LLM_PROVIDER": "mock", "LLM_CACHE": "0", "LLM_FALLBACK": ""})
    return env


def _run_cli(source: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "main.py", str(source), str(report)],
        cwd=ROOT, env=_offline_env(), capture_output=True, text=True, timeout=60,
    )


def _make_pdf(path: Path, text: str) -> None:
    fitz = pytest.importorskip("fitz")
    writer = UnicodePdfWriter(fitz)
    page = writer.new_page()
    y = 45
    for source_line in text.splitlines():
        wrapped = textwrap.wrap(source_line, width=92, replace_whitespace=False) or [""]
        for line in wrapped:
            if y > 800:
                page = writer.new_page()
                y = 45
            writer.insert_text(page, (36, y), line, fontsize=8)
            y += 10
        y += 2
    writer.save(path)


def _make_docx(path: Path, text: str) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            document.add_heading(match.group(2), level=len(match.group(1)))
        else:
            document.add_paragraph(line)
    document.save(path)


@pytest.fixture(scope="module")
def source_text() -> str:
    return (ROOT / "docs" / "requirements.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def input_documents(tmp_path_factory: pytest.TempPathFactory, source_text: str) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("task_formats")
    markdown = ROOT / "docs" / "requirements.md"
    pdf = folder / "requirements.pdf"
    docx = folder / "requirements.docx"
    _make_pdf(pdf, source_text)
    _make_docx(docx, source_text)
    return {"md": markdown, "pdf": pdf, "docx": docx}


@pytest.mark.parametrize("kind", ["md", "pdf", "docx"])
def test_one_command_cli_produces_complete_russian_report(
    kind: str, input_documents: dict[str, Path], tmp_path: Path
) -> None:
    source = input_documents[kind]
    report = tmp_path / f"report-{kind}.md"
    completed = _run_cli(source, report)

    assert completed.returncode == 0, completed.stderr
    assert report.is_file() and report.with_suffix(".json").is_file()
    markdown = report.read_text(encoding="utf-8")
    payload = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    document = load_document(source)

    assert re.search(r"[А-Яа-яЁё]", markdown)
    for number, title in enumerate((
        "Паспорт прогона", "Как агент нарезал требования", "Проверка каждого требования",
        "Проверка всего набора требований", "Сводка", "Итог",
    ), 1):
        assert f"## {number}. {title}" in markdown
    assert "Название документа:" in markdown
    assert "Дата и время:" in markdown
    assert "Провайдер и модель: mock / mock-deterministic" in markdown
    assert re.search(r"Версия агента: .*\+[0-9a-f]{7,}", markdown)
    assert "ID в отчёте | ID в документе" in markdown
    assert "Требований выделено:" in markdown and "Нарушений:" in markdown
    assert "### Критические дефекты" in markdown
    assert "Можно ли начинать разработку и тестирование:" in markdown
    assert "Что исправить в первую очередь" in markdown
    assert payload["metadata"]["cache_hits"] == 0

    findings = [
        finding
        for check in payload["requirement_checks"]
        for finding in check["findings"]
    ] + payload["set_check"]["findings"]
    assert findings
    for finding in findings:
        assert finding["quote"]
        assert finding["location"] and finding["location"] != "Раздел не определён"
        if finding["quote_verified"]:
            assert normalize(finding["quote"]) in normalize(document.text)

    unnumbered = [r for r in payload["requirements"] if r["source_id"] == "—"]
    assert unnumbered
    assert all(re.fullmatch(r"R-\d+", r["report_id"]) and r["quote"] for r in unnumbered)


def test_example_config_and_repository_contain_no_secrets() -> None:
    example = ROOT / ".env.example"
    assert example.is_file()
    content = example.read_text(encoding="utf-8")
    assert re.search(r"(?m)^GEMINI_API_KEY=\s*$", content)
    assert re.search(r"(?m)^OPENAI_API_KEY=\s*$", content)

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    secret = re.compile(r"(?:AIza[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z_-]{20,})")
    offenders = []
    for relative in listed:
        if relative == ".env" or relative.startswith((".venv/", ".cache/")):
            continue
        path = ROOT / relative
        if path.is_file():
            try:
                value = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if secret.search(value):
                offenders.append(relative)
    assert offenders == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [("missing.md", "Документ не найден"), ("unsupported.csv", "Неподдерживаемый формат")],
)
def test_cli_rejects_missing_and_unsupported_input(
    name: str, expected: str, tmp_path: Path
) -> None:
    source = tmp_path / name
    if source.suffix == ".csv":
        source.write_text("id,requirement\n1,test\n", encoding="utf-8")
    completed = _run_cli(source, tmp_path / "report.md")

    assert completed.returncode != 0
    assert expected in completed.stderr
