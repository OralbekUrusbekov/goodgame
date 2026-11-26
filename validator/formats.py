from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
from typing import Any


def markdown_to_html(markdown: str, title: str) -> str:
    body: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                body.append("</ul>")
                in_list = False
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            body.append(f"<h{level}>{escape(heading.group(2))}</h{level}>")
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{escape(line[2:])}</li>")
        elif line.startswith("|"):
            body.append(f"<pre class='table-row'>{escape(line)}</pre>")
        else:
            body.append(f"<p>{escape(line)}</p>")
    if in_list:
        body.append("</ul>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{escape(title)}</title><style>body{{font:16px/1.55 system-ui;max-width:1100px;margin:auto;padding:32px;color:#172033}}h1,h2{{color:#243c91}}pre{{white-space:pre-wrap;background:#f2f5fa;padding:8px}}@media print{{body{{padding:0}}}}</style></head><body>{''.join(body)}</body></html>"""


def write_artifacts(
    destination: str | Path, markdown: str, payload: dict[str, Any], also_pdf: bool = False
) -> tuple[Path, Path, list[Path]]:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower() or ".md"
    extras: list[Path] = []
    if suffix == ".html":
        path.write_text(markdown_to_html(markdown, payload["metadata"].get("title", "Отчёт")), encoding="utf-8")
    elif suffix == ".docx":
        from docx import Document as WordDocument
        word = WordDocument()
        for raw in markdown.splitlines():
            heading = re.match(r"^(#{1,6})\s+(.+)$", raw)
            if heading:
                word.add_heading(heading.group(2), level=min(len(heading.group(1)), 9))
            elif raw.startswith("- "):
                word.add_paragraph(raw[2:], style="List Bullet")
            elif raw.strip():
                word.add_paragraph(raw)
        word.save(path)
    elif suffix == ".json":
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(markdown, encoding="utf-8")
    json_path = path if suffix == ".json" else path.with_suffix(".json")
    if json_path != path:
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if suffix not in {".md", ".json"}:
        markdown_path = path.with_suffix(".md")
        markdown_path.write_text(markdown, encoding="utf-8")
        extras.append(markdown_path)
    if also_pdf:
        try:
            import fitz
            pdf_path = path.with_suffix(".pdf")
            pdf = fitz.open()
            page = pdf.new_page()
            remaining = markdown
            while remaining:
                chunk, remaining = remaining[:5000], remaining[5000:]
                page.insert_textbox(page.rect + (36, 36, -36, -36), chunk, fontsize=8)
                if remaining:
                    page = pdf.new_page()
            pdf.save(pdf_path)
            pdf.close()
            extras.append(pdf_path)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
    return path, json_path, extras
