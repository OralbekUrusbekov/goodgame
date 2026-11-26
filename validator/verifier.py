from __future__ import annotations

from difflib import SequenceMatcher
import re

from .loader import Document
from .models import Finding


def normalize(value: str) -> str:
    value = value.replace("ё", "е").casefold()
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def quote_exists(quote: str, source: str, threshold: float = 0.88) -> bool:
    needle, haystack = normalize(quote), normalize(source)
    if not needle:
        return False
    if needle in haystack:
        return True
    words = needle.split()
    source_words = haystack.split()
    if len(words) < 3 or len(source_words) < len(words):
        return False
    window_sizes = {max(1, len(words) - 2), len(words), len(words) + 2}
    for size in window_sizes:
        for start in range(0, max(0, len(source_words) - size + 1)):
            candidate = " ".join(source_words[start : start + size])
            if SequenceMatcher(None, needle, candidate).ratio() >= threshold:
                return True
    return False


def exact_quote(quote: str, source: str, threshold: float = 0.88) -> str | None:
    """Return a literal source fragment corresponding to a normalized/fuzzy quote.

    LLMs often flatten a Markdown table cell pair such as ``REST type: GET``.
    Verification can accept that normalized form, but consumers need the literal
    source text for highlighting and location lookup. Prefer one complete source
    line; fall back to a small consecutive-line window for wrapped text.
    """
    if not quote or not source:
        return None
    if quote in source:
        return quote
    folded_index = source.casefold().find(quote.casefold())
    if folded_index >= 0:
        return source[folded_index : folded_index + len(quote)]

    lines = source.splitlines()
    for width in (1, 2, 3):
        for start in range(0, len(lines) - width + 1):
            candidate = "\n".join(lines[start : start + width]).strip()
            if candidate and quote_exists(quote, candidate, threshold=threshold):
                return candidate
    return None


def verify_findings(document: Document, findings: list[Finding]) -> list[Finding]:
    unique: list[Finding] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for finding in findings:
        all_quotes = [finding.quote, *finding.related_quotes]
        anchored = [exact_quote(item, document.text) for item in all_quotes]
        finding.quote_verified = bool(all_quotes) and all(anchored)
        if anchored and anchored[0]:
            finding.quote = anchored[0]
        finding.related_quotes = [
            literal or original
            for original, literal in zip(finding.related_quotes, anchored[1:])
        ]
        if not finding.quote_verified and "цитата не подтверждена" not in finding.explanation:
            finding.explanation += " [цитата не подтверждена]"
        if finding.quote:
            finding.location = document.location_for(finding.quote)
        key = (finding.criterion, normalize(finding.quote), normalize(finding.explanation), tuple(sorted(finding.requirement_ids)))
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def findings_match(left: Finding, right: Finding) -> bool:
    if left.criterion != right.criterion:
        return False
    left_quotes = [left.quote, *left.related_quotes]
    right_quotes = [right.quote, *right.related_quotes]
    return any(
        quote_exists(first, second, threshold=0.82) or quote_exists(second, first, threshold=0.82)
        for first in left_quotes for second in right_quotes if first and second
    )


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    result: list[Finding] = []
    for finding in findings:
        duplicate = next((item for item in result if findings_match(item, finding)), None)
        if duplicate is None:
            result.append(finding)
            continue
        duplicate.requirement_ids = list(dict.fromkeys([
            *duplicate.requirement_ids, *finding.requirement_ids
        ]))
        duplicate.related_quotes = list(dict.fromkeys([
            *duplicate.related_quotes, finding.quote, *finding.related_quotes
        ]))
        if {"critical": 3, "major": 2, "minor": 1}.get(finding.severity, 0) > {
            "critical": 3, "major": 2, "minor": 1
        }.get(duplicate.severity, 0):
            duplicate.severity = finding.severity
    return result
