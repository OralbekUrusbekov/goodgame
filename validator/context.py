from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

from .llm import LLMProvider
from .loader import Document, load_document
from .models import Requirement
from .verifier import quote_exists


SUPPORTED = {".md", ".txt", ".pdf", ".docx"}
CONTEXT_CATEGORIES = ("goals", "scope_in", "scope_out", "constraints", "glossary", "systems", "policies")


@dataclass
class ContextBundle:
    documents: list[Document]
    extracted: dict[str, list[dict[str, Any]]]


def resolve_context_paths(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            result.extend(sorted(item for item in path.rglob("*") if item.suffix.lower() in SUPPORTED))
        elif path.is_file() and path.suffix.lower() in SUPPORTED:
            result.append(path)
        else:
            raise ValueError(f"Контекст не найден или формат не поддерживается: {path}")
    return list(dict.fromkeys(result))


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"}, "quote": {"type": "string"},
        "source_file": {"type": "string"},
    },
    "required": ["text", "quote", "source_file"],
}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {key: {"type": "array", "items": ITEM_SCHEMA} for key in CONTEXT_CATEGORIES},
    "required": list(CONTEXT_CATEGORIES),
}


def extract_context(paths: list[str], provider: LLMProvider) -> ContextBundle:
    documents = [load_document(path) for path in resolve_context_paths(paths)]
    payload = [{"source_file": document.path.name, "text": document.text} for document in documents]
    system = """Извлеки проектный контекст для проверки требований: цели, границы scope in/out,
ограничения (включая SLA, технологии и сроки), термины глоссария, системы и политики.
Каждый элемент содержит краткий text, точную verbatim quote до 200 символов и source_file.
Не делай выводов и не добавляй сведения. Верни только JSON по схеме."""
    response = provider.generate_json(
        system, "ОПЕРАЦИЯ: CONTEXT_EXTRACT\nДОКУМЕНТЫ:\n" + json.dumps(payload, ensure_ascii=False),
        schema=EXTRACT_SCHEMA, operation="context_extract",
    )
    by_name = {document.path.name: document for document in documents}
    extracted: dict[str, list[dict[str, Any]]] = {}
    for category in CONTEXT_CATEGORIES:
        extracted[category] = []
        for item in response.get(category, []):
            if not isinstance(item, dict):
                continue
            source_file = Path(str(item.get("source_file", ""))).name
            document = by_name.get(source_file)
            quote = str(item.get("quote", ""))
            extracted[category].append({
                "text": str(item.get("text", "")), "quote": quote, "source_file": source_file,
                "line": document.line_for(quote) if document else None,
                "quote_verified": bool(document and quote_exists(quote, document.text)),
            })
    return ContextBundle(documents, extracted)


CONTEXT_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [
            "goal_not_covered", "unjustified_requirement", "scope_conflict", "constraint_conflict",
            "term_conflict", "system_conflict", "policy_conflict",
        ]},
        "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
        "explanation": {"type": "string"}, "fix": {"type": "string"},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
        "requirement_quote": {"type": "string"}, "context_quote": {"type": "string"},
        "context_file": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["type", "severity", "explanation", "fix", "requirement_ids",
                 "requirement_quote", "context_quote", "context_file", "confidence"],
}
TRACE_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"}, "goal_quote": {"type": "string"},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
        "acceptance_ids": {"type": "array", "items": {"type": "string"}},
        "api_ids": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["covered", "partial", "missing"]},
    },
    "required": ["goal", "goal_quote", "requirement_ids", "acceptance_ids", "api_ids", "status"],
}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": CONTEXT_FINDING_SCHEMA},
        "traceability_matrix": {"type": "array", "items": TRACE_ROW_SCHEMA},
    },
    "required": ["findings", "traceability_matrix"],
}


def check_context(
    bundle: ContextBundle, requirements: list[Requirement], provider: LLMProvider,
    requirements_document: Document | None = None,
) -> dict[str, Any]:
    system = """Сравни каталог требований с извлечённым проектным контекстом и найди только доказуемые
несоответствия: непокрытые цели, нетрассируемые требования, конфликты scope, ограничений, терминов,
систем и политик. Каждый finding обязан содержать две дословные цитаты: из требования и из контекста,
а также файл контекста. Построй матрицу цели → требования → критерии приёмки → элементы API.
Не используй внешние знания. Верни только JSON по схеме."""
    payload = {
        "context": bundle.extracted,
        "requirements": [vars(requirement) for requirement in requirements],
    }
    response = provider.generate_json(
        system, "ОПЕРАЦИЯ: CONTEXT_CHECK\nДАННЫЕ:\n" + json.dumps(payload, ensure_ascii=False),
        schema=CHECK_SCHEMA, operation="context_check",
    )
    docs = {document.path.name: document for document in bundle.documents}
    requirements_text = (
        requirements_document.text if requirements_document
        else "\n".join(requirement.quote for requirement in requirements)
    )
    findings = []
    for item in response.get("findings", []):
        if not isinstance(item, dict):
            continue
        context_file = Path(str(item.get("context_file", ""))).name
        context_document = docs.get(context_file)
        requirement_quote = str(item.get("requirement_quote", ""))
        context_quote = str(item.get("context_quote", ""))
        findings.append({
            **item,
            "context_file": context_file,
            "requirement_quote_verified": quote_exists(requirement_quote, requirements_text),
            "requirement_line": requirements_document.line_for(requirement_quote) if requirements_document else None,
            "context_quote_verified": bool(context_document and quote_exists(context_quote, context_document.text)),
            "context_line": context_document.line_for(context_quote) if context_document else None,
        })
    return {"findings": findings, "traceability_matrix": response.get("traceability_matrix", [])}
