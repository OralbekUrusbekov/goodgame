from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.matcher import evaluate, render_results
from validator.cache import LLMCache
from validator.context import check_context, extract_context
from validator.diff import compare_with_previous
from validator.formats import write_artifacts
from validator.llm import LLMProvider, MockProvider, ProviderRouter, mask_secrets
from validator.loader import load_document
from validator.models import Requirement
from validator.config import Settings
from validator.config import load_config
from tests.pdf_helpers import UnicodePdfWriter


def mock_settings() -> Settings:
    return Settings(provider="mock", model="mock-deterministic", cache_enabled=False)


def test_current_gemini_defaults_and_fallback_chain(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("LLM_FALLBACK", raising=False)
    settings = load_config(provider="gemini")
    assert settings.model == "gemini-3.5-flash"
    assert settings.llm_fallback == "gemini:gemini-3.5-flash-lite,gemini:gemini-2.5-flash"


def test_cache_prevents_second_provider_call(tmp_path: Path) -> None:
    provider = MockProvider(mock_settings())
    router = ProviderRouter([provider], LLMCache(tmp_path / "cache", enabled=True))
    user = "ОПЕРАЦИЯ: SEGMENT\nДОКУМЕНТ:\n- Система сохраняет значение пользователя."
    first = router.generate_json("system", user, operation="segment")
    second = router.generate_json("system", user, operation="segment")
    assert first == second
    assert router.calls == 1
    assert router.cache_hits == 1


def test_fallback_switches_on_quota_error(tmp_path: Path) -> None:
    class Broken(LLMProvider):
        name, model = "gemini", "primary"

        def generate_json(self, system, user, schema=None, operation="llm"):
            raise RuntimeError("Исчерпана квота Gemini")

    class Working(LLMProvider):
        name, model = "mock", "fallback"

        def generate_json(self, system, user, schema=None, operation="llm"):
            return {"ok": True}

    router = ProviderRouter([Broken(), Working()], LLMCache(tmp_path / "off", enabled=False))
    assert router.generate_json("s", "u", operation="step") == {"ok": True}
    assert router.model == "fallback"
    assert router.models_by_step["step"] == ["mock:fallback"]


def test_fallback_switches_on_http_429(tmp_path: Path) -> None:
    class Quota429(LLMProvider):
        name, model = "gemini", "primary"

        def generate_json(self, system, user, schema=None, operation="llm"):
            error = RuntimeError("HTTP 429 resource exhausted")
            error.status_code = 429
            raise error

    fallback = MockProvider(mock_settings())
    router = ProviderRouter([Quota429(), fallback], LLMCache(tmp_path, enabled=False))

    assert router.generate_json("s", "ОПЕРАЦИЯ: CONSISTENCY\n", operation="step")["findings"] == []
    assert router.model == "mock-deterministic"


def test_secret_masking_supports_new_gemini_format() -> None:
    secret = "AQ." + "x" * 32
    assert secret not in mask_secrets(f"Ошибка с ключом {secret}")


def test_context_mock_and_quote_verification(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("# Каталог\n- Система сохраняет настройку.\n", encoding="utf-8")
    context = tmp_path / "passport.md"
    context.write_text("# Паспорт\nЦель проекта — сохранить настройку пользователя.\n", encoding="utf-8")
    provider = MockProvider(mock_settings())
    bundle = extract_context([str(context)], provider)
    requirement = Requirement("R-01", "—", "Каталог", "Сохранение", "Система сохраняет настройку.")
    result = check_context(bundle, [requirement], provider, load_document(source))
    assert bundle.extracted["goals"][0]["quote_verified"] is True
    assert result["traceability_matrix"][0]["requirement_ids"] == ["R-01"]


def test_diff_and_all_output_formats(tmp_path: Path) -> None:
    old = {"requirement_checks": [{"findings": [{"criterion": "complete", "quote": "старый текст", "explanation": "x"}]}], "set_check": {"findings": []}}
    old_path = tmp_path / "old.json"
    old_path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    current = {"metadata": {"title": "Тест"}, "requirement_checks": [{"findings": [{"criterion": "complete", "quote": "новый текст", "explanation": "y"}]}], "set_check": {"findings": []}}
    diff = compare_with_previous(current, str(old_path))
    assert len(diff["fixed"]) == 1 and len(diff["new"]) == 1
    for suffix in (".md", ".html", ".docx", ".json"):
        path, json_path, _ = write_artifacts(tmp_path / f"report{suffix}", "# Отчёт\n", current)
        assert path.exists() and json_path.exists()


def test_eval_matcher_synthetic() -> None:
    report = {
        "requirement_checks": [{"findings": [{
            "criterion": "consistent", "quote": "Правило A", "related_quotes": ["Правило B"],
            "quote_verified": True,
        }]}],
        "set_check": {"findings": []},
    }
    expected = [{"id": "E-1", "criterion": "consistent", "anchor_quotes": ["Правило A", "Правило B"]}]
    result = evaluate(report, expected)
    assert result["recall"] == 1.0
    assert result["lenient_recall"] == 1.0
    assert result["precision_proxy"] == 1.0


def test_eval_matcher_lenient_accepts_related_criterion_and_one_anchor() -> None:
    report = {
        "requirement_checks": [{"findings": [{
            "criterion": "correct", "quote": "Первое конфликтующее правило",
            "related_quotes": [], "quote_verified": True,
        }]}],
        "set_check": {"findings": []},
    }
    expected = [{
        "id": "E-contradiction", "category": "consistent",
        "anchor_quotes": ["Первое конфликтующее правило", "Второе правило"],
    }]

    result = evaluate(report, expected)

    assert result["recall"] == 0.0
    assert result["lenient_recall"] == 1.0
    assert result["rows"][0]["hit"] is False
    assert result["rows"][0]["lenient_hit"] is True
    rendered = render_results(result)
    assert "Strict recall: 0.0%" in rendered
    assert "Lenient recall: 100.0%" in rendered


def test_pdf_table_rows_survive(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    path = tmp_path / "table.pdf"
    pdf = UnicodePdfWriter(fitz)
    page = pdf.new_page()
    pdf.insert_text(page, (50, 45), "Требования", fontsize=16)
    x0, y0, cell_w, cell_h = 50, 80, 180, 30
    for row in range(3):
        page.draw_line((x0, y0 + row * cell_h), (x0 + 2 * cell_w, y0 + row * cell_h))
    for col in range(3):
        page.draw_line((x0 + col * cell_w, y0), (x0 + col * cell_w, y0 + 2 * cell_h))
    pdf.insert_text(page, (x0 + 8, y0 + 20), "Field", fontsize=11)
    pdf.insert_text(page, (x0 + cell_w + 8, y0 + 20), "Rule", fontsize=11)
    pdf.insert_text(page, (x0 + 8, y0 + cell_h + 20), "amount", fontsize=11)
    pdf.insert_text(page, (x0 + cell_w + 8, y0 + cell_h + 20), "required", fontsize=11)
    pdf.save(path)
    document = load_document(path)
    assert "| Field | Rule |" in document.text
    assert "| amount | required |" in document.text
