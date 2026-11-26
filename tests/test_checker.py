from pathlib import Path

from validator.checker import check_requirements
from validator.loader import load_document
from validator.models import CRITERIA, Requirement


class IncompleteProvider:
    name = "test"
    model = "test"

    def __init__(self) -> None:
        self.users: list[str] = []

    def generate_json(self, system: str, user: str, schema=None, operation: str = "llm") -> dict:
        self.users.append(user)
        requirement_id = "R-01" if len(self.users) == 1 else "R-02"
        return {
            "checks": [{
                "requirement_id": requirement_id,
                "verdicts": {criterion: "ok" for criterion in CRITERIA},
                "findings": [],
            }]
        }


def test_missing_batch_items_are_retried_once_only_for_missing_ids(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("# Документ\nПервое требование.\nВторое требование.\n", encoding="utf-8")
    document = load_document(source)
    requirements = [
        Requirement("R-01", "—", "Документ", "Первое", "Первое требование."),
        Requirement("R-02", "—", "Документ", "Второе", "Второе требование."),
    ]
    provider = IncompleteProvider()

    checks = check_requirements(document, requirements, provider, batch_size=12)

    assert [item.requirement_id for item in checks] == ["R-01", "R-02"]
    assert len(provider.users) == 2
    retry_catalog = provider.users[1].partition("ТРЕБОВАНИЯ:\n")[2].partition("\nПОЛНЫЙ ДОКУМЕНТ:")[0]
    assert "R-02" in retry_catalog
    assert "R-01" not in retry_catalog
