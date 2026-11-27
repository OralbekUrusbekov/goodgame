from __future__ import annotations

from .llm import LLMProvider
from .loader import Document
from .models import Finding, Requirement, SetCheck
from .placeholders import find_placeholder_findings
from .prompts import SET_SCHEMA, completeness_prompt, consistency_prompt
from .verifier import dedupe_findings
from .verifier import normalize
import re


SET_CRITERIA = (
    "complete", "consistent", "feasible", "comprehensible", "able_to_be_validated",
    "ranked_importance_stability", "modifiable", "traceable",
)


def _terms(requirement: Requirement) -> set[str]:
    value = normalize(f"{requirement.source_id} {requirement.section} {requirement.short_text} {requirement.quote}")
    stop = {"требование", "система", "должен", "должна", "должно", "если", "при", "для", "или", "and", "the"}
    return {word for word in re.findall(r"[\w/-]+", value) if len(word) >= 3 and word not in stop}


def build_traceability_matrix(requirements: list[Requirement]) -> list[dict]:
    """Build stable intradocument goal→requirement→acceptance→API links."""
    acceptance = [item for item in requirements if re.search(
        r"критери\w*\s+при[её]м|acceptance|given\b|when\b|then\b", normalize(f"{item.section} {item.quote}")
    )]
    api = [item for item in requirements if re.search(
        r"\bapi\b|\bhttp\b|\brest\b|endpoint|request|response|/[-\w/{}/]+", normalize(f"{item.section} {item.quote}")
    )]
    goals = [item for item in requirements if re.search(r"\bцель\w*\b|\bgoal\w*\b", normalize(item.section))]
    excluded = {item.report_id for item in [*goals, *acceptance, *api]}
    product = [item for item in requirements if item.report_id not in excluded]
    if not product:
        product = list(requirements)

    def linked(source: Requirement, candidates: list[Requirement]) -> list[str]:
        source_terms = _terms(source)
        result = []
        for candidate in candidates:
            shared_id = source.source_id != "—" and source.source_id == candidate.source_id
            overlap = source_terms & _terms(candidate)
            if shared_id or len(overlap) >= 2 or (overlap and len(source_terms) <= 3):
                result.append(candidate.report_id)
        return result

    rows = []
    for requirement in product:
        acceptance_ids = linked(requirement, acceptance)
        api_ids = linked(requirement, api)
        linked_goals = linked(requirement, goals)
        goal = ", ".join(linked_goals) or requirement.section
        status = "covered" if acceptance_ids and api_ids else "partial" if acceptance_ids or api_ids else "missing"
        rows.append({
            "goal": goal,
            "goal_quote": next((item.quote for item in goals if item.report_id in linked_goals), ""),
            "requirement_ids": [requirement.report_id],
            "acceptance_ids": acceptance_ids,
            "api_ids": api_ids,
            "status": status,
        })
    return rows


def check_set(
    document: Document, requirements: list[Requirement], provider: LLMProvider,
    domain_check: bool = True,
) -> SetCheck:
    responses = []
    for operation, prompt in (
        ("set_consistency", consistency_prompt),
        ("set_completeness", lambda doc, reqs: completeness_prompt(doc, reqs, domain_check)),
    ):
        system, user = prompt(document, requirements)
        responses.append(
            provider.generate_json(system, user, schema=SET_SCHEMA, operation=operation)
        )
    verdicts = {}
    for criterion in SET_CRITERIA:
        values = [response.get("verdicts", {}).get(criterion, "n.a.") for response in responses]
        verdicts[criterion] = "violation" if "violation" in values else ("ok" if "ok" in values else "n.a.")
    llm_findings = [
        Finding.from_dict(item)
        for response in responses
        for item in response.get("findings", [])
        if isinstance(item, dict)
    ]
    deterministic_findings = find_placeholder_findings(document)
    findings = dedupe_findings([*deterministic_findings, *llm_findings])
    if deterministic_findings:
        verdicts["complete"] = "violation"
    missing_sections = list(dict.fromkeys(
        str(item) for response in responses for item in response.get("missing_sections", [])
    ))
    traceability = " ".join(
        str(response.get("traceability", "")).strip() for response in responses
        if str(response.get("traceability", "")).strip()
    )
    matrix = build_traceability_matrix(requirements)
    seen_rows = {(str(row.get("goal", "")), tuple(row.get("requirement_ids", []))) for row in matrix}
    for response in responses:
        for row in response.get("traceability_matrix", []):
            if not isinstance(row, dict):
                continue
            key = (str(row.get("goal", "")), tuple(row.get("requirement_ids", [])))
            if key not in seen_rows:
                seen_rows.add(key)
                matrix.append(row)
    return SetCheck(
        verdicts=verdicts,
        findings=findings,
        missing_sections=missing_sections,
        traceability=traceability,
        traceability_matrix=matrix,
    )
