"""
Appeal letter generator — produces PA appeal drafts for high-denial-risk orders.

When CFIP identifies a high-risk order (score < 50) or a prior denial pattern,
it generates a draft appeal letter the provider can review, edit, and send to
the payer's medical director.

Triggered by: D8 hook handler (after orchestrator scores the request)
Delivered via: "View Appeal Draft" link on the CDS card (Phase 6: SMART app)

Design:
  - AppealGenerator.generate() is the single public method
  - Uses OpenAIClient.generate_appeal_letter() → 5s timeout → template fallback
  - should_generate_appeal() guards the call — only invoked when risk warrants it
  - All appeal content goes through the OpenAI client; this file is orchestration only

C# analogy: an async service class implementing IAppealGenerator,
injected by the hook handler and called when denialRisk.RiskLevel == "high".
"""

from __future__ import annotations

import logging

from app.intelligence.openai_client import OpenAIClient
from app.models.domain import AgentResult, AppealLetter, DenialRiskResult

logger = logging.getLogger(__name__)

# Threshold below which an appeal letter is always generated.
_HIGH_RISK_THRESHOLD = 50

# Threshold for moderate risk — appeal generated only when past denials also exist.
_MODERATE_RISK_THRESHOLD = 80


# ---------------------------------------------------------------------------
# Guard — should we generate an appeal for this result?
# ---------------------------------------------------------------------------

def should_generate_appeal(agent_result: AgentResult) -> bool:
    """
    Return True when generating an appeal letter is warranted.

    Rules:
      Score < 50  (high risk / critical)   → always generate
      50 ≤ score < 80 (moderate / warning) → generate only if past denials exist
      Score ≥ 80  (low risk / info)        → no appeal needed

    C# analogy: a static bool ShouldGenerateAppeal(AgentResult result)
    """
    denial = agent_result.denial_risk
    if denial is None or isinstance(denial, type(None)):
        return False

    # Use duck-typing: DenialRiskResult and ProcedureScoreResult both have
    # approval_probability. isinstance check guards against PipelineError.
    if not hasattr(denial, "approval_probability"):
        return False

    prob = denial.approval_probability

    if prob < _HIGH_RISK_THRESHOLD:
        return True     # always appeal when critically low

    # Moderate risk: appeal only if there's a history of prior denials
    if prob < _MODERATE_RISK_THRESHOLD:
        # Check unmet_criteria for any denial-history mention
        unmet = getattr(denial, "unmet_criteria", [])
        return any("denial" in c.lower() or "prior" in c.lower() for c in unmet)

    return False


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class AppealGenerator:
    """
    Generates a PA appeal letter draft from an AgentResult.

    One instance per request — stateless beyond the OpenAI client.

    Usage:
        generator = AppealGenerator()
        if should_generate_appeal(agent_result):
            letter = await generator.generate(agent_result)
    """

    async def generate(
        self,
        agent_result: AgentResult,
        denial_reason: str = "",
    ) -> AppealLetter:
        """
        Generate an appeal letter for a high-risk or denied prior authorization.

        Args:
            agent_result:  Full orchestrator result (provides drug, payer, evidence).
            denial_reason: Override denial reason string. If omitted, the generator
                           infers it from unmet criteria.

        Returns:
            AppealLetter — always returns a letter (template on OpenAI failure).

        C# analogy: async Task<AppealLetter> GenerateAsync(AgentResult, string)
        """
        denial = agent_result.denial_risk

        # Extract typed denial data safely — DenialRiskResult or None
        denial_result: DenialRiskResult | None = (
            denial
            if isinstance(denial, DenialRiskResult)
            else None
        )

        drug   = agent_result.drug or "the medication"
        payer  = denial_result.payer if denial_result else ""
        risk   = denial_result.risk_level if denial_result else "high"

        # Determine denial reason
        reason = denial_reason or self._infer_denial_reason(denial_result)

        # Build evidence context for the OpenAI prompt
        context = self._build_context(agent_result, denial_result)

        # Call OpenAI (with timeout + fallback)
        client = OpenAIClient()
        content, source = await client.generate_appeal_letter(context, reason)

        # Extract evidence references from met criteria (for audit)
        evidence_refs: list[str] = []
        if denial_result:
            evidence_refs = denial_result.met_criteria[:5]   # top 5 met criteria as citations

        logger.info(
            "Appeal letter generated: drug=%s payer=%s risk=%s source=%s",
            drug, payer, risk, source,
        )

        return AppealLetter(
            drug=drug,
            payer=payer,
            denial_reason=reason,
            content=content,
            source=source,
            addressed_to="Medical Director",
            evidence_references=evidence_refs,
            generated_for_risk_level=risk,
        )

    # ── Private helpers ────────────────────────────────────────────────────

    def _infer_denial_reason(
        self,
        denial_result: DenialRiskResult | None,
    ) -> str:
        """
        Infer the most likely denial reason from unmet criteria.

        Maps known unmet criterion prefixes to denial reason codes.
        Returns "insufficient_documentation" as a safe default.

        C# analogy: a private string InferDenialReason(DenialRiskResult?)
        """
        if not denial_result or not denial_result.unmet_criteria:
            return "insufficient_documentation"

        first_unmet = denial_result.unmet_criteria[0].lower()

        if "step therapy" in first_unmet:
            return "step_therapy_not_met"
        if "clinical criteria" in first_unmet:
            return "clinical_criteria_not_met"
        if "documentation" in first_unmet:
            return "insufficient_documentation"
        if "coverage" in first_unmet:
            return "coverage_issue"
        if "denial" in first_unmet or "prior" in first_unmet:
            return "prior_denial_history"

        # Fallback: use the first 40 chars of the unmet criterion as the reason
        return first_unmet[:40].strip(" :,-")

    def _build_context(
        self,
        agent_result: AgentResult,
        denial_result: DenialRiskResult | None,
    ) -> dict[str, str]:
        """
        Build the flat evidence context dict for the OpenAI prompt.

        Mirrors the structure expected by OpenAIClient.generate_appeal_letter().
        All values are strings — Pydantic models are not serialised here.

        C# analogy: a mapping method projecting AgentResult → AppealPromptContext DTO.
        """
        ctx: dict[str, str] = {
            "drug":       agent_result.drug or "the medication",
            "drug_class": agent_result.drug_class,
            "narrative":  agent_result.narrative,
        }

        if denial_result:
            ctx["payer"]               = denial_result.payer or ""
            ctx["approval_probability"] = str(denial_result.approval_probability)
            ctx["risk_level"]           = denial_result.risk_level
            ctx["met_criteria"]         = "; ".join(denial_result.met_criteria[:3])
            ctx["unmet_criteria"]       = "; ".join(denial_result.unmet_criteria[:3])

        return ctx
