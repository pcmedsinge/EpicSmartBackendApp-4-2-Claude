"""
Specialty PA Builder — assembles a prior authorization bundle from denial pipeline results.

Phase 4: structured bundle assembly (no FHIR Claim/$submit).
Phase 5: bundle will be submitted via FHIR Claim resource in the orchestrator.

Takes a DenialRiskResult (from Phase 3 denial pipeline) and converts it into
a PABundle with structured evidence, readiness flag, and supporting document checklist.

Entry point: build_pa_bundle(denial_result, hook_request) → PABundle
"""

from __future__ import annotations

import logging

from app.models.cds_hooks import HookRequest
from app.models.domain import DenialRiskResult, EvidenceItem, PABundle

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Payer requirements per drug class — what must be documented in the PA bundle.
# These are human-readable strings shown in the PA bundle and on the card.
# ---------------------------------------------------------------------------
_PAYER_REQUIREMENTS: dict[str, list[str]] = {
    "glp1": [
        "Metformin trial ≥90 days (or documented contraindication)",
        "HbA1c ≥7.0% — lab result within last 6 months",
        "BMI ≥30 (or ≥27 with weight-related comorbidity)",
        "Active Type 2 Diabetes diagnosis (ICD-10 E11.x)",
        "Active insurance coverage confirmed",
    ],
}

# Supporting documents required per drug class for the PA submission packet
_SUPPORTING_DOCUMENTS: dict[str, list[str]] = {
    "glp1": [
        "HbA1c lab report (dated within last 6 months)",
        "BMI measurement (dated within last 12 months)",
        "Medication history documenting metformin trial",
        "Active diagnosis list (ICD-10 codes)",
        "Provider letter of medical necessity",
    ],
}

# Keywords used to match criterion strings to document types
# When a criterion is unmet, we add the relevant document to a "missing docs" note
_CRITERION_DOCUMENT_MAP: dict[str, str] = {
    "Step therapy": "Medication history documenting metformin trial",
    "A1C":          "HbA1c lab report (dated within last 6 months)",
    "BMI":          "BMI measurement (dated within last 12 months)",
    "Clinical criteria": "HbA1c lab report and BMI measurement",
    "Documentation": "All required lab reports and medication history",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_pa_bundle(
    denial_result: DenialRiskResult,
    hook_request: HookRequest,
) -> PABundle:
    """
    Assemble a PA bundle from a DenialRiskResult.

    Args:
        denial_result:  The output of the Clinical-Financial Bridge (Phase 3).
        hook_request:   The original CDS hook request (for patient name from prefetch).

    Returns:
        PABundle — always returns a bundle, even if requirements are unmet.
        ready_to_submit=False with unmet requirements listed gives the clinician
        a clear action plan.

    C# analogy: a pure static factory method — no side effects, same input = same output.
    """
    patient_id = denial_result.patient_id
    patient_name = _extract_patient_name(hook_request)
    drug_class = denial_result.drug_class

    # Convert the denial result's criteria strings into typed EvidenceItems
    clinical_evidence = _build_evidence_items(denial_result)

    # Requirements and readiness
    payer_requirements = _PAYER_REQUIREMENTS.get(drug_class, [])
    ready_to_submit = len(denial_result.unmet_criteria) == 0

    # Supporting documents — base list + any extra noted for unmet criteria
    supporting_documents = _build_supporting_documents(drug_class, denial_result)

    # Appeal notes — only if prior denials exist
    appeal_notes = _build_appeal_notes(denial_result) if not ready_to_submit else None

    bundle = PABundle(
        drug=denial_result.drug_name or drug_class.upper(),
        drug_class=drug_class,
        payer=denial_result.payer,
        patient_id=patient_id,
        patient_name=patient_name,
        clinical_evidence=clinical_evidence,
        payer_requirements=payer_requirements,
        requirements_met=denial_result.met_criteria,
        requirements_unmet=denial_result.unmet_criteria,
        ready_to_submit=ready_to_submit,
        supporting_documents=supporting_documents,
        appeal_notes=appeal_notes,
        approval_probability=denial_result.approval_probability,
        data_source=denial_result.data_source,
    )

    logger.info(
        "PA bundle built: drug=%s payer=%s ready=%s met=%d unmet=%d",
        bundle.drug,
        bundle.payer,
        bundle.ready_to_submit,
        len(bundle.requirements_met),
        len(bundle.requirements_unmet),
    )

    return bundle


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_evidence_items(denial_result: DenialRiskResult) -> list[EvidenceItem]:
    """
    Convert met_criteria and unmet_criteria strings from DenialRiskResult
    into typed EvidenceItem objects.

    The denial scorer produces strings like:
      "Step therapy met: metformin 180 days (required ≥90)"
      "Clinical criteria met: A1C 7.5% meets threshold; BMI 33 meets standard threshold"

    We parse these into structured EvidenceItems by splitting on the first colon.
    C# analogy: LINQ projection — criteria.Select(c => new EvidenceItem { ... })
    """
    items: list[EvidenceItem] = []

    for criterion_text in denial_result.met_criteria:
        # Split "Step therapy met: metformin 180 days" into
        # criterion="Step therapy met" and value="metformin 180 days"
        parts = criterion_text.split(":", 1)  # maxsplit=1 — split at first colon only
        criterion = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else criterion_text

        items.append(EvidenceItem(
            criterion=criterion,
            met=True,
            value=value,
            source=denial_result.data_source,
        ))

    for criterion_text in denial_result.unmet_criteria:
        parts = criterion_text.split(":", 1)
        criterion = parts[0].strip()
        value = parts[1].strip() if len(parts) > 1 else "Not available"

        items.append(EvidenceItem(
            criterion=criterion,
            met=False,
            value=value,
            source=denial_result.data_source,
        ))

    return items


def _build_supporting_documents(
    drug_class: str,
    denial_result: DenialRiskResult,
) -> list[str]:
    """
    Build the list of documents to attach to the PA submission.

    Starts with the standard document list for this drug class.
    If any criteria are unmet, appends a note about what's specifically missing.

    list() creates a shallow copy of the base list so we don't modify the
    module-level constant when we append to it.
    C# analogy: new List<string>(baseList) { extraItem }
    """
    docs = list(_SUPPORTING_DOCUMENTS.get(drug_class, []))

    # If criteria are unmet, add a specific note about what must be obtained
    missing_docs: list[str] = []
    for unmet in denial_result.unmet_criteria:
        for keyword, doc in _CRITERION_DOCUMENT_MAP.items():
            if keyword in unmet and doc not in missing_docs and doc not in docs:
                missing_docs.append(doc)

    if missing_docs:
        docs.append(
            f"ACTION REQUIRED — obtain before submitting: {'; '.join(missing_docs)}"
        )

    return docs


def _build_appeal_notes(denial_result: DenialRiskResult) -> str | None:
    """
    Generate pre-filled appeal notes when requirements are unmet.

    In Phase 5 these become the seed content for OpenAI-generated appeal letters.
    In Phase 4 they're template strings that give the clinician a starting point.
    """
    if not denial_result.unmet_criteria:
        return None

    lines = [
        f"PA for {denial_result.drug_name or denial_result.drug_class.upper()} — "
        f"pre-submission review required.",
        "",
        "Outstanding items:",
    ]

    for i, action in enumerate(denial_result.suggested_actions, start=1):
        lines.append(f"  {i}. {action}")

    lines += [
        "",
        "Clinical justification:",
        f"  - Approval probability: {denial_result.approval_probability}%",
        f"  - Risk level: {denial_result.risk_level}",
        f"  - Payer: {denial_result.payer}",
    ]

    # "\n".join(lines) concatenates list into a multi-line string
    # C# analogy: string.Join(Environment.NewLine, lines)
    return "\n".join(lines)


def _extract_patient_name(hook_request: HookRequest) -> str:
    """
    Extract patient display name from the prefetch Patient resource.
    Returns empty string if not available — name is nice-to-have, not required.
    """
    prefetch = hook_request.prefetch or {}
    patient = prefetch.get("patient", {})
    if not isinstance(patient, dict):
        return ""

    names = patient.get("name", [])
    if not names:
        return ""

    # Prefer "official" name, fall back to first available
    # next() with a default — C# analogy: names.FirstOrDefault(n => n["use"] == "official") ?? names[0]
    best = next((n for n in names if n.get("use") == "official"), names[0])
    given = " ".join(best.get("given", []))
    family = best.get("family", "")
    return f"{given} {family}".strip()
