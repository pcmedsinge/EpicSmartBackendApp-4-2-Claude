# CFIP — Clinical-Financial Intelligence Platform

> **Session startup:** Paste this file AND `architecture.md` at the start of every new session.
> This file = decisions, scope, contract. Architecture.md = diagrams, structure, flows.

## 1. Project Vision

CFIP is an **Agentic SMART on FHIR Backend Service** that bridges clinical decision support, revenue cycle intelligence, and precision medicine into a single platform operating on the Epic EHR ecosystem.

It occupies a genuine market white space: no existing product simultaneously evaluates clinical appropriateness, financial viability (denial risk, coverage, cost), genomic safety (pharmacogenomics), and prior authorization requirements — at the point of care, within the clinician's ordering workflow.

**One-line pitch:** "When a clinician orders something, CFIP tells them if it will be approved, what it will cost, whether it's safe for this patient's genome, and submits the prior auth — before they finish signing."

---

## 2. Technical Decisions (Settled)

These decisions are final. Do not revisit or suggest alternatives.

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python 3.12+ | Builder's AI expertise is in Python; best LLM/agent ecosystem |
| Web Framework | FastAPI | Async-native, auto OpenAPI docs, lightweight on 8GB RAM |
| FHIR Client | `fhirclient` (SMART Health IT) + `httpx` | Official SMART library + async HTTP |
| Auth | PyJWT + cryptography | JWT signing for SMART Backend Services OAuth 2.0 |
| LLM Integration | OpenAI API (gpt-4o-mini) | Builder already has working key; fast and cost-effective for NL generation |
| Clinical Rules | Deterministic Python rule engine | Auditable, traceable — healthcare requirement |
| Data Store | SQLite (dev) → PostgreSQL (prod) | Payer rules cache, denial patterns, PGx knowledge base |
| EHR Target | Epic Sandbox (fhir.epic.com) | FHIR R4 with synthetic data |
| Testing | pytest + respx (HTTP mocking) | Standard Python testing stack |
| Dev Machine | Windows, 8GB RAM | All tooling must respect this constraint |
| IDE | Builder's choice (VS Code recommended) | With Python extension |

---

## 3. Architecture Overview

> **Full diagrams:** See `architecture.md` for all Mermaid diagrams (system context, CDS Hooks flow, agentic decision tree, OAuth flow, solution structure, FHIR relationships, test harness, deployment view).

### 3.1 Integration Surfaces

CFIP has **one backend** with **three UI surfaces**:

**Backend (the product):**
- SMART Backend Service (OAuth 2.0 client credentials + JWT)
- CDS Hooks Service (REST endpoints responding to Epic hook events)
- Async agent workers (PA monitoring, denial appeals, rule updates)

**Surface 1 — CDS Hooks Cards (inside EHR):**
- Rendered natively by Epic Hyperspace
- Clinician sees cards with denial risk, cost, PGx alerts, PA actions
- For demo: we build a CDS Hooks test harness that simulates Epic

**Surface 2 — SMART Companion App (launched from cards):**
- Lightweight web UI launched via `links` in CDS card with `type: "smart"`
- Shows full evidence chain, PGx gene-drug map, PA tracker, appeal preview
- Uses EHR launch flow (SMART on FHIR)

**Surface 3 — Operations Dashboard (standalone):**
- Separate web app for revenue cycle leaders / C-suite
- Aggregate analytics: denial trends, PA rates, cost savings, PGx stats
- Sells the ROI story

### 3.2 Core Components

1. **FHIR Gateway** — All Epic FHIR R4 communication, OAuth token lifecycle
2. **CDS Hooks Engine** — Discovery endpoint + three hook handlers (patient-view, order-select, order-sign)
3. **Clinical-Financial Bridge** — Fuses clinical + claims data for denial prediction, cost transparency, alternatives
4. **Pharmacogenomics CDS** — Drug-gene interaction checking via CPIC guidelines + FHIR Genomics
5. **Specialty PA Optimizer** — Complex PA for GLP-1s, biologics, oncology with pathway intelligence
6. **Agentic Orchestrator** — Multi-step reasoning pipeline that chains components 2-5 dynamically per order context

### 3.3 Why Agentic Is Structurally Required (Not Buzzword)

Each drug-class + payer + patient combination triggers a different evidence-gathering chain. Example:
- Ozempic order → check BMI → check A1C → verify metformin trial → check PGx → check coverage → submit PA
- Keytruda order → check tumor type → verify PD-L1 biomarker → check prior regimens → validate NCCN pathway → submit PA
- Warfarin order → check CYP2C9/VKORC1 genotype → calculate adjusted dose → safety alert (no PA needed)

A static rule engine cannot handle this combinatorial explosion. The agent dynamically decides what data to fetch and what action to take based on intermediate results.

**Hybrid AI architecture:**
- **Deterministic rule engine** (Python) = clinical rules (CPIC, payer requirements, denial scoring) — auditable, traceable
- **OpenAI API** (external, gpt-4o-mini) = natural language generation (appeal letters, card narratives), complex reasoning for edge cases

---

## 4. FHIR Resource Map

| FHIR Resource | Operation | Component | Purpose |
|---------------|-----------|-----------|---------|
| Patient | Read/Search | All | Patient context from CDS Hook |
| Coverage | Read/Search | Clinical-Financial Bridge | Active insurance, plan, deductible status |
| ExplanationOfBenefit | Search | Bridge + Specialty PA | Claims history, denial patterns, step therapy |
| MedicationRequest | Read (prefetch) | PGx + Formulary | Drug being ordered |
| Observation (lab) | Search | Specialty PA | A1C, BMI, tumor markers |
| Observation (genomics) | Search | PGx CDS | Star alleles, diplotypes |
| Condition | Search | Specialty PA + Denial | Active diagnoses for medical necessity |
| AllergyIntolerance | Read | PGx CDS | Cross-reactivity |
| Claim | Submit ($submit) | PAS | Prior auth request bundle |
| ClaimResponse | Read | PAS | PA decision |
| Questionnaire | Read | DTR | Payer documentation requirements |
| QuestionnaireResponse | Create | DTR | Auto-populated PA documentation |
| ServiceRequest | Read (prefetch) | CRD | Procedure being ordered |

---

## 5. Demo Scenarios (Synthetic Data)

| # | Scenario | Patient Profile | What CFIP Shows |
|---|----------|----------------|-----------------|
| A | GLP-1 PA | T2 diabetic, BMI 33, A1C 7.5, tried metformin 6mo, UHC | Full PA: necessity check, step therapy, cost, one-click submit |
| B | PGx Safety | Prescribed clopidogrel, CYP2C19 poor metabolizer | Gene-drug alert, dose adjust, alternative (prasugrel) |
| C | Oncology PA | Lung cancer, PD-L1+, requesting Keytruda | Biomarker validation, NCCN pathway, complex PA bundle |
| D | Denial Prevention | Routine MRI, history of similar denials with Aetna | Pre-submission risk flag, doc gap, alt imaging suggestion |

---

## 6. Phase Roadmap (Lightweight — Detail Lives in Current Phase Only)

| Phase | Goal | "Done" Means |
|-------|------|-------------|
| 1 | Foundation | Authenticate with Epic sandbox, read Patient + Coverage, print to terminal |
| 2 | CDS Hooks Service | Discovery endpoint works, test harness fires order-select, card returned |
| 3 | Clinical-Financial Bridge | Denial prediction for synthetic order with 4+ evidence factors |
| 4 | PGx CDS + Specialty PA | PGx alert for clopidogrel scenario, specialty PA for Ozempic |
| 5 | Agentic Orchestrator | Full Ozempic chain: hook → agent → intelligent card → PA submit |
| 6 | Demo + Polish | All 3 UI surfaces working, 4 scenarios demo-ready |

---

 ## 7. Current Phase

  **Phase:** 2 — CDS Hooks Service
  **Status:** COMPLETE ✓

  **Verified working:**
  - GET /cds-services returns valid discovery JSON (order-select, prefetch templates) ✓
  - POST /cds-services/cfip-order-intelligence returns stub card ✓
  - Terminal harness: python tools/cds_hooks_harness/harness.py fires Scenario A ✓
  - Browser UI: http://localhost:8000/harness/ renders INFO card ✓
  - 18/18 tests passing ✓

  **All Phase 2 files:**
  - app/models/cds_hooks.py — CDS Hooks 2.0 Pydantic models
  - app/api/__init__.py, app/api/cds_hooks.py — discovery + hook handler
  - app/main.py — updated with router + static mount
  - tools/cds_hooks_harness/scenarios.py, harness.py — terminal test harness
  - tools/cds_hooks_harness/static/index.html — browser UI
  - tests/test_cds_hooks.py — 18 spec-compliance tests

  **Next phase:** Phase 3 — Clinical-Financial Bridge
  - Denial prediction for synthetic order with 4+ evidence factors
  - Real FHIR data: Coverage, ExplanationOfBenefit, Observation (A1C, BMI)
  - Denial scorer replacing stub card values
  - SQLite payer rules cache

---

## 8. Mutual Understanding Contract

### Builder's Role (You)
- Review each deliverable before I proceed to the next
- Test each piece on your Windows machine before we move forward
- Tell me when something doesn't feel right — your 17-year instinct is a valid signal
- Keep this requirement.md updated (I'll provide the updates, you paste them)
- **Paste both `requirement.md` and `architecture.md` at the start of every new session**

### AI's Role (Me)
- **Never write more than one file without your review.** I explain what I'm about to build and why before writing code.
- **Flag every significant design decision** as a question, not a default. Present options with tradeoffs.
- **Comment Python idioms** that wouldn't be obvious to a C# developer (e.g., decorators, comprehensions, context managers).
- **Never assume the next step.** Always ask or present options after completing a deliverable.
- **Provide "done" criteria** at the start of each phase so we both know what success looks like.
- **Session handoff:** At the end of each session, I provide updates to Section 7 (Current Phase) with: what we completed, what's working, what's next, any open questions.

### Review Protocol
1. I describe what I'm about to build (architecture/approach)
2. You approve, adjust, or redirect
3. I build it (one file at a time)
4. You review the code
5. You test on your machine
6. We update requirement.md and proceed

### Escalation
- If I'm unsure about a clinical domain detail → I tell you and suggest research
- If a technical approach has multiple valid paths → I present 2-3 options with tradeoffs
- If something will take more than one session → I break it into testable increments
- If I realize a past decision should change → I raise it explicitly with evidence, not silently change direction

---

## 9. Architectural Decision Log

| # | Date | Decision | Rationale |
|---|------|----------|-----------|
| ADR-001 | 2026-03-20 | Python over C# | Builder's AI expertise in Python; best LLM/agent SDK ecosystem; lighter RAM footprint |
| ADR-002 | 2026-03-20 | Hybrid AI (rules + LLM API) | Deterministic rules for clinical logic (auditable); LLM for NL generation and edge cases |
| ADR-003 | 2026-03-20 | Three UI surfaces | CDS Cards for clinicians, SMART app for detail, Dashboard for C-suite — one backend serves all |
| ADR-004 | 2026-03-20 | Clinical-Financial Bridge as core differentiator | No existing product bridges CDS + RCM + PGx at point of care — genuine white space |
| ADR-005 | 2026-03-20 | Plan one phase in detail, next in outline | Over-planning wastes effort; early phases change assumptions for later phases |
| ADR-006 | 2026-03-24 | OpenAI (gpt-4o-mini) over Claude API | Builder already has working OpenAI key; LLM is a composing tool not the brain — model choice doesn't affect core product value |
| ADR-007 | 2026-03-24 | Reuse existing Epic app registration | Same client_id + key pair works for multiple backend apps; all scopes already assigned; zrok tunnel already registered |

---

## 10. Epic Sandbox Configuration

> **SECURITY:** Actual keys and secrets live in `.env` file only (gitignored). This section documents variable names and non-sensitive metadata.

### .env File Template
```
# Epic SMART Backend Services
EPIC_CLIENT_ID=dfc59c89-fcc7-47ff-a453-7af76f63ee77
EPIC_TOKEN_ENDPOINT=https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token
EPIC_FHIR_BASE_URL=https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
EPIC_PRIVATE_KEY_PATH=./keys/privatekey.pem
EPIC_KEY_ID=my-epic-key-v2
EPIC_GROUP_ID=e3iabhmS8rsueyz7vaimuiaSmfGvi.QwjVXJANlPOgR83

# Tunneling (for CDS Hooks — Epic needs to POST to our service)
ZROK_PUBLIC_URL=https://pcmsmartbackendapp1.share.zrok.io

# OpenAI (for NL generation, appeal letters, card narratives)
OPENAI_API_KEY=sk-proj-...your-key...
OPENAI_MODEL=gpt-4o-mini
```

### Configuration Notes
- **Algorithm:** RS384 (Epic standard for Backend Services)
- **App Type:** Backend Systems (client_credentials flow)
- **Key ID:** `my-epic-key-v2` — matches what's registered in Epic app config
- **Private Key:** Existing RSA key pair in workspace, shared with other backend app (no conflict)
- **Tunneling:** zrok URL registered with Epic — required for CDS Hooks (Epic POSTs to us)
- **Group ID:** For Bulk FHIR export — needed in later phases for population health data
- **Scopes:** All scopes assigned in Epic app registration

### Verified Working
- Epic Backend OAuth flow: ✅ (working in existing app)
- zrok tunneling: ✅ (registered and tested)
- Key pair: ✅ (same keys used successfully for another backend app)

---

## 11. Session History

| Session | Date | What Happened |
|---------|------|---------------|
| 1 | 2026-03-24 | Market research, use case ranking, gap analysis, architecture decisions, requirement.md + architecture.md created |
| 2 | 2026-03-24 | Finalized tech stack (Python/FastAPI), settled C# vs Python debate, designed three UI surfaces, discussed working style, created mutual understanding contract, verified Epic sandbox config, decided OpenAI over Claude API |
| 3 | 2026-03-24 | Phase 1 complete: Project scaffold, Epic OAuth (RS384 JWT), FHIR Patient + Coverage read, verify_epic.py working. No Coverage data in sandbox — handled gracefully. |
| 4 | 2026-03-24 | Phase 1 + Phase 2 complete. Scaffold, Epic auth, FHIR reads, CDS Hooks service, 18 tests passing. |  
