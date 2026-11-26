"""Лёгкое чтение документа для предпросмотра на экране загрузки и для демо-конвейера.

Настоящее чтение (с таблицами и разделами) делает validator/. Здесь — только то,
что нужно вебу до запуска проверки: формат, страницы, текст, заголовок, грубые метрики.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {".pdf": "pdf", ".md": "md", ".markdown": "md", ".txt": "txt", ".docx": "docx"}
MAX_BYTES = 20 * 1024 * 1024

REQ_MARKER_RE = re.compile(r"(?<![\w-])([A-ZА-ЯЁ]{1,6}-\d{1,3}(?:\.\d+)?)(?![\w])")
SHALL_RE = re.compile(r"\b(должен|должна|должно|должны|shall|must|требуется|необходимо)\b", re.I)


class DocumentError(Exception):
    """Файл не принят. code — машинный код для фронтенда."""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class ReadResult:
    text: str
    format: str
    pages: int | None
    tables: int
    title: str
    req_markers: list[str] = field(default_factory=list)
    shall_count: int = 0

    @property
    def looks_like_requirements(self) -> bool:
        return bool(self.req_markers) or self.shall_count > 0 or "требован" in self.text.lower()


def detect_format(filename: str) -> str | None:
    return SUPPORTED.get(Path(filename).suffix.lower())


def read_document(path: Path) -> ReadResult:
    path = Path(path)
    fmt = detect_format(path.name)
    if not fmt:
        raise DocumentError(
            "format",
            "Этот формат агент не читает",
            "Поддерживаются PDF с текстовым слоем, DOCX и Markdown.",
        )
    if path.stat().st_size > MAX_BYTES:
        raise DocumentError("size", "Файл больше 20 МБ", "Сохраните документ без изображений или разбейте на части.")

    pages: int | None = None
    if fmt == "pdf":
        text, pages = _read_pdf(path)
    elif fmt == "docx":
        text = _read_docx(path)
    else:
        text = _read_text(path)

    text = _clean(text)
    if not text.strip():
        raise DocumentError("empty", "В документе нет текста", "Проверьте, что файл не пустой.")

    return ReadResult(
        text=text,
        format=fmt,
        pages=pages,
        tables=count_md_tables(text),
        title=guess_title(text, path.stem),
        req_markers=list(dict.fromkeys(REQ_MARKER_RE.findall(text))),
        shall_count=len(SHALL_RE.findall(text)),
    )


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_pdf(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise DocumentError("internal", "Не установлен pypdf", "pip install -r requirements.txt") from e
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        raise DocumentError("broken", "PDF повреждён или не открывается", "Сохраните PDF заново из редактора.") from e
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")
        except Exception:
            ok = 0
        if not ok:
            raise DocumentError(
                "locked",
                "Документ защищён паролем",
                "Снимите защиту в редакторе и загрузите снова — пароль сервис не запрашивает и не хранит.",
            )
    texts: list[str] = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    pages = len(reader.pages)
    if sum(len(t.strip()) for t in texts) < 40 * max(1, pages):
        raise DocumentError(
            "scan",
            "Это скан, а не текст",
            "В PDF только изображения страниц. Загрузите исходный DOCX или PDF, сохранённый из редактора.",
        )
    return "\n\n".join(texts), pages


def _read_docx(path: Path) -> str:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:  # pragma: no cover
        raise DocumentError("internal", "Не установлен python-docx", "pip install -r requirements.txt") from e
    try:
        document = docx.Document(str(path))
    except Exception as e:
        raise DocumentError("broken", "DOCX повреждён или не открывается", "Сохраните документ заново.") from e

    lines: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, document)
            text = p.text.strip()
            if not text:
                lines.append("")
                continue
            style = (p.style.name if p.style is not None else "") or ""
            m = re.match(r"(?:Heading|Заголовок)\s*(\d)", style)
            if m:
                lines.append("#" * int(m.group(1)) + " " + text)
            elif "List" in style or "Спис" in style:
                lines.append("* " + text)
            else:
                lines.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            lines.append("")
            for i, row in enumerate(table.rows):
                cells = [c.text.replace("\n", " ").strip() for c in row.cells]
                lines.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    lines.append("|" + "|".join([" --- "] * len(cells)) + "|")
            lines.append("")
    return "\n".join(lines)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("­", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def count_md_tables(text: str) -> int:
    count, inside = 0, False
    for line in text.splitlines():
        is_row = line.lstrip().startswith("|")
        if is_row and not inside:
            count += 1
        inside = is_row
    return count


def guess_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in text.splitlines():
        s = line.strip().strip("#*").strip()
        if 3 <= len(s) <= 120:
            return s
    return fallback
