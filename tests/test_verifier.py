from pathlib import Path

from validator.loader import load_document
from validator.models import Finding
from validator.verifier import quote_exists, verify_findings


def test_quote_matching_is_normalized_and_fuzzy() -> None:
    assert quote_exists("Порог равен 1 000 тенге", "Порог равен 1 000   тенге.")


def test_unverified_finding_is_kept_and_deduplicated(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Требования\nТочная фраза документа.\n", encoding="utf-8")
    document = load_document(source)
    finding = Finding("complete", "major", "Не хватает границы.", "Указать границу.", "Выдуманная цитата", "Раздел не определён")

    result = verify_findings(document, [finding, Finding(**vars(finding))])

    assert len(result) == 1
    assert not result[0].quote_verified
    assert "цитата не подтверждена" in result[0].explanation
