from __future__ import annotations

import json
from typing import Any

from .llm import LLMProvider
from .models import Requirement


TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "test_cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "requirement_ids": {"type": "array", "items": {"type": "string"}},
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "expected_result": {"type": "string"},
                    "boundary_values": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "requirement_ids", "preconditions", "steps", "expected_result", "boundary_values"],
            },
        }
    },
    "required": ["test_cases"],
}


def generate_test_cases(requirements: list[Requirement], provider: LLMProvider) -> list[dict[str, Any]]:
    system = """Создай черновики тест-кейсов только для функциональных требований.
Покрой позитивные, негативные и граничные сценарии, не придумывая новых бизнес-правил.
Каждый тест должен ссылаться на ID требований и содержать предусловия, шаги, ожидаемый результат
и граничные значения. Верни только JSON по схеме."""
    response = provider.generate_json(
        system,
        "ОПЕРАЦИЯ: TEST_CASES\nТРЕБОВАНИЯ:\n" + json.dumps(
            [vars(requirement) for requirement in requirements], ensure_ascii=False
        ),
        schema=TEST_SCHEMA,
        operation="test_cases",
    )
    return [item for item in response.get("test_cases", []) if isinstance(item, dict)]
