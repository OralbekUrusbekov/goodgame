from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from validator import contract
from validator.contract import run_pipeline, to_contract
from validator.criteria import BY_CODE


def _payload_with_table_quote() -> dict:
    return {
        "metadata": {
            "title": "API",
            "run_at": "2026-11-26T12:00:00+05:00",
            "provider": "mock",
            "model": "mock-deterministic",
            "agent_version": "test",
        },
        "requirements": [
            {
                "report_id": "R-01",
                "source_id": "—",
                "section": "Интерфейс",
                "short_text": "Метод интерфейса",
                "quote": "| **REST type** | GET |",
                "quote_verified": True,
                "line_number": 4,
            }
        ],
        "requirement_checks": [
            {
                "requirement_id": "R-01",
                "findings": [
                    {
                        "criterion": "complete",
                        "severity": "major",
                        "quote": "REST type: GET",
                        "location": "Раздел не определён",
                        "explanation": "Не описан ответ.",
                        "fix": "Описать ответ.",
                        "quote_verified": True,
                    }
                ],
            }
        ],
        "set_check": {"findings": []},
        "summary": {"decision": "С оговорками", "top_fixes": []},
    }


def test_fuzzy_table_quote_is_replaced_with_exact_document_fragment() -> None:
    text = "# API\n\n## Интерфейс\n| **REST type** | GET |\n"

    result = to_contract(
        _payload_with_table_quote(), "# Отчёт\n", text, "api.md", {}, doc_format="md"
    )

    finding = result["findings"][0]
    assert finding["quote"] == "| **REST type** | GET |"
    assert finding["quote"] in text
    assert finding["location"] == "Интерфейс, строка 4"


def _contract_payload(decision: str = "Нет") -> dict:
    payload = _payload_with_table_quote()
    payload["summary"]["decision"] = decision
    payload["requirement_checks"][0]["findings"] = [
        {
            "criterion": "complete",
            "severity": "major",
            "quote": "REST type: GET",
            "location": "Раздел не определён",
            "explanation": "Не описан ответ.",
            "fix": "Описать ответ.",
            "quote_verified": True,
        },
        {
            "criterion": "verifiable",
            "severity": "critical",
            "quote": "REST type: GET",
            "location": "Интерфейс",
            "explanation": "Нет измеримого результата.",
            "fix": "Описать ответ.",
            "quote_verified": True,
        },
        {
            "criterion": "conforming",
            "severity": "minor",
            "quote": "REST type: GET",
            "location": "Интерфейс",
            "explanation": "Нарушена форма.",
            "fix": "",
            "quote_verified": True,
        },
    ]
    payload["set_check"] = {
        "findings": [
            {
                "criterion": "consistent",
                "severity": "major",
                "quote": "REST type: GET",
                "location": "Интерфейс",
                "explanation": "Набор требует уточнения.",
                "fix": "Описать ответ.",
                "quote_verified": True,
            }
        ]
    }
    return payload


@pytest.mark.parametrize(
    ("decision", "status"),
    [("Нет", "not_ready"), ("С оговорками", "ready_with_notes"), ("Да", "ready")],
)
def test_payload_is_mapped_to_complete_web_contract(decision: str, status: str) -> None:
    text = "# API\n\n## Интерфейс\n| **REST type** | GET |\n"
    result = to_contract(_contract_payload(decision), "# Отчёт\n", text, "api.md", {})

    assert result["verdict"]["status"] == status
    assert {
        "contract_version", "passport", "document", "requirements", "findings",
        "questions", "summary", "verdict", "warnings", "partial", "report_markdown",
    } <= result.keys()
    assert {"document_title", "file_name", "date", "model", "agent_version", "criteria"} <= result["passport"].keys()
    assert {"text", "format"} <= result["document"].keys()
    assert all(finding["criterion"] in BY_CODE for finding in result["findings"])
    assert [finding["severity"] for finding in result["findings"]] == [
        "critical", "major", "major", "minor"
    ]
    assert [finding["id"] for finding in result["findings"]] == [
        "F-01", "F-02", "F-03", "F-04"
    ]
    assert result["summary"]["by_severity"] == {
        severity: sum(f["severity"] == severity for f in result["findings"])
        for severity in ("critical", "major", "minor")
    }
    assert len({q["text"].casefold() for q in result["questions"]}) == len(result["questions"])
    assert all(q["text"].strip() for q in result["questions"])
    assert result["questions"][0]["finding_ids"] == ["F-01", "F-02", "F-03"]
    assert not any("Нарушена форма" in q["text"] for q in result["questions"])


def test_progress_stages_are_ordered_and_cancellation_propagates(tmp_path: Path) -> None:
    source = tmp_path / "r.md"
    source.write_text("# Сервис\n\n* Система сохраняет настройку.\n", encoding="utf-8")
    events: list[tuple[str, dict]] = []

    run_pipeline(str(source), on_progress=lambda stage, info: events.append((stage, info)))

    stages = [stage for stage, _ in events]
    expected = [
        "reading", "splitting", "checking_requirements", "checking_set",
        "checking_gitlab", "verifying_quotes", "summary",
    ]
    assert [stage for stage in expected if stage in stages] == expected
    progress = [info for stage, info in events if stage == "checking_requirements"]
    assert progress and all({"done", "total"} <= info.keys() for info in progress)
    assert next(info for stage, info in events if stage == "checking_gitlab")["skipped"] is True

    class Cancelled(RuntimeError):
        pass

    def cancel(stage: str, _: dict) -> None:
        if stage == "splitting":
            raise Cancelled("stop")

    with pytest.raises(Cancelled, match="stop"):
        run_pipeline(str(source), on_progress=cancel)


def test_gitlab_context_is_forwarded_and_temporary_directories_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "r.md"
    source.write_text("# API\n\n* Система вызывает /items.\n", encoding="utf-8")
    captured: dict = {}

    class GitLab:
        project = "group/project"
        ref = "main"

        def collect(self):
            return [{"path": "src/api.py", "category": "api", "content": "ROUTE = '/items'"}]

        def web_url(self, path: str, line: int | None = None):
            return f"https://git.example/{path}#L{line}"

    def fake_agent(doc_path: str, destination: str, settings, **kwargs):
        captured["context_paths"] = kwargs["context_paths"]
        captured["out_dir"] = str(Path(destination).parent)
        context_file = next(Path(kwargs["context_paths"][0]).iterdir()).name
        Path(destination).write_text("# Отчёт\n", encoding="utf-8")
        payload = {
            "metadata": {
                "title": "API", "run_at": "2026-11-26T12:00:00+05:00",
                "provider": "mock", "model": "mock-deterministic", "agent_version": "test",
                "context_used": True,
            },
            "requirements": [], "requirement_checks": [], "set_check": {"findings": []},
            "summary": {"decision": "С оговорками", "top_fixes": []},
            "context": {"findings": [{
                "type": "system_conflict", "severity": "major",
                "requirement_quote": "Система вызывает /items.",
                "requirement_quote_verified": True,
                "context_quote": "ROUTE = '/items'", "context_file": context_file,
                "context_line": 1, "explanation": "Контракт расходится.", "fix": "Согласовать контракт.",
            }]},
        }
        return SimpleNamespace(report_path=Path(destination), payload=payload)

    monkeypatch.setattr(contract, "_run_agent", fake_agent)
    result = run_pipeline(str(source), gitlab=GitLab())

    assert captured["context_paths"] and not Path(captured["context_paths"][0]).exists()
    assert not Path(captured["out_dir"]).exists()
    finding = result["findings"][0]
    assert finding["scope"] == "code"
    assert finding["code_ref"]["path"] == "src/api.py"
    assert finding["code_ref"]["url"].endswith("src/api.py#L1")


def test_unverified_findings_are_separate_and_do_not_affect_verdict() -> None:
    payload = _contract_payload("Да")
    payload["requirement_checks"][0]["findings"][1]["quote"] = "цитаты в документе нет"

    result = to_contract(payload, "# Отчёт\n", "# API\n\nREST type: GET\n", "api.md", {})

    assert len(result["unverified_findings"]) == 1
    assert result["passport"]["dropped_findings"] == 1
    assert result["summary"]["by_severity"]["critical"] == 0
    assert result["verdict"]["status"] == "ready"
    assert result["unverified_findings"][0]["id"] not in result["verdict"]["fix_first"]


def test_duplicate_findings_merge_criteria_and_decision_fix_becomes_choice() -> None:
    payload = _payload_with_table_quote()
    base = payload["requirement_checks"][0]["findings"][0]
    base.update({"criterion": "complete", "criteria": ["complete"], "fix_kind": "decision",
                 "explanation": "Не описан ответ метода.", "fix": "выбрать 200 или 201"})
    payload["requirement_checks"][0]["findings"].append({
        **base, "criterion": "verifiable", "criteria": ["verifiable"],
        "explanation": "Не описан ответ метода однозначно.",
    })

    result = to_contract(payload, "# Отчёт\n", "REST type: GET", "api.md", {})

    assert len(result["findings"]) == 1
    assert set(result["findings"][0]["criteria"]) == {"29148.complete", "29148.verifiable"}
    assert result["findings"][0]["question"].startswith("Какой вариант выбирает владелец:")


def test_partial_result_error_becomes_partial_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "partial.md"
    source.write_text("# API\n\nREST type: GET\n", encoding="utf-8")

    class Partial(Exception):
        payload = _payload_with_table_quote()
        report_markdown = "# Частичный отчёт\n"
        stage = "checking_set"
        cause = RuntimeError("квота исчерпана")

    monkeypatch.setattr(contract, "_run_agent", lambda *args, **kwargs: (_ for _ in ()).throw(Partial()))
    result = run_pipeline(str(source))

    assert result["partial"] is True
    assert result["partial_error"] == {"stage": "checking_set", "reason": "квота исчерпана"}
    assert any(warning["code"] == "partial_result" for warning in result["warnings"])


def test_pipeline_forwards_model_and_no_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "settings.md"
    source.write_text("# API\n\nREST type: GET\n", encoding="utf-8")
    captured = {}

    def fake_config(**kwargs):
        captured.update(kwargs)
        return object()

    def fake_agent(doc_path, destination, settings, **kwargs):
        Path(destination).write_text("# Отчёт\n", encoding="utf-8")
        return SimpleNamespace(report_path=Path(destination), payload=_payload_with_table_quote())

    monkeypatch.setattr(contract, "load_config", fake_config)
    monkeypatch.setattr(contract, "_run_agent", fake_agent)
    run_pipeline(str(source), options={"model": "model-a", "no_cache": True})

    assert captured == {"model": "model-a", "cache_enabled": False}
