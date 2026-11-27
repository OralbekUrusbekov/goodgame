from __future__ import annotations

from difflib import SequenceMatcher
import re

from .loader import Document
from .models import Finding


SEVERITY_RANK = {"critical": 3, "major": 2, "minor": 1}
_STOP_WORDS = {
    "и", "или", "но", "а", "в", "во", "на", "по", "для", "из", "к", "ко", "с", "со",
    "не", "нет", "что", "это", "как", "при", "если", "то", "же", "быть", "есть", "у",
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "with",
}


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


def exact_quote_fragments(quote: str, source: str, threshold: float = 0.88) -> list[str]:
    """Return literal fragments when a model flattened adjacent rows or cells.

    ``exact_quote`` remains the compatibility API for callers that need one
    highlight.  Verification uses this function so a flattened quote becomes a
    primary literal quote plus literal ``related_quotes`` instead of one broad
    multi-line approximation.
    """
    if not quote or not source:
        return []
    direct = exact_quote(quote, source, threshold)
    if direct and "\n" not in direct and not ("|" in direct and "|" not in quote):
        return [direct]
    if direct and "|" in direct and "|" not in quote:
        cells = [cell.strip() for cell in direct.strip(" |").split("|") if cell.strip()]
        quote_tokens = set(normalize(quote).split())
        matched_cells = [
            cell for cell in cells
            if set(normalize(cell).split()) <= quote_tokens
        ]
        if len(matched_cells) >= 2:
            return matched_cells

    parts = [item.strip(" \t|:;,") for item in re.split(r"\r?\n|\s*\|\s*", quote) if item.strip(" \t|:;,")]
    anchored: list[str] = []
    for part in parts:
        literal = exact_quote(part, source, threshold)
        if literal and literal not in anchored:
            anchored.append(literal)
    if len(anchored) >= 2:
        return anchored

    # A common flattening has no separators ("label value") while the source
    # contains Markdown cells or consecutive lines.  Select nearby source lines
    # whose meaningful tokens are represented in the model quote.
    quote_tokens = set(normalize(quote).split())
    candidates: list[tuple[int, str, set[str]]] = []
    for index, raw in enumerate(source.splitlines()):
        literal = raw.strip()
        tokens = set(normalize(literal).split())
        meaningful = {token for token in tokens if len(token) > 1 and token not in _STOP_WORDS}
        if literal and meaningful and meaningful <= quote_tokens:
            candidates.append((index, literal, meaningful))
    for start, first, first_tokens in candidates:
        group = [first]
        covered = set(first_tokens)
        previous = start
        for index, literal, tokens in candidates:
            if index <= start or index - previous > 2:
                continue
            group.append(literal)
            covered.update(tokens)
            previous = index
            if len(group) >= 2 and len(covered) / max(1, len(quote_tokens - _STOP_WORDS)) >= 0.75:
                return list(dict.fromkeys(group))
    return [direct] if direct else []


def verify_findings(document: Document, findings: list[Finding]) -> list[Finding]:
    unique: list[Finding] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for finding in findings:
        all_quotes = [finding.quote, *finding.related_quotes]
        anchored_groups = [exact_quote_fragments(item, document.text) for item in all_quotes]
        finding.quote_verified = bool(all_quotes) and all(anchored_groups)
        finding.unverified = not finding.quote_verified
        literals = list(dict.fromkeys(fragment for group in anchored_groups for fragment in group))
        if literals:
            finding.quote = literals[0]
            finding.related_quotes = literals[1:]
        if not finding.quote_verified and "цитата не подтверждена" not in finding.explanation:
            finding.explanation += " [цитата не подтверждена]"
        if finding.quote:
            finding.location = document.location_for(finding.quote, context=" ".join(literals))
        finding.criteria = list(dict.fromkeys([finding.criterion, *finding.criteria]))
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


def _meaningful_words(value: str) -> set[str]:
    return {
        word for word in normalize(value).split()
        if len(word) >= 3 and word not in _STOP_WORDS and not word.isdigit()
    }


def _shared_verified_quote(left: Finding, right: Finding) -> bool:
    if not left.quote_verified or not right.quote_verified:
        return False
    return bool(
        {normalize(item) for item in [left.quote, *left.related_quotes] if item}
        & {normalize(item) for item in [right.quote, *right.related_quotes] if item}
    )


def findings_are_clusterable(left: Finding, right: Finding, threshold: float = 0.45) -> bool:
    """Whether findings describe the same defect supported by the same evidence."""
    if not _shared_verified_quote(left, right):
        return False
    left_words, right_words = _meaningful_words(left.explanation), _meaningful_words(right.explanation)
    if not left_words or not right_words:
        return False
    return len(left_words & right_words) / min(len(left_words), len(right_words)) >= threshold


def cluster_findings(findings: list[Finding], threshold: float = 0.45) -> list[Finding]:
    """Deterministically merge same-evidence, same-meaning verified findings."""
    result: list[Finding] = []
    for finding in findings:
        match = next((item for item in result if findings_are_clusterable(item, finding, threshold)), None)
        if match is None:
            finding.criteria = list(dict.fromkeys([finding.criterion, *finding.criteria]))
            result.append(finding)
            continue
        candidates = [match, finding]
        primary = sorted(
            candidates,
            key=lambda item: (-SEVERITY_RANK.get(item.severity, 0), item.criterion, item.explanation),
        )[0]
        if primary is finding:
            index = result.index(match)
            result[index] = finding
            match = finding
        match.severity = max(candidates, key=lambda item: SEVERITY_RANK.get(item.severity, 0)).severity
        match.criteria = sorted(set(
            criterion for item in candidates for criterion in [item.criterion, *item.criteria]
        ))
        match.requirement_ids = list(dict.fromkeys(
            requirement_id for item in candidates for requirement_id in item.requirement_ids
        ))
        match.related_quotes = list(dict.fromkeys(
            quote for item in candidates for quote in [item.quote, *item.related_quotes]
            if quote and quote != match.quote
        ))
        match.fix_kind = "decision" if any(item.fix_kind == "decision" for item in candidates) else "edit"
    return result


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
        duplicate.criteria = sorted(set([duplicate.criterion, *duplicate.criteria, finding.criterion, *finding.criteria]))
        if SEVERITY_RANK.get(finding.severity, 0) > SEVERITY_RANK.get(duplicate.severity, 0):
            duplicate.severity = finding.severity
    return result
