from pathlib import Path
import re

from validator.config import Settings
from validator.pipeline import run_pipeline
from tests.pdf_helpers import UnicodePdfWriter


ROOT = Path(__file__).parents[1]


def test_pdf_without_markdown_headings_runs_mock_pipeline(tmp_path: Path) -> None:
    import pytest

    fitz = pytest.importorskip("fitz")
    markdown = (ROOT / "docs" / "requirements.md").read_text(encoding="utf-8")
    plain_lines = [re.sub(r"^#{1,6}\s+", "", line) for line in markdown.splitlines()]
    pdf_path = tmp_path / "requirements.pdf"
    pdf = UnicodePdfWriter(fitz)
    for offset in range(0, len(plain_lines), 45):
        page = pdf.new_page()
        pdf.insert_text(page, (36, 36), "Документ требований", fontsize=9)
        for row, line in enumerate(plain_lines[offset : offset + 45], 1):
            pdf.insert_text(page, (36, 36 + row * 16), line[:140], fontsize=8)
    pdf.save(pdf_path)

    result = run_pipeline(
        str(pdf_path), str(tmp_path / "report.md"), Settings("mock", "mock-deterministic")
    )

    title = result.payload["metadata"]["title"]
    assert title
    assert len(re.findall(r"[A-Za-zА-Яа-я0-9]", title)) >= 3
    assert result.requirements_count > 0
