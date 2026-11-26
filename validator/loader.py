from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class SourceLine:
    number: int
    section: str
    text: str


@dataclass(frozen=True)
class Document:
    path: Path
    title: str
    text: str
    lines: tuple[SourceLine, ...]

    def location_for(self, quote: str) -> str:
        needle = _normalize(quote)
        for line in self.lines:
            candidate = _normalize(line.text)
            if needle and candidate and (needle in candidate or candidate in needle):
                return f"{line.section}, строка {line.number}"
        return "Раздел не определён"

    def line_for(self, quote: str) -> int | None:
        needle = _normalize(quote)
        for line in self.lines:
            candidate = _normalize(line.text)
            if needle and candidate and (needle in candidate or candidate in needle):
                return line.number
        return None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _meaningful_title(value: str) -> bool:
    return sum(character.isalnum() for character in value) >= 3


def _pdf_table_rows(rows: list[list[object]]) -> list[str]:
    normalized = [
        [str(cell or "").replace("\n", " ").strip() for cell in row]
        for row in rows
    ]
    width = max((len(row) for row in normalized), default=0)
    if not width:
        return []
    padded = [row + [""] * (width - len(row)) for row in normalized]
    keep_columns = [
        index for index in range(width)
        if any(row[index] for row in padded)
    ]
    result: list[str] = []
    for row in padded:
        cells = [row[index] for index in keep_columns]
        deduplicated: list[str] = []
        for cell in cells:
            if cell and deduplicated and cell == deduplicated[-1]:
                continue
            deduplicated.append(cell)
        result.append("| " + " | ".join(deduplicated) + " |")
    return result


def _from_text(path: Path, text: str, markdown: bool) -> Document:
    heading = "Начало документа"
    title = path.stem
    result: list[SourceLine] = []
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if markdown and re.match(r"^#{1,6}\s+", stripped):
            heading = re.sub(r"^#{1,6}\s+", "", stripped).strip()
            if stripped.startswith("# ") and title == path.stem:
                title = heading
        result.append(SourceLine(number, heading, raw))
    return Document(path, title, text, tuple(result))


def _load_pdf(path: Path) -> Document:
    import fitz

    rendered: list[str] = []
    lines: list[SourceLine] = []
    number = 0
    heading = "Начало документа"
    title = path.stem
    with fitz.open(path) as pdf:
        for page_index, page in enumerate(pdf, 1):
            table_items: list[tuple[float, float, list[str]]] = []
            table_boxes: list[fitz.Rect] = []
            try:
                finder = page.find_tables()
                for table in finder.tables:
                    box = fitz.Rect(table.bbox)
                    table_boxes.append(box)
                    rows = table.extract() or []
                    markdown_rows = _pdf_table_rows(rows)
                    table_items.append((box.y0, box.x0, markdown_rows))
            except (AttributeError, TypeError, ValueError):
                table_boxes = []
            text_items: list[tuple[float, float, list[str], bool]] = []
            page_dict = page.get_text("dict")
            sizes = [
                float(span.get("size", 0)) for block in page_dict.get("blocks", [])
                for line in block.get("lines", []) for span in line.get("spans", []) if span.get("text", "").strip()
            ]
            base_size = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
            for block in page_dict.get("blocks", []):
                if "lines" not in block:
                    continue
                box = fitz.Rect(block.get("bbox", (0, 0, 0, 0)))
                if any(box.intersects(table_box) and box.get_area() > 0 for table_box in table_boxes):
                    continue
                block_lines: list[str] = []
                is_heading = False
                for line_data in block.get("lines", []):
                    spans = line_data.get("spans", [])
                    text = "".join(span.get("text", "") for span in spans).strip()
                    if not text:
                        continue
                    max_size = max((float(span.get("size", 0)) for span in spans), default=0)
                    bold = any("bold" in str(span.get("font", "")).lower() for span in spans)
                    numbered = bool(re.match(r"^(?:\d+(?:\.\d+)*[.)]?|[A-ZА-Я][.)])\s+", text))
                    is_heading = is_heading or max_size >= base_size * 1.25 or (bold and len(text) < 140) or numbered
                    block_lines.append(text)
                if block_lines:
                    text_items.append((box.y0, box.x0, block_lines, is_heading))
            combined: list[tuple[float, float, list[str], bool]] = text_items + [
                (y, x, rows, False) for y, x, rows in table_items
            ]
            for _, _, item_lines, is_heading in sorted(combined, key=lambda item: (item[0], item[1])):
                for raw in item_lines:
                    if is_heading and len(item_lines) == 1:
                        heading = raw
                        if title == path.stem and _meaningful_title(raw):
                            title = raw
                    rendered.append(raw)
                    number += 1
                    lines.append(SourceLine(number, heading or f"Страница {page_index}", raw))
            if page_index < len(pdf):
                rendered.append("")
                number += 1
                lines.append(SourceLine(number, heading, ""))
    text = "\n".join(rendered)
    if not _meaningful_title(title):
        title = next(
            (
                line.text.strip() for line in lines
                if _meaningful_title(line.text.strip())
            ),
            path.stem,
        )
    return Document(path, title, text, tuple(lines))


def _load_docx(path: Path) -> Document:
    from docx import Document as WordDocument

    word = WordDocument(path)
    collected: list[tuple[str, str]] = []
    heading = "Начало документа"
    title = path.stem
    for paragraph in word.paragraphs:
        value = paragraph.text.strip()
        if paragraph.style and paragraph.style.name.startswith("Heading") and value:
            heading = value
            if title == path.stem:
                title = value
        if value:
            collected.append((heading, value))
    for table_index, table in enumerate(word.tables, 1):
        table_section = f"{heading} — таблица {table_index}"
        for row in table.rows:
            values = [cell.text.strip() for cell in row.cells]
            collected.append((table_section, " | ".join(values)))
    lines = tuple(SourceLine(i, section, value) for i, (section, value) in enumerate(collected, 1))
    return Document(path, title, "\n".join(item.text for item in lines), lines)


def load_document(filename: str | Path) -> Document:
    path = Path(filename)
    if not path.is_file():
        raise FileNotFoundError(f"Документ не найден: {path}")
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return _from_text(path, path.read_text(encoding="utf-8"), suffix == ".md")
    if suffix == ".pdf":
        return _load_pdf(path)
    if suffix == ".docx":
        return _load_docx(path)
    raise ValueError(f"Неподдерживаемый формат: {suffix}. Допустимы .md, .txt, .pdf, .docx")
