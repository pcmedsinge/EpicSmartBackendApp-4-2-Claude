"""
OpenAI client — async wrapper for narrative generation and appeal letter drafting.

Used by:
  Orchestrator.generate_narrative step  → concise clinical card narrative
  AppealGenerator                       → formal appeal letter draft

Design decisions:
  - 5-second timeout on every OpenAI call — CDS Hooks must respond quickly
  - Template fallback when OpenAI is unavailable (network error, no key, timeout)
  - PGx safety content NEVER routed here — deterministic templates only for
    safety-critical decisions (enforced at the orchestrator chain level)
  - All methods return (text, source) tuple so callers can log narrative_source
    without needing separate state

C# analogy: an async service class injected via DI —
  IOpenAIClient with two async methods + a sync availability check.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from openai import AsyncOpenAI, APIError, APITimeoutError

from app.config import get_settings

logger = logging.getLogger(__name__)

# Timeout for narrative generation (short — CDS Hooks expects a fast response).
_OPENAI_TIMEOUT_SECONDS = 5.0

# Timeout for appeal letter generation — longer because appeal letters are ~400 tokens
# and are generated after the primary CDS card has already been composed.
_APPEAL_TIMEOUT_SECONDS = 15.0

# Source literals — used in AgentResult.narrative_source and AppealLetter.source
NarrativeSource = Literal["openai", "template"]


class OpenAIClient:
    """
    Async wrapper around the OpenAI Chat Completions API.

    Create one instance per request — the underlying AsyncOpenAI client is
    cheap to construct and manages its own connection pool.

    Usage:
        client = OpenAIClient()
        text, source = await client.generate_narrative(context, drug_class)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._model  = settings.openai_model
        self._api_key = settings.openai_api_key
        # AsyncOpenAI requires an explicit api_key; it does NOT read from env by default
        # when constructed this way. We pass it explicitly for clarity.
        # C# analogy: new OpenAIClient(new OpenAIClientOptions { ApiKey = apiKey })
        self._client = AsyncOpenAI(api_key=self._api_key)

    # ── Public API ─────────────────────────────────────────────────────────

    async def generate_narrative(
        self,
        context: dict[str, str],
        drug_class: str,
    ) -> tuple[str, NarrativeSource]:
        """
        Generate a concise clinical card narrative from evidence context.

        Args:
            context:    Flat dict of key clinical facts, e.g.:
                          {"drug": "Ozempic", "payer": "UHC", "approval_probability": "87",
                           "met_criteria": "A1C 7.5%; metformin 180 days", ...}
            drug_class: "glp1" | "oncology" | "standard"
                        NEVER "pgx_sensitive" — PGx always uses templates.

        Returns:
            Tuple (text, source) where source is "openai" or "template".
            Always returns a non-empty string — never raises.

        C# analogy: async Task<(string text, string source)> GenerateNarrativeAsync(...)
        """
        if not self.is_available():
            logger.info("OpenAI not available — using template narrative")
            return self._template_narrative(context, drug_class), "template"

        prompt = self._build_narrative_prompt(context, drug_class)

        try:
            text = await asyncio.wait_for(
                self._chat_complete(prompt, max_tokens=150),
                timeout=_OPENAI_TIMEOUT_SECONDS,
            )
            logger.info("OpenAI narrative generated: drug_class=%s tokens≈%d", drug_class, len(text.split()))
            return text.strip(), "openai"

        except asyncio.TimeoutError:
            logger.warning("OpenAI narrative timed out after %.1fs — using template", _OPENAI_TIMEOUT_SECONDS)
        except APITimeoutError:
            logger.warning("OpenAI API timeout — using template narrative")
        except APIError as exc:
            logger.warning("OpenAI API error: %s — using template narrative", exc)
        except Exception as exc:
            logger.warning("Unexpected error from OpenAI: %s — using template narrative", exc)

        return self._template_narrative(context, drug_class), "template"

    async def generate_appeal_letter(
        self,
        context: dict[str, str],
        denial_reason: str,
    ) -> tuple[str, NarrativeSource]:
        """
        Generate a formal appeal letter draft for a denied prior authorization.

        Args:
            context:       Flat dict of evidence facts (same structure as narrative context).
            denial_reason: The denial reason string from the payer (e.g. 'step_therapy_not_met').

        Returns:
            Tuple (letter_text, source). Always returns a non-empty string.

        C# analogy: async Task<(string letter, string source)> GenerateAppealLetterAsync(...)
        """
        if not self.is_available():
            return self._template_appeal(context, denial_reason), "template"

        prompt = self._build_appeal_prompt(context, denial_reason)

        try:
            text = await asyncio.wait_for(
                self._chat_complete(prompt, max_tokens=400),
                timeout=_APPEAL_TIMEOUT_SECONDS,
            )
            logger.info("OpenAI appeal letter generated: ~%d words", len(text.split()))
            return text.strip(), "openai"

        except asyncio.TimeoutError:
            logger.warning("OpenAI appeal letter timed out after %.1fs — using template", _APPEAL_TIMEOUT_SECONDS)
        except APITimeoutError:
            logger.warning("OpenAI API timeout — using template appeal letter")
        except APIError as exc:
            logger.warning("OpenAI API error: %s — using template appeal letter", exc)
        except Exception as exc:
            logger.warning("Unexpected error from OpenAI (appeal): %s — using template", exc)

        return self._template_appeal(context, denial_reason), "template"

    def is_available(self) -> bool:
        """
        Return True if the OpenAI API key is configured and non-empty.

        This is a synchronous check — it does NOT make a network call.
        The actual availability is determined at call time (if the key is wrong,
        generate_narrative() catches the APIError and falls back to template).

        C# analogy: a property that checks if the credentials are present.
        """
        return bool(self._api_key and self._api_key.strip() and self._api_key != "placeholder")

    # ── Private: API call ───────────────────────────────────────────────────

    async def _chat_complete(self, prompt: str, max_tokens: int) -> str:
        """
        Make a single Chat Completions call and return the text.

        Uses the cheapest model (gpt-4o-mini) — sufficient for narrative generation.
        Temperature=0.3: low enough for consistent clinical language, high enough
        to avoid robotic repetition.

        C# analogy: a private async method wrapping the SDK client call.
        """
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        # response.choices[0].message.content is the generated text.
        # The OpenAI SDK returns None if the response was filtered — we guard against that.
        content = response.choices[0].message.content
        return content or ""

    # ── Private: prompt builders ────────────────────────────────────────────

    def _build_narrative_prompt(self, context: dict[str, str], drug_class: str) -> str:
        """
        Build a drug-class-specific narrative prompt.

        Prompts are deliberately concise — gpt-4o-mini follows tight instructions well.
        The tone is "clinical informatics assistant helping a physician" — factual,
        not marketing language, no disclaimer boilerplate.
        """
        drug   = context.get("drug", "the medication")
        payer  = context.get("payer", "the payer")

        if drug_class == "glp1":
            prob   = context.get("approval_probability", "unknown")
            met    = context.get("met_criteria", "")
            unmet  = context.get("unmet_criteria", "")
            return (
                f"You are a clinical informatics assistant. Write a concise 2-3 sentence "
                f"prior authorization summary for a physician. Be factual and clinical. "
                f"Do not add disclaimers.\n\n"
                f"Drug: {drug} | Payer: {payer} | Approval probability: {prob}%\n"
                f"Met: {met or 'all criteria met'}\n"
                f"Gaps: {unmet or 'none'}\n\n"
                f"Write 2-3 sentences summarising the PA status."
            )

        elif drug_class == "oncology":
            tumor     = context.get("tumor_type", "NSCLC") or "NSCLC"
            pd_l1     = context.get("pd_l1_score", "unknown")
            pathway   = context.get("nccn_pathway", "validated")
            pa_status = context.get("pa_status", "")
            return (
                f"You are a clinical informatics assistant. Write a concise 2-3 sentence "
                f"oncology prior authorization summary for a physician.\n\n"
                f"Drug: {drug} | Tumor: {tumor} | PD-L1: {pd_l1}% | "
                f"NCCN pathway: {pathway} | {pa_status}\n\n"
                f"Write 2-3 sentences covering biomarker status, pathway validation, and PA readiness."
            )

        elif drug_class == "standard":
            risk    = context.get("risk_level", "high")
            pattern = context.get("denial_pattern", "insufficient documentation")
            missing = context.get("missing_docs", "")
            return (
                f"You are a clinical informatics assistant. Write a concise 2-3 sentence "
                f"denial risk alert for a procedure order. Warn the physician about the risk.\n\n"
                f"Procedure: {drug} | Payer: {payer} | Risk: {risk}\n"
                f"Recurring denial pattern: {pattern}\n"
                f"Missing documentation: {missing or 'none identified'}\n\n"
                f"Write 2-3 sentences summarising the denial risk and what to do before submitting."
            )

        # Fallback for unknown drug class
        return (
            f"Summarise this clinical order in 2-3 sentences: "
            f"drug={drug}, payer={payer}. Be concise and clinical."
        )

    def _build_appeal_prompt(self, context: dict[str, str], denial_reason: str) -> str:
        """
        Build a formal appeal letter prompt.

        Output format: three paragraphs — intent, clinical justification, closing.
        Target length: ~200-250 words. Addressed to "Medical Director".
        """
        drug     = context.get("drug", "the medication")
        payer    = context.get("payer", "the payer")
        met      = context.get("met_criteria", "clinical criteria documented")
        evidence = context.get("narrative", met)

        # Convert snake_case denial reason to human-readable
        reason_readable = denial_reason.replace("_", " ").title()

        return (
            f"Write a formal prior authorization appeal letter to a Medical Director. "
            f"Use professional medical writing. Under 280 words. Three paragraphs.\n\n"
            f"Paragraph 1: State this is a formal appeal for {drug} denied by {payer} "
            f"for reason: {reason_readable}.\n"
            f"Paragraph 2: Clinical justification — {evidence}. "
            f"Explain why the denial reason does not apply.\n"
            f"Paragraph 3: Request reconsideration, offer peer-to-peer review.\n\n"
            f"Sign as 'Attending Physician'. Address to 'Medical Director, {payer}'."
        )

    # ── Private: template fallbacks ─────────────────────────────────────────

    def _template_narrative(self, context: dict[str, str], drug_class: str) -> str:
        """
        Template narrative used when OpenAI is unavailable.

        Produces a clinically useful but plain-language summary.
        Same information as the LLM narrative, just less polished.
        """
        drug = context.get("drug", "the medication")

        if drug_class == "glp1":
            prob  = context.get("approval_probability", "unknown")
            payer = context.get("payer", "the payer")
            unmet = context.get("unmet_criteria", "")
            if unmet:
                return (
                    f"{drug} prior authorization pre-check: {prob}% estimated approval "
                    f"with {payer}. Outstanding items: {unmet}. "
                    f"Address gaps before submitting the PA."
                )
            return (
                f"{drug} prior authorization pre-check: {prob}% estimated approval "
                f"with {payer}. All clinical criteria appear to be met — PA ready to submit."
            )

        if drug_class == "oncology":
            pd_l1     = context.get("pd_l1_score", "")
            pathway   = context.get("nccn_pathway", "validated")
            pa_status = context.get("pa_status", "")
            pd_note   = f" PD-L1: {pd_l1}%." if pd_l1 else ""
            return (
                f"{drug} oncology PA pre-check complete.{pd_note} "
                f"NCCN pathway {pathway}. "
                + (f"{pa_status}. " if pa_status else "")
                + f"Submit with biomarker report and prior regimen documentation."
            )

        if drug_class == "standard":
            risk    = context.get("risk_level", "high")
            pattern = context.get("denial_pattern", "insufficient documentation")
            missing = context.get("missing_docs", "")
            missing_note = f" Missing: {missing}." if missing else ""
            return (
                f"{drug} denial prevention alert: {risk} denial risk.{missing_note} "
                f"Recurring pattern with this payer: '{pattern}'. "
                f"Obtain missing documentation before submitting."
            )

        return f"{drug} clinical analysis complete."

    def _template_appeal(self, context: dict[str, str], denial_reason: str) -> str:
        """
        Template appeal letter used when OpenAI is unavailable.

        Structured letter with placeholders the physician can fill in.
        """
        drug   = context.get("drug", "the medication")
        payer  = context.get("payer", "the payer")
        reason = denial_reason.replace("_", " ").title()
        met    = context.get("met_criteria", "clinical criteria documented in the medical record")

        return (
            f"Dear Medical Director, {payer},\n\n"
            f"We are writing to formally appeal the denial of prior authorization for "
            f"{drug}. The denial reason cited was: {reason}. We respectfully disagree "
            f"with this determination based on the clinical evidence presented below.\n\n"
            f"Clinical justification: {met}. This patient meets all applicable criteria "
            f"for this therapy as documented in the attached medical records. The prescribed "
            f"treatment is consistent with published clinical guidelines and represents the "
            f"medically necessary and appropriate standard of care for this patient's condition.\n\n"
            f"We respectfully request a reconsideration of this denial and are available "
            f"for a peer-to-peer review at your convenience. Please contact our office to "
            f"schedule this discussion.\n\n"
            f"Sincerely,\nAttending Physician"
        )
