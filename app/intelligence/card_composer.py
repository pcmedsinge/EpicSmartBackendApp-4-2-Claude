"""
CDS card composer — converts pipeline results into CDS Hooks Cards.

Phase 3: Template-based string formatting. No LLM calls.
Phase 4: Added PGx safety cards and PA-ready status on denial card.
Phase 5: LLM (OpenAI) will replace the detail narrative for richer text.

Public functions:
  compose_denial_card(result, pa_bundle)  — GLP-1 denial risk + PA status
  compose_pgx_card(pgx_result)            — PGx safety alert or recommend-testing
  compose_error_card(error)               — pipeline failure fallback

The hook handler always has a card to return — even when the pipeline fails.
"""

from __future__ import annotations

from app.models.cds_hooks import Card, CdsSource, Link, Suggestion
from app.models.domain import AgentResult, DenialRiskResult, PABundle, PipelineError
from app.rules.cpic_engine import PgxResult

# Source attribution shown on every card
_SOURCE = CdsSource(label="CFIP Clinical-Financial Intelligence")

# CDS Hooks spec: summary must be ≤140 characters
_MAX_SUMMARY_LEN = 140

# Emoji indicators per criterion state — renders in Epic's card detail section
_CHECK = "✅"
_CROSS = "❌"
_WARN  = "⚠️"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compose_denial_card(
    result: DenialRiskResult,
    pa_bundle: PABundle | None = None,
) -> Card:
    """
    Build a CDS card from a successful DenialRiskResult.

    Phase 4: accepts an optional PABundle — when present, the summary and
    detail include PA readiness status ("PA Ready to Submit" or requirements
    outstanding).

    Args:
        result:    The output of the Clinical-Financial Bridge pipeline.
        pa_bundle: Optional PA bundle from the Specialty PA Builder.

    Returns:
        A spec-compliant Card ready to include in a CdsResponse.
    """
    summary     = _build_summary(result, pa_bundle)
    detail      = _build_detail(result, pa_bundle)
    suggestions = _build_suggestions(result, pa_bundle)
    links       = _build_links(result)

    return Card(
        summary=summary,
        indicator=result.indicator,  # type: ignore[arg-type]
        # type: ignore[arg-type]: result.indicator is "info"|"warning"|"critical"
        # — guaranteed by the scorer, but mypy can't prove the string literal type.
        source=_SOURCE,
        detail=detail,
        suggestions=suggestions,
        selectionBehavior="at-most-one" if suggestions else None,
        links=links,
    )


def compose_error_card(error: PipelineError) -> Card:
    """
    Build a safe fallback card when the pipeline fails.

    Rather than letting the hook handler return a 500 (which breaks Epic's
    workflow), we return an informational card explaining that CFIP's analysis
    is unavailable. The clinician can still proceed — they just don't have
    the denial risk score.

    Args:
        error: The PipelineError from the bridge.

    Returns:
        A Card with indicator="info" and a brief explanation.
    """
    summary = "CFIP: Analysis unavailable — proceed with standard workflow"[:_MAX_SUMMARY_LEN]

    detail_lines = [
        "### CFIP Clinical-Financial Analysis",
        "",
        "**Status:** Analysis could not be completed for this order.",
        "",
        f"**Reason:** {error.message}",
        "",
    ]

    if error.recoverable:
        detail_lines.append(
            "_You can proceed with this order. "
            "CFIP's denial risk assessment is temporarily unavailable._"
        )
    else:
        detail_lines.append(
            "_Please contact your system administrator if this persists._"
        )

    return Card(
        summary=summary,
        indicator="info",   # never alarm the clinician when it's a system issue
        source=_SOURCE,
        detail="\n".join(detail_lines),
        suggestions=[],
        links=[],
    )


def compose_pgx_card(pgx_result: PgxResult) -> Card | None:
    """
    Build a CDS card from a PgxResult.

    Three outcomes:
      has_interaction=True              → critical safety alert
      pgx_data_available=False          → warning, recommend testing
      has_interaction=False + data ok   → None (no card needed — drug is safe)

    Returns None when no card is warranted (normal metabolizer, no interaction).
    The hook handler should skip adding None to the cards list.
    """
    drug = pgx_result.drug_name or "Drug"

    # --- No interaction, data was available → silent (no card) ---
    if not pgx_result.has_interaction and pgx_result.pgx_data_available and pgx_result.severity != "none":
        return None

    # --- Drug not PGx-sensitive → no card ---
    if pgx_result.severity == "none":
        return None

    # --- No PGx data on file → recommend testing ---
    if not pgx_result.pgx_data_available:
        return _compose_pgx_recommend_testing_card(pgx_result, drug)

    # --- Interaction found → safety alert ---
    return _compose_pgx_alert_card(pgx_result, drug)


# ---------------------------------------------------------------------------
# Phase 5 D6 — AgentResult entry point
# ---------------------------------------------------------------------------

def compose_from_agent_result(agent_result: AgentResult) -> list[Card]:
    """
    Single entry point for card composition from an orchestrator AgentResult.

    Replaces direct calls to compose_denial_card / compose_pgx_card in the hook
    handler (wired in D8). Adds LLM narrative injection where appropriate.

    Routing:
      glp1         → denial card  + PA status  + narrative
      pgx_sensitive → PGx alert or testing card (templates ONLY — no narrative)
      oncology      → oncology card            + narrative
      standard      → denial card              + narrative

    Always returns at least one card.

    C# analogy: a factory method that dispatches to specialised builders
    based on a discriminated union tag (drug_class).
    """
    drug_class   = agent_result.drug_class
    narrative    = agent_result.narrative
    source       = agent_result.narrative_source
    drug         = agent_result.drug or "Drug"
    data_sources = agent_result.data_sources       # phase 6: per-step provenance
    fhir_fetched = agent_result.fhir_fetched       # phase 6: did Epic auth succeed?
    cards: list[Card] = []

    # ── GLP-1 ─────────────────────────────────────────────────────────────
    if drug_class == "glp1":
        if agent_result.denial_risk and not isinstance(agent_result.denial_risk, PipelineError):
            card = compose_denial_card(agent_result.denial_risk, agent_result.pa_bundle)
            cards.append(_inject_narrative(card, narrative, source))
        else:
            err = (
                agent_result.denial_risk
                if isinstance(agent_result.denial_risk, PipelineError)
                else PipelineError(
                    code="no_result",
                    message="GLP-1 pipeline did not produce a result.",
                    recoverable=True,
                )
            )
            cards.append(compose_error_card(err))

    # ── PGx ────────────────────────────────────────────────────────────────
    elif drug_class == "pgx_sensitive":
        if agent_result.pgx_result:
            pgx_card = compose_pgx_card(agent_result.pgx_result)
            if pgx_card:
                # Safety rule: PGx cards NEVER receive LLM narrative injection.
                # Every word on a PGx safety alert must be deterministic and
                # reproducible — no AI-generated text for safety-critical content.
                cards.append(pgx_card)
        if not cards:
            # Normal metabolizer or PGx not sensitive — silent (no card)
            cards.append(Card(
                summary=f"{drug}: PGx check complete — no action required",
                indicator="info",
                source=_SOURCE,
                detail=(
                    "### PGx Check — All Clear\n\n"
                    "No clinically significant drug-gene interaction was identified "
                    "based on available genomic data."
                ),
                suggestions=[],
                links=[],
            ))

    # ── Oncology ───────────────────────────────────────────────────────────
    elif drug_class == "oncology":
        if agent_result.pa_bundle:
            card = compose_oncology_card(agent_result.pa_bundle, narrative, source)
            cards.append(card)
        else:
            cards.append(Card(
                summary=f"{drug}: Oncology pathway analysis unavailable",
                indicator="warning",
                source=_SOURCE,
                detail=(
                    "### Oncology Pathway Validation\n\n"
                    "Pathway validation could not be completed. "
                    "Please review clinical evidence and resubmit."
                ),
                suggestions=[],
                links=[],
            ))

    # ── Standard (denial prevention) ──────────────────────────────────────
    else:
        if agent_result.denial_risk and not isinstance(agent_result.denial_risk, PipelineError):
            card = compose_denial_card(agent_result.denial_risk, pa_bundle=None)
            cards.append(_inject_narrative(card, narrative, source))
        else:
            cards.append(Card(
                summary=f"{drug}: Denial prevention analysis unavailable",
                indicator="info",
                source=_SOURCE,
                detail=(
                    "### Denial Prevention\n\n"
                    "Could not complete the denial prevention analysis. "
                    "Proceed with standard workflow."
                ),
                suggestions=[],
                links=[],
            ))

    # Phase 6: append data source footnote to every card
    cards = [_inject_data_sources(c, data_sources, fhir_fetched) for c in cards]

    return cards


def compose_oncology_card(
    pa_bundle: PABundle,
    narrative: str = "",
    narrative_source: str = "template",
) -> Card:
    """
    Build a CDS card for an oncology PA request.

    Accepts the PABundle assembled by the orchestrator's oncology chain.
    The bundle already contains requirements_met / requirements_unmet from
    the NCCN + biomarker validation steps.

    Args:
        pa_bundle:        PABundle from the orchestrator's build_pa_bundle step.
        narrative:        LLM-generated or template summary string.
        narrative_source: "openai" | "template" — shown as a badge in the detail.

    Returns:
        A spec-compliant Card (indicator "info" if ready, "warning" if incomplete).

    C# analogy: a static factory method — same shape as compose_denial_card()
    but with oncology-specific labels and linking.
    """
    status_icon = "✅" if pa_bundle.ready_to_submit else "⚠️"
    pa_status   = "PA Bundle Ready" if pa_bundle.ready_to_submit else "PA Incomplete"
    drug = pa_bundle.drug or "Oncology Drug"

    summary = (
        f"{status_icon} {drug}: NCCN Pathway Validated | {pa_status}"
        if pa_bundle.ready_to_submit
        else f"{status_icon} {drug}: Pathway Review Required | {pa_status}"
    )[:_MAX_SUMMARY_LEN]

    # Detail section
    lines: list[str] = []

    # Narrative section (LLM or template)
    if narrative:
        badge = _narrative_source_badge(narrative_source)
        lines += [f"### Clinical Summary {badge}", "", narrative, "", "---", ""]

    lines += ["### Oncology Pathway Validation", ""]

    if pa_bundle.requirements_met:
        lines.append("**Criteria met:**")
        for req in pa_bundle.requirements_met:
            lines.append(f"- {_CHECK} {req}")
        lines.append("")

    if pa_bundle.requirements_unmet:
        lines.append("**Outstanding:**")
        for req in pa_bundle.requirements_unmet:
            lines.append(f"- {_CROSS} {req}")
        lines.append("")

    if pa_bundle.supporting_documents:
        lines.append("**Required documents:**")
        for doc in pa_bundle.supporting_documents[:4]:
            lines.append(f"- {doc}")
        lines.append("")

    lines.append(f"_Payer: {pa_bundle.payer or 'UnitedHealthcare'} | Drug class: Oncology_")

    # Suggestions
    suggestions: list[Suggestion] = []
    if pa_bundle.ready_to_submit:
        suggestions = [
            Suggestion(label="Submit PA Now", isRecommended=True),
            Suggestion(label="Review PA Bundle"),
        ]
    else:
        suggestions = [Suggestion(label="Address Documentation Gaps")]

    return Card(
        summary=summary,
        indicator="info" if pa_bundle.ready_to_submit else "warning",
        source=_SOURCE,
        detail="\n".join(lines),
        suggestions=suggestions,
        selectionBehavior="at-most-one" if suggestions else None,
        links=[Link(
            label="View Pipeline Execution Trace",
            url="https://placeholder.cfip.app/oncology",
            type="absolute",
        )],
    )


# ---------------------------------------------------------------------------
# Private builders
# ---------------------------------------------------------------------------

def _build_summary(result: DenialRiskResult, pa_bundle: PABundle | None) -> str:
    """
    Build the card headline (≤140 chars).

    With PA bundle:
      "Ozempic: 87% Approval | PA Ready to Submit | Est. ~$150/mo (indicative only)"
      "Ozempic: 65% Approval — Moderate Risk | PA Incomplete"
    Without PA bundle:
      "Ozempic: 87% Approval Probability | Est. ~$150/mo (indicative only)"
    """
    drug = result.drug_name or result.drug_class.upper()
    prob = result.approval_probability
    cost = f"Est. ~${result.cost_estimate_monthly:.0f}/mo (indicative only)" if result.cost_estimate_monthly else ""

    if result.risk_level == "low":
        pa_status = " | PA Ready to Submit" if pa_bundle and pa_bundle.ready_to_submit else ""
        core = f"{drug}: {prob}% Approval{pa_status}"
    elif result.risk_level == "moderate":
        pa_status = " | PA Incomplete" if pa_bundle and not pa_bundle.ready_to_submit else ""
        core = f"{drug}: {prob}% Approval — Moderate Risk{pa_status}"
    else:
        first_issue = _first_unmet_short(result)
        core = f"{drug}: {prob}% Approval — {first_issue}" if first_issue else f"{drug}: {prob}% Approval — High Denial Risk"

    summary = f"{core} | {cost}" if cost and len(f"{core} | {cost}") <= _MAX_SUMMARY_LEN else core
    return summary[:_MAX_SUMMARY_LEN]


def _build_detail(result: DenialRiskResult, pa_bundle: PABundle | None) -> str:
    """
    Build the expandable markdown detail section.

    Phase 4: adds a PA Bundle Status section when pa_bundle is provided.
    """
    lines: list[str] = []

    # Header
    lines.append("### Denial Risk Assessment")
    lines.append("")

    # Probability + data source badge
    source_badge = _data_source_badge(result.data_source)
    lines.append(f"**Approval probability: {result.approval_probability}%** {source_badge}")
    lines.append("")

    # Evidence breakdown
    lines.append("**Evidence:**")
    for criterion in result.met_criteria:
        lines.append(f"- {_CHECK} {criterion}")
    for criterion in result.unmet_criteria:
        lines.append(f"- {_CROSS} {criterion}")
    lines.append("")

    # Suggested actions
    if result.suggested_actions:
        lines.append("**Recommended actions:**")
        for action in result.suggested_actions:
            lines.append(f"- {action}")
        lines.append("")

    # Cost estimate
    if result.cost_estimate_monthly:
        lines.append(f"**Estimated cost:** ~${result.cost_estimate_monthly:.0f}/mo copay _(indicative only — not based on patient formulary)_")
        lines.append("")

    # PA Bundle Status section (Phase 4)
    if pa_bundle:
        lines += _build_pa_section(pa_bundle)

    # Footer
    if result.payer:
        lines.append(f"_Payer: {result.payer} | Drug class: {result.drug_class.upper()}_")

    return "\n".join(lines)


def _build_suggestions(
    result: DenialRiskResult,
    pa_bundle: PABundle | None,
) -> list[Suggestion]:
    """
    Build actionable suggestion buttons.

    With PA bundle ready:    "Submit PA Now" (recommended) + "Review PA Bundle"
    PA bundle not ready:     "Review Documentation Before Submitting"
    No PA bundle:            same as Phase 3 behaviour
    """
    if pa_bundle and pa_bundle.ready_to_submit:
        return [
            Suggestion(label="Submit PA Now", isRecommended=True),
            Suggestion(label="Review PA Bundle"),
        ]
    elif pa_bundle and not pa_bundle.ready_to_submit:
        return [Suggestion(label="Review Documentation Before Submitting")]
    elif result.risk_level == "low":
        return [Suggestion(label="Submit PA Now", isRecommended=True)]
    elif result.risk_level == "moderate":
        return [Suggestion(label="Review Documentation Before Submitting")]
    else:
        return [Suggestion(label="Address Issues Before Submitting")]


def _build_links(result: DenialRiskResult) -> list[Link]:
    """
    Build the links section. Always includes "View Full Analysis".

    type="smart" will launch the SMART Companion App in Phase 6.
    For now it's a placeholder — Epic will ignore unknown URLs.
    """
    return [
        Link(
            label="View Pipeline Execution Trace",
            url="https://placeholder.cfip.app/analysis",  # replaced in Phase 6
            type="absolute",
        )
    ]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _first_unmet_short(result: DenialRiskResult) -> str:
    """
    Extract a short (≤40 char) label from the first unmet criterion,
    suitable for embedding in the card summary headline.
    """
    if not result.unmet_criteria:
        return ""

    first = result.unmet_criteria[0]

    # Map known prefixes to short labels for the headline
    # startswith() checks if a string begins with a given prefix.
    # C# analogy: first.StartsWith(...)
    if "Step therapy NOT met" in first:
        return "Step Therapy Not Met"
    if "Clinical criteria NOT met" in first:
        return "Clinical Criteria Not Met"
    if "Documentation gaps" in first:
        return "Documentation Incomplete"
    if "Coverage NOT active" in first:
        return "Coverage Issue"

    # Generic fallback: take first 40 chars, trimmed at a word boundary
    short = first[:40]
    if len(first) > 40 and " " in short:
        short = short[: short.rfind(" ")]
    return short.rstrip(" :,-")


def _data_source_badge(data_source: str) -> str:
    """
    Return a small markdown badge indicating where the evidence came from.
    Shown next to the probability so the clinician knows if synthetic data was used.
    """
    if data_source == "synthetic":
        return "_(demo data)_"
    elif data_source == "mixed":
        return "_(partial FHIR + demo data)_"
    else:
        return ""   # real FHIR data needs no badge


def _build_pa_section(pa_bundle: PABundle) -> list[str]:
    """
    Build the PA Bundle Status section for the denial card detail.

    Renders as:
      ### Prior Authorization Bundle
      **Status:** ✅ Ready to Submit  (or ❌ Requirements Incomplete)
      **Requirements met:** ...
      **Outstanding:** ...
    """
    lines: list[str] = []
    lines.append("### Prior Authorization Bundle")
    lines.append("")

    if pa_bundle.ready_to_submit:
        lines.append(f"**Status:** {_CHECK} Ready to Submit")
    else:
        lines.append(f"**Status:** {_CROSS} Requirements Incomplete")

    lines.append("")

    if pa_bundle.requirements_met:
        lines.append("**Requirements met:**")
        for req in pa_bundle.requirements_met:
            # Show just the first part before the colon to keep it brief
            short = req.split(":")[0].strip()
            lines.append(f"- {_CHECK} {short}")
        lines.append("")

    if pa_bundle.requirements_unmet:
        lines.append("**Outstanding:**")
        for req in pa_bundle.requirements_unmet:
            short = req.split(":")[0].strip()
            lines.append(f"- {_CROSS} {short}")
        lines.append("")

    return lines


def _compose_pgx_alert_card(pgx_result: PgxResult, drug: str) -> Card:
    """
    Build a critical safety alert card for a confirmed PGx interaction.

    Example: Clopidogrel + CYP2C19 *2/*2 poor metabolizer.
    """
    gene   = pgx_result.gene or "Unknown gene"
    status = _format_metabolizer_status(pgx_result.metabolizer_status)
    diplotype = pgx_result.diplotype or "unknown diplotype"

    summary = f"{_WARN} {drug.capitalize()}: {gene} {status} — Drug Ineffective"[:_MAX_SUMMARY_LEN]

    lines = [
        f"### {_WARN} PGx Safety Alert — {drug.capitalize()}",
        "",
        f"**Gene:** {gene}  |  **Status:** {status}  |  **Diplotype:** {diplotype}",
        "",
        "**Clinical impact:**",
        pgx_result.recommendation,
        "",
    ]

    if pgx_result.alternative_drug:
        lines += [
            "**Recommended alternatives:**",
        ]
        # alternative_drug may be "prasugrel, ticagrelor" — split and list each
        for alt in pgx_result.alternative_drug.split(","):
            lines.append(f"- {alt.strip()}")
        lines.append("")

    if pgx_result.evidence_level:
        lines.append(f"_CPIC Evidence Level: {pgx_result.evidence_level} "
                     f"(cpicpgx.org)_")

    # Suggestions: one per alternative drug
    suggestions: list[Suggestion] = []
    if pgx_result.alternative_drug:
        for alt in pgx_result.alternative_drug.split(","):
            suggestions.append(Suggestion(
                label=f"Switch to {alt.strip().capitalize()}",
                isRecommended=(len(suggestions) == 0),  # first alternative is recommended
            ))

    return Card(
        summary=summary,
        indicator="critical",
        source=_SOURCE,
        detail="\n".join(lines),
        suggestions=suggestions,
        selectionBehavior="at-most-one" if suggestions else None,
        links=[Link(
            label="View Pipeline Execution Trace",
            url="https://placeholder.cfip.app/pgx",
            type="absolute",
        )],
    )


def _compose_pgx_recommend_testing_card(pgx_result: PgxResult, drug: str) -> Card:
    """
    Build a warning card recommending PGx testing when no genomic data is on file.

    This is the most common real-world case — most patients haven't been tested.
    """
    gene = pgx_result.gene or "relevant gene"
    summary = f"{drug.capitalize()}: No PGx Data — {gene} Testing Recommended"[:_MAX_SUMMARY_LEN]

    lines = [
        f"### {_WARN} PGx Testing Recommended — {drug.capitalize()}",
        "",
        f"**No {gene} genomic data on file for this patient.**",
        "",
        pgx_result.recommendation,
        "",
        "**Why this matters:**",
        f"- {gene} status can significantly affect {drug} efficacy or safety",
        "- Testing takes 1-2 weeks — order proactively if therapy is planned",
        "- Results are reusable for future prescribing decisions",
    ]

    return Card(
        summary=summary,
        indicator="warning",
        source=_SOURCE,
        detail="\n".join(lines),
        suggestions=[Suggestion(label=f"Order {gene} PGx Panel")],
        selectionBehavior="at-most-one",
        links=[Link(
            label="View Pipeline Execution Trace",
            url="https://placeholder.cfip.app/pgx",
            type="absolute",
        )],
    )


def _inject_narrative(card: Card, narrative: str, source: str) -> Card:
    """
    Prepend a Clinical Summary section (with narrative_source badge) to the
    card detail markdown.

    Called for glp1 and standard cards — never for pgx_sensitive (safety rule).
    Returns a new Card instance using model_copy() — Pydantic v2 immutable update.

    C# analogy: card with { Detail = narrativeSection + card.Detail }
    using record copy semantics.
    """
    if not narrative:
        return card

    badge = _narrative_source_badge(source)
    narrative_section = f"### Clinical Summary {badge}\n\n{narrative}\n\n---\n\n"
    new_detail = narrative_section + (card.detail or "")

    # model_copy(update={...}) is the Pydantic v2 way to produce a modified copy.
    # C# analogy: card with { Detail = newDetail }  (non-destructive record update)
    return card.model_copy(update={"detail": new_detail})


def _narrative_source_badge(source: str) -> str:
    """
    Return a small markdown badge indicating how the narrative was generated.

    Shown next to "Clinical Summary" heading so clinicians know whether
    the text is AI-generated or came from a deterministic template.
    """
    if source == "openai":
        return "_(AI-generated)_"
    return "_(template)_"


def _format_metabolizer_status(status: str | None) -> str:
    """Convert snake_case metabolizer status to a readable label."""
    if not status:
        return "Unknown"
    # str.replace() + title case: "poor_metabolizer" → "Poor Metabolizer"
    # C# analogy: status.Replace("_", " ").ToTitleCase()
    return status.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Phase 6 — Data source footnote
# ---------------------------------------------------------------------------

# Maps evidence chain step names to the human-readable label shown in the
# data sources section of the card.  Steps not listed here are omitted.
_STEP_TO_LABEL: dict[str, str] = {
    "fetch_condition":      "Tumor type & diagnosis",
    "fetch_biomarkers":     "PD-L1 biomarker",
    "fetch_prior_regimens": "Prior chemotherapy regimens",
    "fetch_claims_history": "Payer & coverage",
}

# ✓ = came from real Epic FHIR  |  ⚡ = filled by synthetic overlay
_FHIR_ICON      = "✓"
_SYNTHETIC_ICON = "⚡"


def _build_data_sources_section(
    data_sources: dict[str, str],
    fhir_fetched: bool,
) -> str:
    """
    Build the '### Data Sources' markdown section appended to every card.

    Shows exactly what came from the real Epic FHIR API vs the synthetic
    overlay.  Makes the demo transparent and honest about data provenance.

    Example output:
      ### Data Sources
      ✓ Patient record — Epic FHIR R4
      ✓ Tumor type & diagnosis — Epic FHIR R4
      ⚡ PD-L1 biomarker — Synthetic overlay (not in Epic sandbox)
      ⚡ Prior chemotherapy regimens — Synthetic overlay (not in Epic sandbox)

    C# analogy: a StringBuilder building a provenance section for a report.
    """
    lines: list[str] = ["", "---", "", "### Data Sources", ""]

    # Patient record line — reflects whether we made a real FHIR call at all
    if fhir_fetched:
        lines.append(f"{_FHIR_ICON} Patient record — Epic FHIR R4")
    else:
        lines.append(f"{_SYNTHETIC_ICON} Patient record — Synthetic overlay (Epic FHIR unavailable)")

    # Per-step lines for steps we have a human-readable label for
    for step_name, label in _STEP_TO_LABEL.items():
        source = data_sources.get(step_name)
        if source is None:
            continue   # step didn't run for this scenario — skip
        if source == "fhir":
            lines.append(f"{_FHIR_ICON} {label} — Epic FHIR R4")
        elif source == "mixed":
            lines.append(f"{_FHIR_ICON} {label} — Epic FHIR R4 (partial)")
        else:
            lines.append(f"{_SYNTHETIC_ICON} {label} — Synthetic overlay (not in Epic sandbox)")

    # Closing legend so non-technical audience understands the icons
    lines += [
        "",
        f"_{_FHIR_ICON} = live Epic FHIR data  ·  {_SYNTHETIC_ICON} = demo overlay_",
    ]

    return "\n".join(lines)


def _inject_data_sources(
    card: Card,
    data_sources: dict[str, str],
    fhir_fetched: bool,
) -> Card:
    """
    Append the data sources footnote to a card's detail markdown.

    Returns a new Card (Pydantic immutable copy) — does not mutate the original.
    C# analogy: card with { Detail = card.Detail + footnote }
    """
    footnote = _build_data_sources_section(data_sources, fhir_fetched)
    new_detail = (card.detail or "") + footnote
    return card.model_copy(update={"detail": new_detail})
