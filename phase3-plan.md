# Phase 3 Plan — Clinical-Financial Bridge

> Keep this file in your workspace root alongside requirement.md and architecture.md.
> Point Claude Code to this file when starting Phase 3.

---

## Goal

Replace the Phase 2 stub card with real denial prediction logic. When the test harness fires an `order-select` hook for Ozempic, CFIP returns a card with a **real denial risk score** computed from actual evidence factors — clinical data, payer rules, claims history, and coverage.

## "Done" Criteria

- Hook handler returns a card with a real denial risk score (not hardcoded)
- Score is based on 4+ evidence factors (step therapy, clinical criteria, documentation, coverage)
- Payer rules are stored in SQLite and queried at runtime
- Missing Epic sandbox data is supplemented by a toggleable synthetic overlay
- Card indicator (info/warning/critical) reflects the actual risk level
- Tests validate scoring logic across different evidence combinations

---

## What Changes From Phase 2

```
Phase 2:  Hook received → return hardcoded "87% approval"
Phase 3:  Hook received → classify drug → check payer rules →
          fetch clinical data → score denial risk → estimate cost →
          compose intelligent card
```

This is the first time CFIP actually thinks. No agentic orchestration yet (Phase 5) — just a linear pipeline for one drug class (GLP-1 / Ozempic) with one payer (UHC).

---

## Deliverables (Build Order)

### D1: SQLite Database + Seed Payer Rules
**Files:** `app/data/db.py`, `app/data/seed_payer_rules.py`

Set up SQLite database and seed it with UHC's requirements for GLP-1 drugs.

**What gets seeded:**
- Step therapy requirement: metformin ≥90 days
- Clinical criteria: A1C ≥7.0%, BMI ≥30 (or ≥27 with comorbidity)
- Required documentation: recent A1C lab, BMI measurement, medication history
- Denial rate baseline for this drug class + payer
- Average PA processing time

**Database tables (minimal):**
```
payer_rules:
  id, payer_name, drug_class, rule_type, rule_key, rule_value, description

denial_patterns:
  id, payer_name, drug_class, denial_reason, frequency, recommendation
```

This is synthetic but realistic data — modeled on how real payer requirements work.

### D2: Drug Classifier
**File:** `app/rules/drug_classifier.py`

Takes a medication code or name from the hook context and returns a drug class.

**Drug classes:**
- `glp1` — Ozempic, Wegovy, Mounjaro, Trulicity
- `oncology` — Keytruda, Opdivo (Phase 4+)
- `pgx_sensitive` — Clopidogrel, Warfarin (Phase 4+)
- `standard` — Everything else

Phase 3 only needs `glp1` classification working. Implementation is a simple lookup dictionary — no AI, no NLP. Maps drug names and RxNorm codes to classes.

### D3: Payer Rules Engine
**File:** `app/rules/payer_rules.py`

Given a drug class and payer name, queries SQLite and returns PA requirements:
- What evidence is needed (list of criteria)
- What thresholds must be met (A1C ≥ 7.0, BMI ≥ 30, etc.)
- What documentation is required
- Known denial patterns for this combination

Returns a structured Pydantic model, not raw SQL rows.

### D4: Denial Risk Scorer
**File:** `app/rules/denial_scorer.py`

The core of Phase 3. Takes gathered evidence and scores approval probability.

**Scoring model (deterministic, weighted):**

| Factor | What It Checks | Points |
|--------|---------------|--------|
| Step therapy | Metformin ≥90 days in Rx history? | 25 |
| Clinical criteria | A1C ≥7.0% AND BMI ≥30? | 25 |
| Documentation | Required labs on file and recent (<6 months)? | 20 |
| Payer history | Past denials for similar orders with this payer? | 15 |
| Coverage status | Active insurance, drug on formulary? | 15 |

**Score interpretation:**
- 80-100: High approval likelihood → card indicator `info` (blue)
- 50-79: Moderate risk → card indicator `warning` (orange)
- 0-49: High denial risk → card indicator `critical` (red)

Every point traces back to a specific evidence factor. Fully auditable — no black box.

**The scorer also returns:**
- List of met criteria (with evidence references)
- List of unmet criteria (with what's missing)
- Suggested actions (e.g., "Complete metformin trial" or "Order A1C lab")

### D5: Clinical-Financial Bridge
**File:** `app/agents/denial_prediction.py`

A linear pipeline (not agentic yet) that orchestrates D2-D4:

```
1. Receive hook context + prefetch data
2. Extract medication from context → drug classifier (D2)
3. Extract payer from Coverage → payer rules engine (D3)
4. Gather clinical evidence:
   a. Check prefetch for Patient, MedicationRequest
   b. Fetch A1C from FHIR (Observation?code=A1C)
   c. Fetch BMI from FHIR (Observation?code=BMI)
   d. Fetch Rx history from FHIR (MedicationRequest?patient=X)
   e. Fetch Coverage details from FHIR
   f. Fall back to synthetic overlay if FHIR data missing
5. Run denial scorer (D4) with all evidence
6. Return structured result: DenialRiskResult
```

**DenialRiskResult model:**
```python
class DenialRiskResult:
    approval_probability: int        # 0-100
    risk_level: str                  # "low" / "moderate" / "high"
    cost_estimate_monthly: float     # e.g., 150.00
    evidence_summary: list[str]      # human-readable evidence lines
    met_criteria: list[str]          # what's satisfied
    unmet_criteria: list[str]        # what's missing
    suggested_actions: list[str]     # what to do next
    drug_class: str                  # "glp1"
    payer: str                       # "UHC"
```

### D6: Synthetic Data Overlay
**File:** `app/data/seed_synthetic.py`

Since Epic sandbox likely doesn't have perfect data for our demo, we create synthetic overlays for Scenario A (Ozempic/GLP-1):

```python
SCENARIO_A = {
    "patient_id": "erXuFYUfucBZaryVksYEcMg3",  # Derrick Lin
    "a1c": 7.5,
    "bmi": 33.0,
    "metformin_days": 180,           # 6 months
    "payer": "UnitedHealthcare",
    "plan": "UHC Choice Plus",
    "coverage_active": True,
    "cost_estimate_monthly": 150.00,
    "past_denials_similar": 0,
}
```

**Toggle mechanism:** A config flag `USE_SYNTHETIC_OVERLAY=true` in `.env`. When true, the bridge checks FHIR first, then fills gaps from synthetic data. When false, only real FHIR data is used. Clear separation — you always know what's real vs synthetic.

### D7: Card Composer
**File:** `app/intelligence/card_composer.py`

Takes a `DenialRiskResult` and composes a CDS Hooks card. Template-based (string formatting), no LLM call yet.

**Example output for Scenario A (all criteria met):**
```json
{
  "summary": "Ozempic: 87% Approval Probability | Est. $150/mo",
  "indicator": "info",
  "detail": "### Denial Risk Assessment\n**Approval probability: 87%**\n\n**Evidence:**\n- ✅ Step therapy met: metformin 180 days\n- ✅ Clinical criteria met: A1C 7.5%, BMI 33\n- ✅ Documentation complete\n- ✅ UHC coverage active\n\n**Estimated cost:** $150/mo copay",
  "source": { "label": "CFIP Clinical-Financial Intelligence" },
  "suggestions": [{ "label": "Submit PA Now" }],
  "links": [{ "label": "View Full Analysis", "type": "smart" }]
}
```

**Example output for missing step therapy:**
```json
{
  "summary": "Ozempic: 42% Approval — Step Therapy Not Met",
  "indicator": "critical",
  "detail": "### Denial Risk Assessment\n**Approval probability: 42%**\n\n**Evidence:**\n- ❌ Step therapy NOT met: no metformin history found\n- ✅ Clinical criteria met: A1C 7.5%, BMI 33\n- ⚠️ Documentation gap: medication history incomplete\n- ✅ UHC coverage active\n\n**Recommended actions:**\n- Document metformin trial (≥90 days required)\n- Consider starting metformin first",
  "source": { "label": "CFIP Clinical-Financial Intelligence" },
  "suggestions": [{ "label": "Document Metformin Trial" }],
  "links": [{ "label": "View Full Analysis", "type": "smart" }]
}
```

### D8: Wire Into Hook Handler
**File:** Update `app/api/cds_hooks.py`

Replace the stub card logic with the real pipeline:
```
hook request → extract context → denial_prediction bridge → card_composer → response
```

The handler should:
- Extract patientId and medication from hook context/prefetch
- Call the bridge pipeline
- Compose the card
- Return it
- Handle errors gracefully (if bridge fails, return an error card, not a 500)

### D9: Tests
**File:** `tests/test_denial_scorer.py` (and updates to `tests/test_cds_hooks.py`)

**Scorer tests:**
- All criteria met → score 80+, indicator "info"
- Step therapy missing → score drops by 25, indicator changes
- Multiple criteria missing → score below 50, indicator "critical"
- Edge cases: exactly at threshold (A1C = 7.0, metformin = 90 days)

**Bridge tests (with mocked FHIR):**
- Bridge produces valid DenialRiskResult from mock data
- Synthetic overlay fills gaps when FHIR returns no data
- Synthetic overlay is skipped when flag is false

**Card composer tests:**
- Produces spec-compliant card for each risk level
- Summary contains approval percentage
- Indicator matches risk level

---

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Synthetic data | Toggleable overlay via .env flag | Transparent, easy to demo, clear real vs fake |
| Cost estimation | Hardcoded per scenario | Cost estimation isn't the core differentiator; keeps Phase 3 focused |
| Scoring model | Deterministic weighted points (0-100) | Auditable, explainable — healthcare requirement |
| Drug classification | Simple lookup dictionary | Only need GLP-1 for Phase 3; no over-engineering |
| Card narrative | Template-based string formatting | LLM-generated narratives come in Phase 5 |
| Pipeline style | Linear (not agentic) | Agentic orchestration is Phase 5; prove the logic works first |

---

## Files Created/Modified in Phase 3

New files:
- `app/data/db.py`
- `app/data/seed_payer_rules.py`
- `app/data/seed_synthetic.py`
- `app/rules/drug_classifier.py`
- `app/rules/payer_rules.py`
- `app/rules/denial_scorer.py`
- `app/agents/denial_prediction.py`
- `app/intelligence/card_composer.py`
- `tests/test_denial_scorer.py`

Modified files:
- `app/api/cds_hooks.py` (replace stub with real pipeline)
- `app/config.py` (add USE_SYNTHETIC_OVERLAY flag)
- `app/models/domain.py` (add DenialRiskResult and related models)
- `.env` (add USE_SYNTHETIC_OVERLAY=true)

---

## How to Verify Phase 3 Is Working

1. Start CFIP server: `uvicorn app.main:app --reload`
2. Open test harness in browser: `http://localhost:8000/harness`
3. Select Scenario A (Ozempic)
4. Fire the hook
5. See a card with a real score (not hardcoded), evidence breakdown, and appropriate indicator color
6. Modify synthetic data (e.g., remove metformin history) → re-fire → see score drop and indicator turn red

---

## Prompt for Claude Code

Paste this when starting Phase 3 in CLI:

> Read requirement.md, architecture.md, and phase3-plan.md from this workspace. These are your ground truth.
>
> We're starting Phase 3 — Clinical-Financial Bridge. Follow the deliverable order in phase3-plan.md (D1 through D9).
>
> Key decisions already made:
> - Synthetic overlay approach (toggleable via USE_SYNTHETIC_OVERLAY in .env)
> - Hardcoded cost estimation per scenario
> - Deterministic weighted scoring model (not ML)
> - Template-based card composition (no LLM yet)
> - Linear pipeline (not agentic yet)
>
> Rules: One file at a time. Explain before writing. Wait for my approval before proceeding. Comment Python idioms that wouldn't be obvious to a C# developer.
