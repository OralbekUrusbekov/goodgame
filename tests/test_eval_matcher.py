from __future__ import annotations

import pytest

from eval.matcher import evaluate, render_results


def _report(finding: dict, requirements: list[dict] | None = None) -> dict:
    return {
        "requirements": requirements or [],
        "requirement_checks": [{"findings": [{**finding, "quote_verified": True}]}],
        "set_check": {"findings": []},
    }


def test_lenient_uses_quote_of_linked_requirement() -> None:
    report = _report(
        {
            "criterion": "complete",
            "quote": "Соседнее предложение того же требования.",
            "related_quotes": [],
            "requirement_ids": ["R-07"],
            "explanation": "Не определён переход состояния при изменении значения.",
        },
        [{"report_id": "R-07", "quote": "Исходная формулировка требования."}],
    )
    cases = [{
        "id": "linked", "category": "complete",
        "anchor_quotes": ["Исходная формулировка требования."],
    }]

    result = evaluate(report, cases)

    assert result["strict_recall"] == 0.0
    assert result["lenient_recall"] == 1.0
    assert result["rows"][0]["lenient_match"].startswith("complete — Не определён переход")


def test_lenient_uses_sibling_atom_with_same_source_id() -> None:
    report = _report(
        {
            "criterion": "complete", "quote": "Второй атом.", "related_quotes": [],
            "requirement_ids": ["R-02"], "explanation": "Не описан пограничный переход.",
        },
        [
            {"report_id": "R-01", "source_id": "DOC-7", "quote": "Первый атом с якорем."},
            {"report_id": "R-02", "source_id": "DOC-7", "quote": "Второй атом."},
        ],
    )

    result = evaluate(report, [{
        "id": "sibling", "category": "complete", "anchor_quotes": ["Первый атом с якорем."],
    }])

    assert result["strict_recall"] == 0.0
    assert result["lenient_recall"] == 1.0


def test_lenient_uses_set_traceability_without_changing_strict() -> None:
    report = _report({
        "criterion": "verifiable", "quote": "Другая цитата.", "related_quotes": [],
        "requirement_ids": [], "explanation": "Другой дефект.",
    })
    report["set_check"]["traceability"] = "Критерии приёмки не покрывают функциональную ветвь."
    report["set_check"]["verdicts"] = {"traceable": "violation"}

    result = evaluate(report, [{
        "id": "traceability", "category": "complete", "anchor_quotes": ["Критерии приёмки"],
    }])

    assert result["strict_recall"] == 0.0
    assert result["lenient_recall"] == 1.0
    assert result["rows"][0]["lenient_match"].startswith("traceable —")


def test_lenient_matches_short_anchor_with_inflected_word() -> None:
    report = _report({
        "criterion": "traceable", "quote": "Пробел между критериями приёмки и правилами.",
        "related_quotes": [], "requirement_ids": [], "explanation": "Есть разрыв покрытия.",
    })

    result = evaluate(report, [{
        "id": "inflection", "category": "complete", "anchor_quotes": ["Критерии приёмки"],
    }])

    assert result["strict_recall"] == 0.0
    assert result["lenient_recall"] == 1.0


@pytest.mark.parametrize(
    ("case_category", "finding_criterion"),
    [
        ("complete", "traceable"),
        ("traceable", "set_complete"),
        ("consistent", "singular"),
        ("correct", "conforming"),
        ("verifiable", "unambiguous"),
    ],
)
def test_lenient_accepts_configured_criterion_groups(
    case_category: str, finding_criterion: str
) -> None:
    report = _report({
        "criterion": finding_criterion, "quote": "Общий якорь.", "related_quotes": [],
        "requirement_ids": [], "explanation": "Осмысленное объяснение дефекта.",
    })

    result = evaluate(report, [{
        "id": "group", "category": case_category, "anchor_quotes": ["Общий якорь."],
    }])

    assert result["lenient_recall"] == 1.0
    assert finding_criterion in result["rows"][0]["lenient_match"]


def test_lenient_rejects_other_group_even_with_same_anchor() -> None:
    report = _report({
        "criterion": "verifiable", "quote": "Совпадающий якорь.", "related_quotes": [],
        "requirement_ids": [], "explanation": "Другая категория дефекта.",
    })

    result = evaluate(report, [{
        "id": "negative", "category": "complete", "anchor_quotes": ["Совпадающий якорь."],
    }])

    assert result["lenient_recall"] == 0.0
    assert result["rows"][0]["lenient_match"] == ""


def test_render_shows_truncated_lenient_match() -> None:
    explanation = "Очень длинное объяснение " + "x" * 120
    result = evaluate(
        _report({
            "criterion": "complete", "quote": "Якорь.", "related_quotes": [],
            "requirement_ids": [], "explanation": explanation,
        }),
        [{"id": "shown", "category": "complete", "anchor_quotes": ["Якорь."]}],
    )

    rendered = render_results(result)

    assert "Lenient-находка" in rendered
    assert f"complete — {explanation[:80]}" in rendered
    assert explanation not in rendered
