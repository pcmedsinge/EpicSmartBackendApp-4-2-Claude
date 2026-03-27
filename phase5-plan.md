# Phase 5 Plan — Agentic Orchestrator

> Keep this file in your workspace root alongside requirement.md and architecture.md.
> Point Claude Code to this file when starting Phase 5.

---

## Goal

Replace the Phase 4 if/else routing with a single orchestrator that dynamically selects evidence chains based on drug class + payer + patient context. Add OpenAI-generated narratives. All 4 demo scenarios (A/B/C/D) work through one orchestrator.

## "Done" Criteria

- All 4 scenarios produce intelligent cards through a single orchestrator (no if/else routing)
- Orchestrator uses configured evidence chains (deterministic, auditable)
- OpenAI generates human-readable card narratives and appeal letter drafts
- Template fallback works if OpenAI is unavailable
- Scenarios C (Keytruda/oncology) and D (MRI/denial prevention) are working
- Test harness "Run All" shows all 4 scenario cards side by side
- Tests validate orchestrator routing, chain execution, and LLM fallback

---

## What Changes From Phase 4

```
Phase 4 (if/else routing):
  Hook → drug classifier →
    if glp1:         denial pipeline + PA builder
    elif pgx:        PGx safety engine
    else:            generic card

Phase 5 (orchestrator):
  Hook → orchestrator.process() →
    1. PLAN:    classify drug → look up evidence chain config
    2. EXECUTE: run each step in the chain, adapt based on findings
    3. VERIFY:  check evidence completeness, fill gaps
    4. COMPOSE: generate narrative (OpenAI) → build card
```

The orchestrator calls the same components from Phases 3-4 (denial scorer, PGx engine, PA builder) but decides which ones to call and in what order based on the drug class and what it discovers along the way.

---

## Architecture Decision: Why Not LangGraph/LangChain

**Considered:** LangGraph for graph-based workflow management.

**Decision:** Pure Python orchestrator for Phase 5, designed for LangGraph migration later.

**Rationale:**
- Our evidence chains are 5-7 steps each — a Python class with async methods handles this cleanly
- LangGraph pulls in LangChain as dependency (~50+ packages), heavy for 8GB RAM
- Our steps are deterministic clinical logic, not LLM-driven decisions — LangGraph's value is strongest when the LLM decides the flow
- Healthcare requirement: auditable, traceable decisions. Custom code is easier to audit than framework abstractions

**Migration path:** Each orchestrator step is an independent async function with typed Pydantic inputs/outputs. If CFIP grows to 10+ drug classes, wrapping these functions as LangGraph nodes is a few hours of wiring — the logic doesn't change, only the execution framework.

**When to reconsider:** If we need graph visualization for debugging, complex parallel execution, or human-in-the-loop approval steps — these are LangGraph strengths.

---

## Evidence Chain Architecture

The orchestrator is driven by configuration, not code branching. Each drug class has a defined chain:

```python
EVIDENCE_CHAINS = {
    "glp1": {
        "name": "GLP-1 Prior Authorization",
        "steps": [
            "fetch_labs",           # A1C, BMI from FHIR/synthetic
            "fetch_rx_history",     # Medication history
            "fetch_coverage",       # Insurance details
            "check_step_therapy",   # Metformin >= 90 days?
            "check_clinical_criteria",  # A1C >= 7.0, BMI >= 30?
            "score_denial_risk",    # 5-factor weighted score
            "build_pa_bundle",      # Assemble PA documentation
            "generate_narrative",   # OpenAI summary
        ],
        "pgx_check": False,
    },
    "oncology": {
        "name": "Oncology Pathway Validation",
        "steps": [
            "fetch_condition",      # Tumor type from FHIR/synthetic
            "fetch_biomarkers",     # PD-L1, genomic markers
            "fetch_prior_regimens", # Previous treatment history
            "validate_nccn_pathway",# Does evidence support this drug?
            "build_pa_bundle",      # Complex oncology PA
            "generate_narrative",   # OpenAI summary
        ],
        "pgx_check": False,
    },
    "pgx_sensitive": {
        "name": "Pharmacogenomic Safety Check",
        "steps": [
            "fetch_pgx_data",      # Genomic observations
            "check_cpic",          # CPIC rule lookup
            "suggest_alternative", # If interaction found
        ],
        "pgx_check": True,
        "note": "PGx cards use templates only — no LLM for safety-critical content"
    },
    "standard": {
        "name": "Denial Prevention",
        "steps": [
            "fetch_claims_history", # Past ExplanationOfBenefit
            "pattern_match_denials",# Similar past denials?
            "check_documentation",  # Required docs on file?
            "score_denial_risk",    # Weighted score
            "generate_narrative",   # OpenAI summary
        ],
        "pgx_check": False,
    },
}
```

**How the orchestrator adapts:** After each step, the orchestrator checks the result. If `fetch_pgx_data` returns no data, it skips `check_cpic` and goes to `suggest_testing`. If `check_step_therapy` finds metformin history is only 60 days, the denial score adjusts and the narrative mentions it. The chain is configured, but execution is dynamic.

---

## The Four Demo Scenarios Through the Orchestrator

### Scenario A — Ozempic (GLP-1 PA)
```
Orchestrator → plan: "glp1" chain
  → fetch_labs: A1C=7.5, BMI=33 ✓
  → fetch_rx_history: metformin 180 days ✓
  → fetch_coverage: UHC active ✓
  → check_step_therapy: met (180 >= 90 days) ✓
  → check_clinical_criteria: met (A1C>=7.0, BMI>=30) ✓
  → score_denial_risk: 87% approval
  → build_pa_bundle: all requirements met, ready to submit
  → generate_narrative: OpenAI writes clinical summary
Card: "Ozempic: 87% Approval | PA Ready | $150/mo"
```

### Scenario B — Clopidogrel (PGx Safety)
```
Orchestrator → plan: "pgx_sensitive" chain
  → fetch_pgx_data: CYP2C19 *2/*2 found
  → check_cpic: POOR METABOLIZER — drug ineffective
  → suggest_alternative: prasugrel, ticagrelor
Card: "⚠️ CYP2C19 Poor Metabolizer — Switch to Prasugrel"
(Template only — no LLM for safety alerts)
```

### Scenario C — Keytruda (Oncology PA) [NEW]
```
Orchestrator → plan: "oncology" chain
  → fetch_condition: Non-small cell lung cancer (C34.1)
  → fetch_biomarkers: PD-L1 score 80% (positive)
  → fetch_prior_regimens: carboplatin + pemetrexed (completed)
  → validate_nccn_pathway: Keytruda approved for PD-L1+ NSCLC ✓
  → build_pa_bundle: biomarker + pathology + prior regimen documented
  → generate_narrative: OpenAI writes clinical summary
Card: "Keytruda: NCCN Pathway Validated | PA Bundle Ready"
```

### Scenario D — MRI (Denial Prevention) [NEW]
```
Orchestrator → plan: "standard" chain
  → fetch_claims_history: 2 past MRI denials with Aetna
  → pattern_match_denials: "insufficient documentation" pattern
  → check_documentation: GAPS — missing PT records, X-ray results
  → score_denial_risk: 35% approval (high risk)
  → generate_narrative: OpenAI writes risk summary
Card: "⚠️ MRI: High Denial Risk (35%) — Documentation Gaps Found"
```

---

## Deliverables (Build Order)

### D1: Orchestrator Framework
**File:** `app/agents/orchestrator.py`

Core orchestrator class with plan-execute-verify-compose loop.

```python
class AgentResult(BaseModel):
    drug: str
    drug_class: str
    chain_name: str
    denial_risk: DenialRiskResult | None
    pgx_result: PgxResult | None
    pa_bundle: PABundle | None
    narrative: str                    # LLM-generated or template
    narrative_source: str             # "openai" or "template"
    evidence_chain_log: list[str]     # audit trail of what ran
    cards: list[Card]                 # composed CDS cards

class Orchestrator:
    async def process(self, hook_context, prefetch) -> AgentResult:
        # 1. PLAN
        drug_class = classify_drug(medication)
        chain = EVIDENCE_CHAINS[drug_class]

        # 2. EXECUTE
        evidence = {}
        chain_log = []
        for step in chain["steps"]:
            result = await self.execute_step(step, hook_context, prefetch, evidence)
            evidence[step] = result
            chain_log.append(f"{step}: {result.summary}")

        # 3. VERIFY
        gaps = self.check_evidence_completeness(evidence, chain)
        if gaps:
            chain_log.append(f"Evidence gaps: {gaps}")

        # 4. COMPOSE
        narrative = await self.generate_narrative(evidence, drug_class)
        cards = self.compose_cards(evidence, narrative, drug_class)

        return AgentResult(...)
```

**Audit trail:** Every orchestrator run produces a `chain_log` — a list of what steps ran, what they found, and what decisions were made. This is the audit trail for regulators.

### D2: Evidence Chain Definitions
**File:** `app/agents/evidence_chains.py`

Configuration file defining evidence chains per drug class (see architecture section above). Each step maps to an async function in the orchestrator.

### D3: Oncology Support (Scenario C)
**Files:**
- Update `app/data/seed_payer_rules.py` — add oncology payer rules (NCCN pathway, biomarker requirements)
- Update `app/data/seed_synthetic.py` — add Scenario C data (Keytruda, NSCLC, PD-L1+)
- Update `app/rules/drug_classifier.py` — add oncology drugs
- New: `app/rules/nccn_validator.py` — simple NCCN pathway validation (tumor type + biomarker + drug → approved/not)

**NCCN validation is simplified:** We're not implementing the full NCCN database. We seed a few validated pathways (e.g., "pembrolizumab + NSCLC + PD-L1 >= 50% → approved") as rules in SQLite. Enough for the demo.

### D4: Denial Prevention Support (Scenario D)
**Files:**
- Update `app/data/seed_synthetic.py` — add Scenario D data (MRI, past denials, documentation gaps)
- Update `app/rules/drug_classifier.py` — add standard procedure classification
- Update `app/rules/denial_scorer.py` — handle procedures (not just drugs), weight past denial patterns heavily

**Pattern matching logic:** When CFIP sees past denials for similar procedures with the same payer, it:
1. Identifies the denial reasons (documentation gaps, clinical necessity)
2. Checks if those gaps still exist
3. Flags them before submission

### D5: OpenAI Client
**File:** `app/intelligence/openai_client.py`

Async wrapper around the OpenAI API:

```python
class OpenAIClient:
    async def generate_narrative(self, evidence: dict, drug_class: str) -> str:
        """Generate human-readable card narrative from evidence."""

    async def generate_appeal_letter(self, evidence: dict, denial_reason: str) -> str:
        """Generate appeal letter draft for a denial."""

    async def is_available(self) -> bool:
        """Check if OpenAI API is reachable."""
```

**Prompts are carefully crafted:** The narrative prompt includes instructions to be concise, factual, and clinician-friendly. The appeal letter prompt structures the output as a formal letter to a medical director.

**Model:** gpt-4o-mini (from .env config). Fast, cheap, good enough for narrative generation.

**Timeout:** 5-second timeout on OpenAI calls. If it takes longer, fall back to template.

### D6: Card Composer Overhaul
**File:** Update `app/intelligence/card_composer.py`

The composer now:
- Accepts an `AgentResult` from the orchestrator
- Uses LLM narrative if available, template if not
- Handles all 4 scenario types
- PGx safety alerts ALWAYS use templates (never LLM for safety-critical content)
- Includes `narrative_source` indicator so you can tell what generated the text

### D7: Appeal Letter Generator
**File:** `app/intelligence/appeal_generator.py`

When denial risk is high (score < 50) or a past denial exists, CFIP generates an appeal letter draft:

```python
class AppealGenerator:
    async def generate(self, evidence, denial_context) -> AppealLetter:
        # Try OpenAI first
        letter = await openai_client.generate_appeal_letter(evidence, denial_context)
        if not letter:
            letter = self.template_appeal(evidence, denial_context)
        return AppealLetter(
            content=letter,
            source="openai" or "template",
            addressed_to="Medical Director",
            evidence_references=[...],
        )
```

The appeal letter is included as a link on the card: "View Appeal Draft" — this becomes part of the SMART companion app content in Phase 6.

### D8: Wire Orchestrator Into Hook Handler
**File:** Update `app/api/cds_hooks.py`

Replace the if/else router:

```python
# Old:
drug_class = classify_drug(medication)
if drug_class == "glp1": ...
elif drug_class == "pgx_sensitive": ...

# New:
orchestrator = Orchestrator()
result = await orchestrator.process(hook_context, prefetch)
return CdsResponse(cards=result.cards)
```

### D9: Update Test Harness
**Files:** Update `tools/cds_hooks_harness/scenarios.py` and `static/index.html`

- Add Scenario C (Keytruda) and Scenario D (MRI)
- "Run All" fires all 4 scenarios
- Display 4 cards with appropriate colors:
  - A (Ozempic): blue/info — "87% Approval, PA Ready"
  - B (Clopidogrel): red/critical — "PGx Safety Alert"
  - C (Keytruda): blue/info — "NCCN Pathway Validated"
  - D (MRI): orange/warning — "High Denial Risk, Documentation Gaps"

### D10: Tests
**Files:** `tests/test_orchestrator.py`, updates to existing tests

- Orchestrator selects correct chain for each drug class
- Each chain produces correct evidence and result type
- OpenAI failure triggers template fallback gracefully
- Appeal letter generates for high-risk scenarios
- All 4 scenarios produce spec-compliant cards
- Audit chain_log captures every step executed

---

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Orchestrator framework | Pure Python, not LangGraph/LangChain | Lightweight, auditable, 8GB RAM constraint. Designed for LangGraph migration if needed later. |
| Agentic approach | Configured chains (deterministic) | Healthcare requires auditable decisions. LLM decides nothing about clinical flow. |
| OpenAI usage | Required for demo, template fallback | LLM narratives are a product selling point, but system must never fail due to API issues |
| PGx narrative | Always template, never LLM | Safety-critical content must be deterministic and reproducible |
| NCCN validation | Simplified lookup table | Full NCCN database is out of scope; seed enough pathways for demo |
| Appeal letters | Generated by OpenAI with template fallback | Demonstrates AI value-add; included as card link for SMART app |

---

## Files Created/Modified in Phase 5

New files:
- `app/agents/orchestrator.py`
- `app/agents/evidence_chains.py`
- `app/intelligence/openai_client.py`
- `app/intelligence/appeal_generator.py`
- `app/rules/nccn_validator.py`
- `tests/test_orchestrator.py`

Modified files:
- `app/api/cds_hooks.py` (replace router with orchestrator)
- `app/intelligence/card_composer.py` (handle all 4 scenarios + LLM narratives)
- `app/rules/drug_classifier.py` (add oncology + standard procedures)
- `app/rules/denial_scorer.py` (handle procedures + past denial weighting)
- `app/data/seed_payer_rules.py` (add oncology rules)
- `app/data/seed_synthetic.py` (add Scenarios C + D)
- `app/models/domain.py` (add AgentResult, AppealLetter, etc.)
- `tools/cds_hooks_harness/scenarios.py` (add Scenarios C + D)
- `tools/cds_hooks_harness/static/index.html` (4-card display)
- `.env` (verify OPENAI_API_KEY and OPENAI_MODEL are set)

---

## How to Verify Phase 5 Is Working

1. Ensure `.env` has a valid `OPENAI_API_KEY` and `OPENAI_MODEL=gpt-4o-mini`
2. Start CFIP server: `uvicorn app.main:app --reload`
3. Open test harness: `http://localhost:8000/harness`
4. Click "Run All Scenarios"
5. See 4 cards:
   - A (Ozempic): blue — approval score, PA ready, LLM-generated narrative
   - B (Clopidogrel): red — PGx safety alert, template text
   - C (Keytruda): blue — NCCN validated, PA bundle ready, LLM narrative
   - D (MRI): orange — denial risk, documentation gaps, LLM narrative
6. Check narrative quality — LLM text should be concise and clinical
7. Disable OpenAI key in .env → restart → re-run → cards still work with template text
8. Check "Raw JSON response" — verify `narrative_source` field shows "openai" or "template"

---

## Prompt for Claude Code

Paste this when starting Phase 5 in CLI:

> Read requirement.md, architecture.md, and phase5-plan.md from this workspace. These are your ground truth.
>
> We're starting Phase 5 — Agentic Orchestrator. Follow the deliverable order in phase5-plan.md (D1 through D10).
>
> Key decisions already made:
> - Pure Python orchestrator (no LangGraph/LangChain) — designed for future migration
> - Configured evidence chains (deterministic, auditable) — LLM does NOT decide clinical flow
> - OpenAI required for demo narratives, template fallback if API unavailable
> - PGx safety alerts always use templates (never LLM for safety-critical content)
> - Simplified NCCN validation (lookup table, not full database)
> - 5-second timeout on OpenAI calls, graceful fallback
>
> Rules: One file at a time. Explain before writing. Wait for my approval before proceeding. Comment Python idioms that wouldn't be obvious to a C# developer.
