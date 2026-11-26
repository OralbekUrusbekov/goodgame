from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CRITERIA = (
    "necessary", "appropriate", "unambiguous", "complete", "singular",
    "feasible", "verifiable", "correct", "conforming",
)


@dataclass
class Requirement:
    report_id: str
    source_id: str
    section: str
    short_text: str
    quote: str
    quote_verified: bool = True
    line_number: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int) -> "Requirement":
        return cls(
            report_id=f"R-{index:02d}",
            source_id=str(data.get("source_id") or "—"),
            section=str(data.get("section") or "Раздел не определён"),
            short_text=str(data.get("short_text") or data.get("quote") or "")[:240],
            quote=str(data.get("quote") or "").strip(),
        )


@dataclass
class Finding:
    criterion: str
    severity: str
    explanation: str
    fix: str
    quote: str
    location: str
    requirement_ids: list[str] = field(default_factory=list)
    related_quotes: list[str] = field(default_factory=list)
    quote_verified: bool = True
    confidence: str | None = None
    expert_confirmation: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_id: str | None = None) -> "Finding":
        ids = data.get("requirement_ids") or ([default_id] if default_id else [])
        quote = str(data.get("quote") or "").strip()
        related = [str(q).strip() for q in data.get("related_quotes", []) if str(q).strip()]
        return cls(
            criterion=str(data.get("criterion") or "complete"),
            severity=str(data.get("severity") or "major").lower(),
            explanation=str(data.get("explanation") or "Недостаток требует уточнения."),
            fix=str(data.get("fix") or "Уточнить формулировку требования."),
            quote=quote,
            location=str(data.get("location") or "Раздел не определён"),
            requirement_ids=[str(item) for item in ids if item],
            related_quotes=related,
            confidence=str(data.get("confidence")) if data.get("confidence") else None,
            expert_confirmation=bool(data.get("expert_confirmation", False)),
        )


@dataclass
class RequirementCheck:
    requirement_id: str
    verdicts: dict[str, str]
    findings: list[Finding]
    rewrite: str = ""


@dataclass
class SetCheck:
    verdicts: dict[str, str]
    findings: list[Finding]
    missing_sections: list[str] = field(default_factory=list)
    traceability: str = ""
    traceability_matrix: list[dict[str, Any]] = field(default_factory=list)
