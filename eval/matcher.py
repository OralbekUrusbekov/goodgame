from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from validator.verifier import normalize, quote_exists


CRITERION_GROUPS = {
    "contradiction_correctness": {
        "consistent", "correct", "appropriate", "unambiguous", "singular", "conforming",
    },
    "completeness": {"complete", "set_complete", "traceable"},
    "verifiability": {"verifiable", "unambiguous"},
}


def _criterion_name(value: str) -> str:
    return str(value or "").strip().casefold().rsplit(".", 1)[-1]


def _same_criterion_group(left: str, right: str) -> bool:
    left_name, right_name = _criterion_name(left), _criterion_name(right)
    if left_name == right_name:
        return True
    return any(left_name in members and right_name in members for members in CRITERION_GROUPS.values())


def _quote_matches(anchor: str, quote: str) -> bool:
    return quote_exists(anchor, quote, threshold=0.78) or quote_exists(quote, anchor, threshold=0.78)


def _lenient_quote_matches(anchor: str, quote: str) -> bool:
    if _quote_matches(anchor, quote):
        return True
    anchor_words, quote_words = normalize(anchor).split(), normalize(quote).split()
    if not anchor_words or len(anchor_words) > len(quote_words):
        return False
    for start in range(len(quote_words) - len(anchor_words) + 1):
        window = quote_words[start : start + len(anchor_words)]
        if all(
            left == right or (
                min(len(left), len(right)) >= 5
                and SequenceMatcher(None, left, right).ratio() >= 0.72
            )
            for left, right in zip(anchor_words, window)
        ):
            return True
    return False


def _lenient_match_label(finding: dict[str, Any] | None) -> str:
    if finding is None:
        return ""
    criterion = str(finding.get("criterion") or "—")
    explanation = " ".join(str(finding.get("explanation") or "").split())[:80]
    return f"{criterion} — {explanation}"


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8")
    try:
        import yaml
        value = yaml.safe_load(text)
    except ImportError:
        value = json.loads(text)
    return value.get("cases", value) if isinstance(value, dict) else value


def report_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings = [
        item for check in report.get("requirement_checks", [])
        for item in check.get("findings", []) if isinstance(item, dict)
    ]
    findings.extend(
        item for item in report.get("set_check", {}).get("findings", []) if isinstance(item, dict)
    )
    for item in report.get("context", {}).get("findings", []):
        if isinstance(item, dict):
            findings.append({
                **item, "criterion": item.get("type", "context"),
                "quote": item.get("requirement_quote", ""),
                "related_quotes": [item.get("context_quote", "")],
                "quote_verified": bool(item.get("requirement_quote_verified") and item.get("context_quote_verified")),
            })
    return findings


def evaluate(report: dict[str, Any], cases: list[dict[str, Any]]) -> dict[str, Any]:
    findings = report_findings(report)
    requirements = [item for item in report.get("requirements", []) if isinstance(item, dict)]
    requirements_by_id = {
        str(item.get("report_id")): item for item in requirements if item.get("report_id")
    }
    source_quotes: dict[str, list[str]] = {}
    for item in requirements:
        source_id, quote = str(item.get("source_id") or ""), str(item.get("quote") or "")
        if source_id and source_id != "—" and quote:
            source_quotes.setdefault(source_id, []).append(quote)
    lenient_findings = list(findings)
    traceability = str(report.get("set_check", {}).get("traceability") or "").strip()
    traceability_failed = report.get("set_check", {}).get("verdicts", {}).get("traceable") == "violation"
    if traceability and traceability_failed:
        lenient_findings.append({
            "criterion": "traceable", "explanation": traceability,
            "quote": traceability, "related_quotes": [], "requirement_ids": [],
        })
    rows = []
    for case in cases:
        anchors = case.get("anchor_quotes", [])
        categories = case.get("categories") or [
            case.get("category") or case.get("criterion") or case.get("criterion_category", "")
        ]
        matched = None
        for finding in findings:
            quotes = [finding.get("quote", ""), *finding.get("related_quotes", [])]
            strict_criterion = not categories or finding.get("criterion") in categories
            if strict_criterion and all(
                any(_quote_matches(anchor, quote) for quote in quotes if quote)
                for anchor in anchors
            ):
                matched = finding
                break

        lenient_matched = None
        lenient_score: tuple = ()
        for finding in lenient_findings:
            quotes = [finding.get("quote", ""), *finding.get("related_quotes", [])]
            lenient_criterion = not categories or any(
                _same_criterion_group(finding.get("criterion", ""), category)
                for category in categories
            )
            linked_quotes: list[str] = []
            for requirement_id in finding.get("requirement_ids", []):
                requirement = requirements_by_id.get(str(requirement_id))
                if not requirement:
                    continue
                quote = str(requirement.get("quote") or "")
                if quote:
                    linked_quotes.append(quote)
                source_id = str(requirement.get("source_id") or "")
                linked_quotes.extend(source_quotes.get(source_id, []))
            lenient_quotes = [*quotes, *linked_quotes]
            if not lenient_criterion:
                continue
            direct = sum(any(_lenient_quote_matches(a, q) for q in quotes if q) for a in anchors)
            linked = sum(any(_lenient_quote_matches(a, q) for q in lenient_quotes if q) for a in anchors)
            if anchors and not linked:
                continue
            exact = _criterion_name(finding.get("criterion", "")) in {_criterion_name(c) for c in categories}
            score = (direct, exact, linked)
            # Показываем самое сильное свидетельство, а не первое попавшееся.
            if lenient_matched is None or score > lenient_score:
                lenient_matched, lenient_score = finding, score
        rows.append({
            "id": case.get("id"), "description": case.get("description"),
            "hit": matched is not None, "strict_hit": matched is not None,
            "lenient_hit": lenient_matched is not None,
            "lenient_match": _lenient_match_label(lenient_matched),
        })
    hits = sum(row["hit"] for row in rows)
    lenient_hits = sum(row["lenient_hit"] for row in rows)
    verified = sum(bool(item.get("quote_verified")) for item in findings)
    return {
        "recall": hits / len(rows) if rows else 1.0,
        "strict_recall": hits / len(rows) if rows else 1.0,
        "lenient_recall": lenient_hits / len(rows) if rows else 1.0,
        "precision_proxy": verified / len(findings) if findings else 1.0,
        "hits": hits, "strict_hits": hits, "lenient_hits": lenient_hits,
        "total": len(rows), "rows": rows,
    }


def render_results(result: dict[str, Any]) -> str:
    lines = ["# Результаты eval", "", f"- Strict recall: {result['strict_recall']:.1%}",
             f"- Lenient recall: {result['lenient_recall']:.1%}",
             f"- Precision proxy: {result['precision_proxy']:.1%}", "",
             "| ID | Ожидаемый дефект | Strict | Lenient | Lenient-находка |",
             "|---|---|---|---|---|"]
    lines.extend(
        f"| {row['id']} | {str(row['description']).replace('|', '/')} | "
        f"{'HIT' if row['strict_hit'] else 'MISS'} | {'HIT' if row['lenient_hit'] else 'MISS'} | "
        f"{row.get('lenient_match', '').replace('|', '/')} |"
        for row in result["rows"]
    )
    return "\n".join(lines) + "\n"
