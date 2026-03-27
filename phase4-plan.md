# Phase 4 Plan — PGx CDS + Specialty PA

> Keep this file in your workspace root alongside requirement.md and architecture.md.
> Point Claude Code to this file when starting Phase 4.

---

## Goal

Add two new capabilities to CFIP: pharmacogenomic (PGx) safety checking and specialty prior authorization (PA) bundling. When the test harness fires a hook for clopidogrel, CFIP returns a PGx safety alert. When it fires for Ozempic, CFIP returns the denial risk card plus a PA-ready bundle. The drug classifier routes to the correct pipeline.

## "Done" Criteria

- Scenario B (clopidogrel): card shows PGx safety alert — "CYP2C19 poor metabolizer, drug ineffective, consider prasugrel"
- Scenario A (Ozempic): card shows denial risk score + PA bundle status ("PA ready to submit" or "requirements incomplete")
- Drug classifier routes clopidogrel → PGx pipeline, Ozempic → denial + PA pipeline
- "No PGx data" case returns a "recommend testing" card (not silence)
- "Run All" button in harness fires both scenarios and shows both cards
- Tests validate PGx engine, PA builder, and routing logic

---

## Background: What Are CPIC and PGx?

### PGx (Pharmacogenomics)

Your genes affect how your body processes drugs. Liver enzymes (coded by genes) metabolize most medications. Gene variants can make a drug ineffective, dangerously strong, or cause severe side effects at normal doses.

About 90% of people carry at least one actionable PGx variant. This isn't rare — it's underdiagnosed.

**Example:** Clopidogrel (blood thinner) is a prodrug — inactive until your liver enzyme CYP2C19 converts it. If you're a CYP2C19 poor metabolizer, the drug never activates. You're taking a sugar pill while thinking you're protected from a heart attack.

### CPIC (Clinical Pharmacogenomics Implementation Consortium)

CPIC publishes peer-reviewed guidelines for drug-gene pairs:

```
Drug: clopidogrel    Gene: CYP2C19    Status: poor metabolizer
→ Action: Don't use. Prescribe prasugrel or ticagrelor instead.

Drug: warfarin    Genes: CYP2C9 + VKORC1    Status: poor metabolizer
→ Action: Reduce dose by 50%.
```

These are not AI predictions — they're evidence-graded clinical rules published in medical journals. Hospitals follow CPIC guidelines when implementing PGx programs.

**Website:** https://cpicpgx.org — all guidelines are freely available.

CFIP implements CPIC rules as a deterministic Python rule engine — auditable, traceable, same answer every time. Patient safety logic must never depend on an LLM.

### How CFIP Handles the Three PGx Scenarios

**PGx data exists + interaction found:**
```
Card: "⚠️ CYP2C19 Poor Metabolizer — clopidogrel ineffective. Use prasugrel."
Indicator: critical
```

**No PGx data on file (the common case — most hospitals don't have PGx data yet):**
```
Card: "Clopidogrel: No PGx data on file — testing recommended.
       CYP2C19 status affects clopidogrel effectiveness.
       Consider ordering PGx panel before starting therapy."
       [Order PGx Panel]
Indicator: warning
```

**PGx data exists + no interaction:**
```
No PGx card shown. Drug is safe for this patient's genome.
```

---

## Deliverables (Build Order)

### D1: CPIC Knowledge Base + Seed Data
**Files:** `app/data/seed_cpic.py`, update `app/data/db.py`

Seed SQLite with real CPIC drug-gene interaction data.

**Database table:**
```sql
cpic_rules:
  id              INTEGER PRIMARY KEY
  drug_name       TEXT        -- "clopidogrel", "warfarin"
  gene            TEXT        -- "CYP2C19", "CYP2C9"
  diplotype_pattern TEXT      -- "*2/*2", "*1/*2" (regex-friendly)
  metabolizer_status TEXT     -- "poor_metabolizer", "intermediate", "normal", "rapid"
  recommendation  TEXT        -- "Use alternative antiplatelet agent"
  alternative_drug TEXT       -- "prasugrel" (nullable)
  severity        TEXT        -- "high", "moderate", "low"
  evidence_level  TEXT        -- "1A", "1B", "2A" (CPIC evidence grading)
```

**Seed data for Phase 4:**

Clopidogrel + CYP2C19:
| Diplotype | Status | Recommendation | Alternative | Severity |
|-----------|--------|---------------|-------------|----------|
| *2/*2 | Poor metabolizer | Use alternative | prasugrel, ticagrelor | High |
| *2/*3 | Poor metabolizer | Use alternative | prasugrel, ticagrelor | High |
| *1/*2 | Intermediate | Consider alternative or monitor closely | prasugrel | Moderate |
| *1/*1 | Normal | Use as prescribed | — | Low |
| *1/*17 | Rapid | Use as prescribed | — | Low |
| *17/*17 | Ultrarapid | Use as prescribed | — | Low |

Warfarin + CYP2C9/VKORC1 (seed now, use in future):
| Gene | Status | Recommendation | Severity |
|------|--------|---------------|----------|
| CYP2C9 *1/*3 | Intermediate | Reduce dose 25% | Moderate |
| CYP2C9 *2/*2 | Poor | Reduce dose 50% | High |
| CYP2C9 *3/*3 | Poor | Reduce dose 50% | High |

This is real clinical data from published CPIC guidelines — not synthetic.

### D2: PGx Safety Engine
**File:** `app/rules/cpic_engine.py`

Core PGx checking logic. Given a drug and genomic data, returns interaction results.

**Interface:**
```python
class PgxResult(BaseModel):
    has_interaction: bool
    gene: str | None
    metabolizer_status: str | None        # "poor_metabolizer", "normal", etc.
    diplotype: str | None                 # "*2/*2"
    recommendation: str
    alternative_drug: str | None
    severity: str                         # "high", "moderate", "low", "none"
    evidence_level: str | None            # "1A", "1B"
    pgx_data_available: bool              # False = no genomic data on file

# Usage:
result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
# → PgxResult(has_interaction=True, severity="high", ...)

result = check_pgx(drug="clopidogrel", genomic_data=None)
# → PgxResult(has_interaction=False, pgx_data_available=False,
#             recommendation="No PGx data on file. Recommend CYP2C19 testing.")

result = check_pgx(drug="ozempic", genomic_data=None)
# → PgxResult(has_interaction=False, severity="none",
#             recommendation="No PGx checking required for this drug")
```

All logic is deterministic CPIC rules. No AI, no LLM.

### D3: PGx Synthetic Overlay
**File:** Update `app/data/seed_synthetic.py`

Add Scenario B data:
```python
SCENARIO_B = {
    "patient_id": "erXuFYUfucBZaryVksYEcMg3",
    "scenario_name": "PGx Safety Alert",
    "drug": "clopidogrel",
    "drug_class": "pgx_sensitive",
    "pgx_data": {
        "CYP2C19": {
            "diplotype": "*2/*2",
            "metabolizer_status": "poor_metabolizer",
            "source": "synthetic_overlay"
        }
    },
    "payer": "Aetna",
    "plan": "Aetna PPO",
    "coverage_active": True,
    "cost_estimate_monthly": 15.00,    # generic drug, cheap
}
```

### D4: PGx Agent (Linear Pipeline)
**File:** `app/agents/pgx_safety.py`

Linear pipeline for PGx-sensitive drugs:

```
1. Receive drug info from hook context
2. Check FHIR for genomic Observations:
   GET Observation?patient={id}&category=genomics&code={gene_code}
3. If no FHIR data → check synthetic overlay (if enabled)
4. Run CPIC engine (D2) with drug + genomic data (or None)
5. Return PgxResult
```

Handles three outcomes:
- PGx data found + interaction → safety alert
- PGx data found + no interaction → all clear (no card needed)
- No PGx data → recommend testing

### D5: Specialty PA Builder
**File:** `app/agents/specialty_pa.py`

Assembles PA documentation from the denial pipeline results (Phase 3).

**Model:**
```python
class PABundle(BaseModel):
    drug: str
    drug_class: str
    patient_summary: dict               # name, DOB, diagnoses
    clinical_evidence: list[EvidenceItem]  # A1C, BMI, step therapy proof
    payer_requirements: list[str]        # what the payer needs
    requirements_met: list[str]          # satisfied criteria
    requirements_unmet: list[str]        # missing criteria
    ready_to_submit: bool               # all requirements met?
    supporting_documents: list[str]      # what to attach
    appeal_notes: str | None            # if prior denial exists

class EvidenceItem(BaseModel):
    criterion: str                       # "Step therapy: metformin >= 90 days"
    met: bool
    value: str                           # "metformin 180 days"
    source: str                          # "FHIR MedicationRequest" or "synthetic"
```

For Phase 4, this creates the structured bundle. Actual FHIR Claim/$submit comes in Phase 5+.

### D6: Update Drug Classifier
**File:** Update `app/rules/drug_classifier.py`

Add PGx-sensitive drugs:
```python
DRUG_CLASSES = {
    # GLP-1 (existing from Phase 3)
    "ozempic": "glp1",
    "semaglutide": "glp1",
    "wegovy": "glp1",
    "mounjaro": "glp1",
    "tirzepatide": "glp1",
    "trulicity": "glp1",
    "dulaglutide": "glp1",

    # PGx-sensitive (new)
    "clopidogrel": "pgx_sensitive",
    "plavix": "pgx_sensitive",
    "warfarin": "pgx_sensitive",
    "coumadin": "pgx_sensitive",
}
```

### D7: Card Composer Updates
**File:** Update `app/intelligence/card_composer.py`

Add three new card templates:

**PGx safety alert (interaction found):**
- Summary: "⚠️ Clopidogrel: CYP2C19 Poor Metabolizer — Drug Ineffective"
- Indicator: critical
- Detail: gene, status, impact explanation, recommendation, CPIC evidence level
- Suggestions: "Switch to Prasugrel", "Switch to Ticagrelor"

**PGx data missing (recommend testing):**
- Summary: "Clopidogrel: No PGx Data — Testing Recommended"
- Indicator: warning
- Detail: which gene matters, why it matters for this drug, what to order
- Suggestions: "Order PGx Panel"

**PA-ready (enhanced Scenario A):**
- Summary: "Ozempic: 87% Approval | PA Ready to Submit"
- Indicator: info
- Detail: evidence breakdown + PA bundle status
- Suggestions: "Submit PA Now", "Review PA Bundle"

### D8: Route Drug Classes in Hook Handler
**File:** Update `app/api/cds_hooks.py`

Replace single-pipeline logic with drug-class routing:

```python
drug_class = classify_drug(medication)

if drug_class == "glp1":
    denial_result = await run_denial_pipeline(context, prefetch)
    pa_bundle = await build_pa_bundle(denial_result, context)
    card = compose_glp1_card(denial_result, pa_bundle)

elif drug_class == "pgx_sensitive":
    pgx_result = await run_pgx_pipeline(context, prefetch)
    card = compose_pgx_card(pgx_result)

else:
    # Generic card for unclassified drugs
    card = compose_generic_card(medication, context)
```

This is a simple if/else router. Phase 5 replaces it with the agentic orchestrator.

### D9: Update Test Harness — Run All
**File:** Update `tools/cds_hooks_harness/scenarios.py` and `static/index.html`

- Add Scenario B (clopidogrel) to scenario definitions
- Add "Run All Scenarios" button to the harness UI
- Display both card responses side by side
- Each card shows with its correct indicator color

### D10: Tests
**Files:** `tests/test_pgx_engine.py`, updates to existing tests

**PGx engine tests:**
- Clopidogrel + poor metabolizer (*2/*2) → high severity alert, alternative = prasugrel
- Clopidogrel + intermediate (*1/*2) → moderate severity, consider alternative
- Clopidogrel + normal (*1/*1) → no interaction
- Clopidogrel + no PGx data → pgx_data_available=False, recommend testing
- Ozempic (not PGx-sensitive) → no PGx check needed
- Unknown drug → no PGx check needed

**PA builder tests:**
- All denial criteria met → ready_to_submit = True, bundle complete
- Missing criteria → ready_to_submit = False, unmet list populated
- Bundle contains correct evidence items

**Routing tests:**
- Ozempic hook → denial card + PA action
- Clopidogrel hook → PGx safety card
- Clopidogrel + no PGx data → "recommend testing" card
- Unknown drug → generic card

---

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| CPIC data | Real clinical guidelines (not synthetic) | These are published, peer-reviewed rules — we use the real ones |
| PGx engine | Deterministic rules, no AI | Patient safety must be auditable and reproducible |
| PA submission | Structured bundle only (no FHIR Claim/$submit) | Actual submission comes in Phase 5 with the orchestrator |
| Missing PGx data handling | Show "recommend testing" card | Common case (most hospitals lack PGx data); provides clear next step |
| Harness UI | "Run All" button (no individual dropdown) | Builder always wants to see all scenarios together |
| Drug routing | Simple if/else | Agentic orchestrator in Phase 5 replaces this |

---

## Files Created/Modified in Phase 4

New files:
- `app/data/seed_cpic.py`
- `app/rules/cpic_engine.py`
- `app/agents/pgx_safety.py`
- `app/agents/specialty_pa.py`
- `tests/test_pgx_engine.py`

Modified files:
- `app/data/db.py` (add cpic_rules table)
- `app/data/seed_synthetic.py` (add Scenario B)
- `app/rules/drug_classifier.py` (add pgx_sensitive class)
- `app/intelligence/card_composer.py` (add PGx and PA card templates)
- `app/api/cds_hooks.py` (drug-class routing)
- `app/models/domain.py` (add PgxResult, PABundle, EvidenceItem)
- `tools/cds_hooks_harness/scenarios.py` (add Scenario B)
- `tools/cds_hooks_harness/static/index.html` (Run All button)

---

## How to Verify Phase 4 Is Working

1. Start CFIP server: `uvicorn app.main:app --reload`
2. Open test harness: `http://localhost:8000/harness`
3. Click "Run All Scenarios"
4. See two cards:
   - **Ozempic card (blue/info):** "87% Approval | PA Ready to Submit" with evidence and PA bundle status
   - **Clopidogrel card (red/critical):** "CYP2C19 Poor Metabolizer — Drug Ineffective" with alternative suggestion
5. Modify Scenario B synthetic data to remove PGx data → re-run → clopidogrel card changes to warning: "No PGx Data — Testing Recommended"

---

## Prompt for Claude Code

Paste this when starting Phase 4 in CLI:

> Read requirement.md, architecture.md, and phase4-plan.md from this workspace. These are your ground truth.
>
> We're starting Phase 4 — PGx CDS + Specialty PA. Follow the deliverable order in phase4-plan.md (D1 through D10).
>
> Key decisions already made:
> - CPIC data is real clinical guidelines (from cpicpgx.org), not synthetic
> - PGx engine is deterministic rules only (no AI/LLM)
> - PA builder creates a structured bundle, not a FHIR Claim submission
> - Missing PGx data shows a "recommend testing" card
> - Harness gets a "Run All" button (no individual scenario dropdown)
> - Drug routing is simple if/else (agentic orchestrator comes in Phase 5)
>
> Rules: One file at a time. Explain before writing. Wait for my approval before proceeding. Comment Python idioms that wouldn't be obvious to a C# developer.
