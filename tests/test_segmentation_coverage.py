from pathlib import Path

from validator.config import Settings
from validator.llm import MockProvider
from validator.loader import load_document
from validator.segmenter import segment, split_for_segmentation
from validator.set_checker import check_set


ROOT = Path(__file__).parents[1]


def test_bundled_document_is_split_into_two_top_level_parts() -> None:
    document = load_document(ROOT / "docs" / "requirements.md")

    chunks = split_for_segmentation(document)

    assert len(document.text) > 6000
    assert len(chunks) == 2
    assert "".join(chunks) == document.text
    assert all(chunk.lstrip().startswith(("#", "##")) for chunk in chunks)


def test_mock_exercises_split_segmentation_and_coverage_call() -> None:
    document = load_document(ROOT / "docs" / "requirements.md")
    provider = MockProvider(Settings("mock", "mock-deterministic"))

    requirements, warnings, coverage = segment(document, provider, include_coverage=True)

    assert provider.operations[:2] == ["segment_1", "segment_2"]
    assert "coverage" in provider.operations
    assert len(requirements) > 25
    assert coverage["total_units"] > 0
    assert 0 <= coverage["percent"] <= 100
    assert sum(item.quote_verified for item in requirements) >= len(requirements) - len(warnings)


def test_mock_exercises_two_set_checks() -> None:
    document = load_document(ROOT / "docs" / "requirements.md")
    provider = MockProvider(Settings("mock", "mock-deterministic"))
    requirements, _ = segment(document, provider)
    provider.operations.clear()

    check_set(document, requirements, provider)

    assert provider.operations == ["set_consistency", "set_completeness"]
