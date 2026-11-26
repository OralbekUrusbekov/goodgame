from __future__ import annotations

from pathlib import Path

from validator.loader import load_document
from validator.prompts import completeness_prompt


def test_completeness_prompt_covers_state_transitions_branches_and_empty_sections(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Требования\n* Система хранит настройку.\n", encoding="utf-8")
    document = load_document(source)

    system, _ = completeness_prompt(document, [], domain_check=False)

    assert "изменении настройки или параметра" in system
    assert "ниже, равно и выше" in system
    assert "каждой функциональной ветви" in system
    assert "пустые разделы" in system
    assert not any(value in system for value in ("Алматы", "тиын", "Ф-1", "saveLimit"))
