"""Общие текстовые утилиты веба: нормализация для поиска цитат и разделы документа."""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")
_STRIP = str.maketrans({"*": "", "`": "", "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "ё": "е", "Ё": "Е",
                        " ": " ", " ": " ", "–": "-", "—": "-"})


def norm(s: str) -> str:
    return _WS.sub(" ", s.translate(_STRIP)).strip().lower()


def find_quote(text: str, quote: str) -> int:
    """Позиция цитаты в исходном тексте (с нормализацией пробелов и разметки) или -1."""
    if not quote or not quote.strip():
        return -1
    pos = text.find(quote)
    if pos >= 0:
        return pos
    # строим нормализованный текст с картой индексов в исходный
    out, idx = [], []
    prev_space = True
    for i, ch in enumerate(text):
        c = ch.translate(_STRIP)
        if not c:
            continue
        if c.isspace():
            if prev_space:
                continue
            c, prev_space = " ", True
        else:
            prev_space = False
        out.append(c.lower())
        idx.append(i)
    p = "".join(out).find(norm(quote))
    return idx[p] if p >= 0 else -1


def headings(text: str) -> list[tuple[int, int, str]]:
    """[(позиция, уровень, заголовок)] по markdown-заголовкам."""
    res = []
    for m in re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", text):
        res.append((m.start(), len(m.group(1)), m.group(2).strip().strip("*")))
    return res


def section_path(text: str, pos: int, hs: list[tuple[int, int, str]] | None = None) -> str:
    stack: list[tuple[int, str]] = []
    for start, level, title in hs if hs is not None else headings(text):
        if start > pos:
            break
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    parts = [t for lvl, t in stack if lvl >= 2] or [t for _, t in stack]
    return " → ".join(parts[-3:]) if parts else "Документ"


def plain(s: str, limit: int = 110) -> str:
    s = re.sub(r"[*`#]", "", s)
    s = re.sub(r"\s*\|\s*", " · ", s).strip(" ·")
    s = _WS.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
