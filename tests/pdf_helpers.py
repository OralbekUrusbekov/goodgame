from __future__ import annotations

from pathlib import Path
from typing import Any


FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
)


class UnicodePdfWriter:
    """Small PyMuPDF writer that always embeds a Cyrillic-capable font."""

    def __init__(self, fitz: Any):
        self.fitz = fitz
        self.document = fitz.open()
        self._font_names: dict[int, str] = {}

    def new_page(self) -> Any:
        page = self.document.new_page()
        font_file = next((path for path in FONT_CANDIDATES if path.is_file()), None)
        if font_file:
            page.insert_font(fontname="unicode", fontfile=str(font_file))
            self._font_names[id(page)] = "unicode"
        else:
            page.insert_font(fontname="china-s")
            self._font_names[id(page)] = "china-s"
        return page

    def insert_text(
        self, page: Any, point: tuple[float, float], text: str, fontsize: float
    ) -> None:
        page.insert_text(
            point, text, fontsize=fontsize, fontname=self._font_names[id(page)]
        )

    def save(self, path: Path) -> None:
        self.document.save(path)
        self.document.close()
