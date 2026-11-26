from pathlib import Path

from validator.loader import load_document
from validator.segmenter import split_for_segmentation


def test_markdown_loader_tracks_title_section_and_lines(tmp_path: Path) -> None:
    source = tmp_path / "sample.md"
    source.write_text("# Документ\n\n## Функции\nСистема сохраняет значение.\n", encoding="utf-8")

    document = load_document(source)

    assert document.title == "Документ"
    assert document.lines[-1].section == "Функции"
    assert document.location_for("Система сохраняет значение") == "Функции, строка 4"


def test_unknown_format_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "sample.csv"
    source.write_text("a,b", encoding="utf-8")
    try:
        load_document(source)
    except ValueError as error:
        assert "Неподдерживаемый формат" in str(error)
    else:
        raise AssertionError("ValueError expected")


def test_long_document_is_split_by_h2_into_at_most_three_chunks(tmp_path: Path) -> None:
    sections = [f"## Часть {index}\n" + (f"Требование {index}. " * 30) + "\n" for index in range(1, 7)]
    source = tmp_path / "long.md"
    source.write_text("# Большой документ\n" + "".join(sections), encoding="utf-8")
    document = load_document(source)

    chunks = split_for_segmentation(document, threshold=300, max_chunks=3)

    assert 2 <= len(chunks) <= 3
    assert "".join(chunks) == document.text
    for index in range(1, 7):
        assert sum(f"## Часть {index}" in chunk for chunk in chunks) == 1
