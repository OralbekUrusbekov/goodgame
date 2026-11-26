from __future__ import annotations

from .llm import LLMProvider
from .loader import Document
from .models import Finding, Requirement, SetCheck
from .placeholders import find_placeholder_findings
from .prompts import SET_SCHEMA, completeness_prompt, consistency_prompt
from .verifier import dedupe_findings


SET_CRITERIA = (
    "complete", "consistent", "feasible", "comprehensible", "able_to_be_validated",
    "ranked_importance_stability", "modifiable", "traceable",
)


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
    matrix = []
    seen_rows = set()
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
