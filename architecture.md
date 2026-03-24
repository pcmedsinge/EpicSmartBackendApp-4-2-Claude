## ctrl+shift+v is the shortcut
# CFIP — Architecture Diagrams

> This document is the visual companion to `requirement.md`.
> Both documents must be provided at the start of every session.

---

## 1. System Context — Three UI Surfaces, One Backend

```mermaid
graph TB
    subgraph EPIC["Epic Hyperspace (EHR)"]
        CV[patient-view hook]
        OS[order-select hook]
        OSN[order-sign hook]
        CARD[CDS Cards rendered in EHR]
    end

    subgraph CFIP_BACKEND["CFIP Backend Service (Python/FastAPI)"]
        CDS[CDS Hooks Engine]
        BRIDGE[Clinical-Financial Bridge]
        PGX[Pharmacogenomics CDS]
        SPA[Specialty PA Optimizer]
        ORCH[Agentic Orchestrator]
        FHIR_GW[FHIR Gateway]
    end

    subgraph UI_SURFACES["UI Surfaces"]
        SMART_APP[SMART Companion App<br/>Launched from CDS card links]
        DASHBOARD[Ops Dashboard<br/>Standalone analytics]
    end

    subgraph EXTERNAL["External Services"]
        EPIC_FHIR[Epic FHIR R4 Server]
        CLAUDE[OpenAI API<br/>Appeal letters, NL generation]
        RULES_DB[(SQLite<br/>Payer rules, PGx KB,<br/>denial patterns)]
    end

    CV -->|hook request| CDS
    OS -->|hook request| CDS
    OSN -->|hook request| CDS
    CDS --> ORCH
    ORCH --> BRIDGE
    ORCH --> PGX
    ORCH --> SPA
    ORCH --> FHIR_GW
    FHIR_GW -->|SMART Backend OAuth| EPIC_FHIR
    ORCH --> CLAUDE
    ORCH --> RULES_DB
    CDS -->|cards JSON| CARD
    CDS -->|smart link| SMART_APP
    CFIP_BACKEND --> DASHBOARD
    SMART_APP -->|EHR launch OAuth| EPIC_FHIR
```

---

## 2. CDS Hooks Request/Response Flow

```mermaid
sequenceDiagram
    participant EHR as Epic Hyperspace
    participant CDS as CFIP CDS Engine
    participant ORCH as Agentic Orchestrator
    participant FHIR as Epic FHIR R4
    participant RULES as Rules Engine
    participant LLM as OpenAI API

    Note over EHR: Clinician orders Ozempic

    EHR->>CDS: POST /cds-services/cfip-order-intelligence<br/>{hook: "order-select", context: {patientId, selections}, prefetch: {patient, medications}}

    CDS->>ORCH: process(hookContext, prefetch)

    Note over ORCH: Agent identifies drug class: GLP-1

    ORCH->>FHIR: GET Observation?patient=X&code=A1C
    FHIR-->>ORCH: A1C = 7.5%

    ORCH->>FHIR: GET Observation?patient=X&code=BMI
    FHIR-->>ORCH: BMI = 33

    ORCH->>FHIR: GET ExplanationOfBenefit?patient=X
    FHIR-->>ORCH: Rx history (metformin 6 months)

    ORCH->>FHIR: GET Coverage?patient=X&status=active
    FHIR-->>ORCH: UHC Plan, deductible status

    ORCH->>RULES: checkDenialRisk(drug, payer, evidence)
    RULES-->>ORCH: 87% approval probability

    ORCH->>RULES: checkPgx(patient, drug)
    RULES-->>ORCH: No PGx contraindication

    ORCH->>RULES: getCostEstimate(drug, coverage)
    RULES-->>ORCH: $150/mo copay

    ORCH->>LLM: composeCardNarrative(evidence)
    LLM-->>ORCH: Human-readable summary

    ORCH-->>CDS: AgentResult(denialRisk, cost, pgx, narrative)

    CDS-->>EHR: {cards: [{summary, detail, indicator, suggestions, links}]}

    Note over EHR: Clinician sees card:<br/>87% approval | $150/mo | No PGx issue<br/>[Submit PA Now] [View Full Analysis]
```

---

## 3. Agentic Orchestration — Decision Tree

This shows how the orchestrator dynamically selects different evidence chains based on drug class.

```mermaid
flowchart TD
    START([order-select / order-sign hook received]) --> CLASSIFY{Classify drug/procedure}

    CLASSIFY -->|GLP-1<br/>Ozempic, Wegovy| GLP1_CHAIN
    CLASSIFY -->|Oncology<br/>Keytruda, Opdivo| ONCO_CHAIN
    CLASSIFY -->|PGx-sensitive<br/>Warfarin, Clopidogrel| PGX_CHAIN
    CLASSIFY -->|Standard<br/>MRI, CT, etc.| STD_CHAIN

    subgraph GLP1_CHAIN["GLP-1 evidence chain"]
        G1[Fetch BMI + A1C<br/>Observation] --> G2[Fetch Rx history<br/>ExplanationOfBenefit]
        G2 --> G3{Metformin trial<br/>>= 90 days?}
        G3 -->|Yes| G4[Check PGx data]
        G3 -->|No| G3F[Flag: step therapy<br/>not met]
        G4 --> G5[Check coverage tier]
        G5 --> G6[Score denial risk]
    end

    subgraph ONCO_CHAIN["Oncology evidence chain"]
        O1[Fetch tumor type<br/>Condition] --> O2[Fetch biomarker<br/>Observation PD-L1]
        O2 --> O3[Fetch prior regimens<br/>MedicationRequest history]
        O3 --> O4[Validate NCCN pathway]
        O4 --> O5[Build PA bundle with<br/>pathology evidence]
    end

    subgraph PGX_CHAIN["PGx safety chain"]
        P1[Fetch genomic data<br/>Observation PGx] --> P2{Gene-drug<br/>interaction?}
        P2 -->|Poor metabolizer| P3[Calculate adjusted dose]
        P2 -->|Normal| P4[No alert needed]
        P3 --> P5[Suggest alternative drug]
        P1 --> P1F{PGx data exists?}
        P1F -->|No| P1G[Recommend pre-emptive<br/>PGx testing]
        P1F -->|Yes| P2
    end

    subgraph STD_CHAIN["Standard denial prevention"]
        S1[Fetch claims history<br/>ExplanationOfBenefit] --> S2[Pattern match:<br/>similar past denials?]
        S2 --> S3[Check documentation<br/>completeness]
        S3 --> S4[Score denial risk]
    end

    G6 --> COMPOSE
    G3F --> COMPOSE
    O5 --> COMPOSE
    P5 --> COMPOSE
    P4 --> COMPOSE
    P1G --> COMPOSE
    S4 --> COMPOSE

    COMPOSE([Compose CDS card<br/>with all evidence]) --> RETURN([Return to CDS Engine])
```

---

## 4. SMART Backend Services OAuth Flow

```mermaid
sequenceDiagram
    participant CFIP as CFIP Backend
    participant EPIC_AUTH as Epic OAuth Server
    participant EPIC_FHIR as Epic FHIR R4

    Note over CFIP: On startup / token expiry

    CFIP->>CFIP: Create JWT<br/>iss: client_id<br/>sub: client_id<br/>aud: token_endpoint<br/>exp: now + 5min<br/>Sign with RS384 private key

    CFIP->>EPIC_AUTH: POST /oauth2/token<br/>grant_type=client_credentials<br/>client_assertion_type=jwt-bearer<br/>client_assertion={signed_jwt}

    EPIC_AUTH-->>CFIP: {access_token, expires_in, token_type, scope}

    Note over CFIP: Token cached until expiry

    CFIP->>EPIC_FHIR: GET /Patient/abc123<br/>Authorization: Bearer {access_token}

    EPIC_FHIR-->>CFIP: Patient resource (JSON)
```

---

## 5. Solution Structure

```
CFIP/
├── app/
│   ├── main.py                     # FastAPI entry point
│   ├── config.py                   # Settings, Epic sandbox config
│   │
│   ├── api/
│   │   ├── cds_hooks.py            # GET /cds-services, POST /cds-services/{id}
│   │   ├── smart_launch.py         # SMART companion app launch handler
│   │   └── dashboard.py            # Ops dashboard API endpoints
│   │
│   ├── fhir/
│   │   ├── client.py               # Epic FHIR R4 client (httpx + fhirclient)
│   │   ├── auth.py                 # SMART Backend Services OAuth (JWT + token mgmt)
│   │   └── resource_mappers.py     # FHIR resource → domain model converters
│   │
│   ├── agents/
│   │   ├── orchestrator.py         # Main agentic orchestrator (plan-execute-verify)
│   │   ├── denial_prediction.py    # Clinical-financial bridge agent
│   │   ├── pgx_safety.py           # Pharmacogenomics CDS agent
│   │   └── specialty_pa.py         # Specialty drug PA agent
│   │
│   ├── rules/
│   │   ├── cpic_engine.py          # Deterministic PGx rules (CPIC guidelines)
│   │   ├── payer_rules.py          # Payer-specific PA requirements
│   │   ├── denial_scorer.py        # Denial risk scoring (weighted criteria)
│   │   └── drug_classifier.py      # Drug class identification (GLP-1, onco, PGx, standard)
│   │
│   ├── intelligence/
│   │   ├── openai_client.py        # OpenAI API wrapper
│   │   ├── appeal_generator.py     # Auto-appeal letter generation
│   │   └── card_composer.py        # CDS card narrative composition
│   │
│   ├── models/
│   │   ├── cds_hooks.py            # CDS Hooks request/response models (Pydantic)
│   │   ├── domain.py               # Domain models (DenialRisk, PgxResult, etc.)
│   │   └── fhir_types.py           # Thin wrappers over FHIR resource types
│   │
│   └── data/
│       ├── db.py                   # SQLite connection + queries
│       ├── seed_payer_rules.py     # Seed payer-specific rules
│       ├── seed_cpic.py            # Seed CPIC drug-gene pairs
│       └── seed_synthetic.py       # Seed demo scenario data
│
├── tests/
│   ├── test_auth.py
│   ├── test_cds_hooks.py
│   ├── test_orchestrator.py
│   ├── test_denial_scorer.py
│   └── test_pgx_engine.py
│
├── tools/
│   └── cds_hooks_harness/          # Test harness simulating Epic hook calls
│       ├── harness.py              # Fires synthetic hook events at CFIP
│       └── scenarios.py            # Demo scenario definitions (A, B, C, D)
│
├── keys/
│   ├── private_key.pem             # RS384 private key (gitignored)
│   └── public_key.pem              # RS384 public key (uploaded to Epic)
│
├── requirements.txt
├── pyproject.toml
├── .env                            # Epic client_id, OpenAI API key (gitignored)
└── README.md
```

---

## 6. FHIR Resource Relationships

Shows how FHIR resources connect to each other in CFIP's data model.

```mermaid
erDiagram
    Patient ||--o{ Coverage : "has insurance"
    Patient ||--o{ MedicationRequest : "prescribed"
    Patient ||--o{ Condition : "diagnosed with"
    Patient ||--o{ Observation : "has results"
    Patient ||--o{ AllergyIntolerance : "allergic to"
    Patient ||--o{ ExplanationOfBenefit : "claims history"

    Coverage ||--o{ ExplanationOfBenefit : "paid by"

    MedicationRequest ||--o{ Claim : "requires PA"
    Claim ||--|| ClaimResponse : "gets decision"

    Observation }|--|| Observation : "PGx panel contains"

    Claim ||--o{ QuestionnaireResponse : "supported by"
    Questionnaire ||--|| QuestionnaireResponse : "filled from"

    MedicationRequest {
        string medication "Drug being ordered"
        string status "active/draft"
        reference patient "Who it's for"
    }

    Coverage {
        string payor "Insurance company"
        string plan "Plan name"
        string status "active"
    }

    ExplanationOfBenefit {
        string type "claim type"
        string outcome "approved/denied"
        money totalCost "billed amount"
    }

    Observation {
        string code "LOINC code (A1C, BMI, PGx)"
        string value "result value"
        string category "laboratory/genomics"
    }
```

---

## 7. CDS Hooks Test Harness Architecture

Since Epic sandbox doesn't support CDS Hooks directly, we build our own test harness.

```mermaid
sequenceDiagram
    participant USER as You (browser)
    participant HARNESS as CDS Hooks Test Harness<br/>(simulates Epic)
    participant CFIP as CFIP CDS Service
    participant EPIC as Epic FHIR Sandbox

    USER->>HARNESS: Select scenario (e.g., "Ozempic for patient X")
    HARNESS->>EPIC: GET Patient/X, Coverage, MedicationRequest (build prefetch)
    EPIC-->>HARNESS: Prefetch data

    HARNESS->>CFIP: POST /cds-services/cfip-order-intelligence<br/>{hook, context, prefetch}

    Note over CFIP: Orchestrator runs full agent chain

    CFIP-->>HARNESS: {cards: [...]}

    HARNESS->>USER: Render cards visually<br/>(mimics Epic Hyperspace card display)

    USER->>HARNESS: Click "View Full Analysis" link
    HARNESS->>USER: Open SMART Companion App
```

---

## 8. Deployment View (Dev Environment)

```
Your Windows Laptop (8GB RAM)
├── Python 3.12 runtime (~50MB)
├── CFIP FastAPI server (localhost:8000) (~100MB)
│   ├── CDS Hooks endpoints
│   ├── SMART companion app (served static)
│   └── Dashboard API
├── SQLite database (~5MB)
├── VS Code (~500MB)
└── Browser
    ├── CDS Hooks test harness UI
    ├── SMART companion app
    └── Ops dashboard

External (no local resources):
├── Epic FHIR Sandbox (fhir.epic.com)
└── OpenAI API (api.openai.com)

Estimated total RAM: ~800MB-1.2GB
Remaining for OS: ~6.5GB+
```

---

## Diagram Index

For quick reference in sessions:

| # | Diagram | Section | Shows |
|---|---------|---------|-------|
| 1 | System context | §1 | All components + three UI surfaces + external services |
| 2 | CDS Hooks flow | §2 | Full order-select sequence with Epic + agent + FHIR calls |
| 3 | Agentic decision tree | §3 | How orchestrator picks different chains per drug class |
| 4 | OAuth flow | §4 | SMART Backend Services JWT auth with Epic |
| 5 | Solution structure | §5 | Python project file/folder layout |
| 6 | FHIR relationships | §6 | How FHIR resources connect in CFIP's data model |
| 7 | Test harness | §7 | How we simulate CDS Hooks without Epic Hyperspace |
| 8 | Deployment view | §8 | What runs where on your 8GB Windows machine |
