# Healthcare Integration & CDS Hooks — A Developer's Guide

> Written for a developer coming from C#/.NET who is building their first SMART on FHIR + CDS Hooks application.
> This document captures the "why" behind CFIP's architecture decisions.

---

## 1. The Real-World Problem

Dr. Smith is in Epic, looking at patient Derrick Lin's chart. She clicks "Order Medication" and selects Ozempic (a GLP-1 drug for diabetes).

**What happens today (without CFIP):**

Nothing intelligent happens at the point of ordering. She submits the order. Pharmacy processes it. The insurance company (say, UnitedHealthcare) denies it three days later because the patient didn't try metformin first — a "step therapy" requirement. Now everyone wastes time: the doctor writes an appeal, the staff resubmits, the patient waits without medication. This cycle costs US healthcare billions annually.

**What should happen (with CFIP):**

The instant she selects Ozempic, a card appears right inside her Epic screen:

```
✅ Ozempic — 87% Approval Probability
   Step therapy met (metformin 6 months). UHC prior auth required.
   Estimated copay: $150/mo
   No pharmacogenomic safety concerns.
   [Submit PA Now]  [View Full Analysis]
```

She clicks "Submit PA Now," the prior authorization is submitted electronically, and she moves on to her next patient. Total added time: 5 seconds.

This is what CFIP builds. The rest of this document explains the technology that makes it possible.

---

## 2. The Three Standards That Power This

Three separate but related standards work together. Think of them as layers:

```
┌─────────────────────────────────────────────────┐
│  CDS Hooks                                      │
│  "How does Epic notify us when something         │
│   happens, and how do we send guidance back?"    │
├─────────────────────────────────────────────────┤
│  SMART on FHIR                                  │
│  "How does our app authenticate with Epic        │
│   and get permission to access data?"            │
├─────────────────────────────────────────────────┤
│  FHIR R4                                        │
│  "What does the patient data look like?          │
│   What format? What fields?"                     │
└─────────────────────────────────────────────────┘
```

### 2.1 FHIR R4 — The Data Language

FHIR (Fast Healthcare Interoperability Resources) is a standard for representing healthcare data as JSON resources. Instead of every hospital having its own proprietary format, FHIR says: "A Patient looks like this. A Medication looks like this. An Observation (lab result) looks like this."

It's like how JSON itself is a standard — everyone agrees on the format, so systems can talk to each other.

**Example — a FHIR Patient resource:**
```json
{
  "resourceType": "Patient",
  "id": "erXuFYUfucBZaryVksYEcMg3",
  "name": [{ "given": ["Derrick"], "family": "Lin" }],
  "birthDate": "1973-05-12",
  "gender": "male"
}
```

**Who owns it:** HL7 International (Health Level Seven), the global authority for healthcare data standards.

### 2.2 SMART on FHIR — The Authentication Layer

SMART (Substitutable Medical Applications, Reusable Technologies) on FHIR is a standard for how apps authenticate with EHRs and get access to FHIR data. It's built on top of OAuth 2.0 — the same auth framework used by Google, GitHub, etc.

It comes in two flavors:

| Flavor | Who uses it | How it works | CFIP usage |
|--------|------------|--------------|------------|
| **EHR Launch** | User-facing apps | Doctor clicks a link in Epic → app opens in browser → user authorizes access | SMART Companion App (Surface 2) |
| **Backend Services** | Server-to-server apps | No user involved. Server proves identity with a signed JWT → gets access token | CFIP's backend (what we built in Phase 1) |

**What we built in Phase 1** was Backend Services auth: CFIP creates a JWT signed with our private RSA key, sends it to Epic's OAuth endpoint, gets back an access token, and uses that token to read FHIR data. No human in the loop.

**Who owns it:** SMART Health IT project (originally Boston Children's Hospital / Harvard), now part of HL7.

### 2.3 CDS Hooks — The Event System

CDS (Clinical Decision Support) Hooks is a standard for real-time, event-driven integration with EHRs. It answers: "How does Epic tell external services that something clinically interesting just happened?"

**Who owns it:** HL7 International — same organization that owns FHIR and SMART.

**Full spec:** https://cds-hooks.hl7.org/2.0/

This is important enough to get its own section below.

---

## 3. CDS Hooks — Deep Dive

### 3.1 The Core Concept

CDS Hooks works like .NET events/delegates. The analogy:

| Concept | C# / .NET | CDS Hooks |
|---------|-----------|-----------|
| Your business logic | A class library | CFIP backend |
| How it gets triggered | An event handler wired to a UI event | A hook endpoint wired to an EHR event |
| The event system | .NET events / delegates | CDS Hooks spec |
| The UI that triggers it | A button click in WPF/WinForms | Clinician action in Epic |
| What you return | Data that updates the UI | Cards that Epic renders in the clinician's screen |

### 3.2 How It Works — Step by Step

**Step 1 — You publish a discovery endpoint.**

Your service exposes `GET /cds-services` that returns a JSON list describing what hooks you handle:

```json
{
  "services": [{
    "hook": "order-select",
    "id": "cfip-order-intelligence",
    "title": "CFIP Order Intelligence",
    "description": "Clinical-financial intelligence for medication orders",
    "prefetch": {
      "patient": "Patient/{{context.patientId}}",
      "medications": "MedicationRequest?patient={{context.patientId}}"
    }
  }]
}
```

This tells the EHR: "I handle `order-select` events. When you call me, please include the Patient and MedicationRequest data."

**Step 2 — Epic fires the hook.**

When a clinician selects a medication, Epic constructs a POST request and sends it to your endpoint:

```json
{
  "hook": "order-select",
  "hookInstance": "unique-id-for-this-event",
  "context": {
    "patientId": "erXuFYUfucBZaryVksYEcMg3",
    "selections": ["MedicationRequest/ozempic-order-123"]
  },
  "prefetch": {
    "patient": { "resourceType": "Patient", "name": [{"given": ["Derrick"], "family": "Lin"}] },
    "medications": { "resourceType": "Bundle", "entry": [...] }
  },
  "fhirServer": "https://fhir.epic.com/...",
  "fhirAuthorization": { "access_token": "...", "token_type": "Bearer" }
}
```

Notice `prefetch` — Epic ran those FHIR queries you declared in Step 1 and included the results. This saves you from making separate API calls.

**Step 3 — You respond with cards.**

Your service processes the request and returns cards:

```json
{
  "cards": [{
    "summary": "Ozempic: 87% approval | $150/mo | No PGx issues",
    "detail": "Step therapy met (metformin 6mo). UHC prior auth required.",
    "indicator": "info",
    "source": { "label": "CFIP", "url": "https://cfip.example.com" },
    "suggestions": [{
      "label": "Submit PA Now",
      "uuid": "pa-submit-123"
    }],
    "links": [{
      "label": "View Full Analysis",
      "url": "https://cfip.example.com/smart-launch?patient=abc123",
      "type": "smart"
    }]
  }]
}
```

**Step 4 — Epic renders the cards.**

The clinician sees the card natively inside Epic's interface. No separate app, no extra clicks, no context switching.

### 3.3 The Three Hooks CFIP Supports

| Hook | When It Fires | What CFIP Does |
|------|--------------|----------------|
| `patient-view` | Clinician opens a patient's chart | Pre-emptive alerts: pending PAs, PGx flags, coverage gaps |
| `order-select` | Clinician picks a drug or procedure in order entry | Real-time intelligence: denial risk, cost, PGx safety, PA requirements |
| `order-sign` | Clinician is about to sign/submit the order | Last gate: submit PA, block unsafe orders, final confirmation |

### 3.4 Prefetch — Why It Matters

Without prefetch, the flow would be:

```
Epic fires hook → CFIP receives it → CFIP calls Epic FHIR API for Patient →
waits → CFIP calls for Coverage → waits → CFIP calls for Observations → waits →
CFIP responds with card
```

That's multiple round-trips. Slow. With prefetch:

```
Epic fires hook (includes Patient, Coverage, Observations in the request) →
CFIP already has the data → CFIP responds with card
```

One round-trip. Fast. The clinician doesn't wait.

CFIP's approach: declare prefetch for common data, use it if Epic sends it, fall back to FHIR API calls if it's missing.

---

## 4. Pharmacogenomics (PGx) — Why Genes Matter in Prescribing

### 4.1 The Problem

Your genes affect how your body processes drugs. Specifically, enzymes in your liver (encoded by genes) metabolize most medications. If you have a variant of one of these genes, a drug might:

- **Not work at all** (your body can't activate it)
- **Work too well** (dangerous overdose effect at normal doses)
- **Cause severe side effects** (your body can't clear it fast enough)

This isn't rare — roughly 90% of people carry at least one actionable PGx variant.

### 4.2 A Real Example — Clopidogrel

Clopidogrel (brand name: Plavix) is a blood thinner prescribed to prevent heart attacks and strokes. It's a "prodrug" — meaning it's inactive when you swallow it. Your liver enzyme CYP2C19 converts it into the active form.

**The gene:** CYP2C19

**The variants:**
| Metabolizer Status | What Happens | Prevalence |
|-------------------|--------------|------------|
| Normal metabolizer | Drug works as expected | ~35% of people |
| Rapid metabolizer | Drug is extra potent (usually fine) | ~30% |
| Intermediate metabolizer | Reduced effectiveness | ~25% |
| Poor metabolizer | Drug barely activates — **essentially useless** | ~2-10% (varies by ethnicity) |

**The disaster scenario:** A patient who is a CYP2C19 poor metabolizer gets prescribed clopidogrel after a cardiac stent. They take it faithfully for months. It never works. They have another heart attack. A simple genetic test before prescribing would have caught this — the doctor would have prescribed prasugrel (a different blood thinner that doesn't depend on CYP2C19) instead.

### 4.3 What CFIP Does With PGx

When a doctor orders a PGx-sensitive drug, CFIP's orchestrator:

1. **Checks if PGx data exists** — looks for genomic Observation resources in the patient's FHIR record
2. **If data exists** — runs it through CPIC guidelines (deterministic rules, not AI) to check for interactions
3. **If interaction found** — shows a safety alert: "⚠️ CYP2C19 poor metabolizer — clopidogrel ineffective. Consider prasugrel."
4. **If no PGx data exists** — suggests testing: "No PGx data on file. Recommend pre-emptive PGx panel before starting clopidogrel."

### 4.4 CPIC Guidelines

CPIC (Clinical Pharmacogenomics Implementation Consortium) publishes peer-reviewed, evidence-based guidelines for drug-gene pairs. These are real clinical guidelines used by hospitals.

Example CPIC guideline for clopidogrel:
- Gene: CYP2C19
- Poor metabolizer → use alternative antiplatelet (prasugrel, ticagrelor)
- Intermediate metabolizer → use alternative OR use clopidogrel with increased monitoring
- Normal/rapid metabolizer → use clopidogrel as prescribed

CFIP encodes these as deterministic Python rules — not AI/LLM decisions. This is a healthcare requirement: clinical safety rules must be auditable, traceable, and reproducible. You can't tell a regulator "the AI thought it was fine."

### 4.5 Why PGx Is Part of CFIP's Moat

Most PGx tools exist as standalone products. Most PA tools exist as standalone products. Nobody combines them. But they're deeply connected:

- A PGx-guided drug switch might change the PA requirements (different drug = different payer rules)
- A payer might deny a drug alternative unless the PGx test is documented
- Cost changes when you switch drugs based on genetics

CFIP's orchestrator handles all of this in one pass. That's the value of integration.

---

## 5. The Competitive Landscape — "Can't Anyone Build This?"

### 5.1 Yes, Anyone Can Build CDS Hooks

CDS Hooks is an open spec. Any developer can:
- Build a service that responds to hooks
- Publish it on Epic's App Orchard
- Sell it to hospitals

This is by design — open standards encourage innovation.

### 5.2 What Exists Today (Separate Products)

| Category | Examples | What They Do |
|----------|----------|-------------|
| Drug interaction checkers | First Databank, Medi-Span | "Drug A interacts with Drug B" |
| Clinical guidelines | Nuance/DAX, Wolters Kluwer | "Patient is overdue for screening" |
| Prior auth tools | CoverMyMeds, Surescripts | "This drug needs PA — here's the form" |
| Cost transparency | RxRevu (now Veradigm) | "This drug costs $X on this plan" |
| PGx platforms | OneOme, Genomind | "This patient has a gene-drug interaction" |

Each shows a separate card. The clinician sees five cards from five vendors and mentally pieces together the picture. This is the status quo.

### 5.3 CFIP's Differentiation — Integration Is the Moat

The CDS Hooks plumbing is just the delivery mechanism — like how HTTP is just a transport protocol. The value is what travels on it.

**CFIP's three moats:**

1. **Clinical-Financial Bridge** — Connecting claims data (denials, costs, coverage) with clinical evidence (labs, diagnoses, medications). No one does this well because it requires understanding both clinical medicine and revenue cycle management.

2. **Agentic orchestration** — Dynamically choosing what evidence to gather based on drug class + payer + patient context. Ozempic triggers a different chain than Keytruda triggers a different chain than clopidogrel. A static rule engine can't handle this combinatorial explosion.

3. **Denial prediction from patterns** — Learning from past denials to prevent future ones. This requires the bridge between clinical and financial data that doesn't exist in current products.

**An analogy:** Thousands of developers can build a REST API. That doesn't make Stripe less valuable. Stripe's moat isn't the API layer — it's the payment intelligence, fraud detection, and integrations behind it. Same principle: anyone can build CDS Hooks plumbing, but the intelligence behind CFIP's cards is the product.

### 5.4 How Hospitals Choose — It's Not Epic's Decision

This is a crucial business point. **Epic does not choose which CDS Hooks services to use.** The flow works like an app store:

```
Developer builds CDS Hooks service
        ↓
Publishes on Epic App Orchard (like App Store)
        ↓
Hospital evaluates available options
        ↓
Hospital IT team activates their chosen service(s)
        ↓
Clinicians at that hospital see cards from activated services
```

- **Epic's role:** Platform provider. They maintain the App Orchard marketplace and the CDS Hooks infrastructure. They don't pick winners.
- **Hospital's role:** Customer. They evaluate, purchase, and activate. Each hospital decides independently.
- **Your role (CFIP):** Vendor. You sell to hospitals by demonstrating ROI.

**Can a hospital run multiple CDS Hooks services?** Yes. Epic fires the hook to all registered services and displays all cards. But hospitals avoid overloading clinicians — "alert fatigue" is a real problem (too many alerts → doctors ignore all of them). So hospitals are selective.

**What this means for CFIP's go-to-market:** The sales conversation is with hospital leadership:
- **CIO** (Chief Information Officer) — cares about integration, security, standards compliance
- **CMIO** (Chief Medical Information Officer) — cares about clinical value, alert fatigue reduction, patient safety
- **CFO / Revenue Cycle VP** — cares about denial reduction, PA automation, cost savings

That's why CFIP has three UI surfaces: CDS Cards for clinicians, SMART App for clinical detail, and the Ops Dashboard for the people who write the check.

---

## 6. How It All Connects in CFIP

Here's the full picture of how these standards work together in a single CFIP interaction:

```
1. SETUP (one-time):
   CFIP registers with Epic via App Orchard
   Hospital activates CFIP in their Epic instance
   CFIP's discovery endpoint tells Epic what hooks we handle

2. RUNTIME (every order):
   Clinician selects Ozempic in Epic
        ↓
   Epic sees: "order-select" event triggered           [CDS Hooks]
        ↓
   Epic calls CFIP: POST /cds-services/cfip-order-intelligence
   (includes patient data in prefetch)                  [CDS Hooks]
        ↓
   CFIP authenticates with Epic for additional data     [SMART on FHIR]
        ↓
   CFIP reads A1C, BMI, coverage, Rx history            [FHIR R4]
        ↓
   CFIP's orchestrator runs the GLP-1 evidence chain:
   - Check step therapy (metformin trial)               [Payer rules]
   - Check PGx safety                                   [CPIC rules]
   - Score denial risk                                  [Clinical-Financial Bridge]
   - Estimate cost                                      [Coverage + formulary]
   - Generate narrative                                 [OpenAI API]
        ↓
   CFIP returns CDS card with everything synthesized    [CDS Hooks]
        ↓
   Epic displays card in clinician's workflow
        ↓
   Clinician clicks "Submit PA Now"
        ↓
   CFIP submits electronic PA via FHIR                  [FHIR R4]
        ↓
   Done. 5 seconds. No phone calls. No faxes.
```

---

## 7. Glossary

| Term | What It Means |
|------|--------------|
| **FHIR** | Fast Healthcare Interoperability Resources — the data format standard |
| **SMART on FHIR** | Authentication/authorization standard for healthcare apps |
| **CDS Hooks** | Event-driven standard for real-time clinical decision support |
| **HL7** | Health Level Seven International — the standards organization that owns FHIR, SMART, and CDS Hooks |
| **Epic Hyperspace** | Epic's main EHR application that clinicians use |
| **App Orchard** | Epic's marketplace for third-party apps (like an App Store) |
| **PGx** | Pharmacogenomics — matching drugs to a patient's genetic profile |
| **CPIC** | Clinical Pharmacogenomics Implementation Consortium — publishes gene-drug guidelines |
| **CYP2C19** | A liver enzyme gene commonly tested in PGx; affects clopidogrel, some antidepressants, PPIs |
| **Prior Authorization (PA)** | Insurance requirement to approve certain drugs/procedures before they're covered |
| **Step Therapy** | Insurance rule requiring cheaper drugs to be tried first (e.g., metformin before Ozempic) |
| **Denial** | Insurance company rejecting a claim or PA request |
| **Appeal** | Formal challenge to a denial, supported by clinical evidence |
| **Prefetch** | CDS Hooks feature where the EHR sends FHIR data along with the hook request |
| **CDS Card** | The visual response a CDS Hooks service sends back — rendered in the EHR UI |
| **Alert Fatigue** | When clinicians get too many alerts and start ignoring all of them |
| **CMIO** | Chief Medical Information Officer — hospital leader bridging clinical and IT |
| **EHR** | Electronic Health Record — the software system (Epic, Cerner, etc.) |
| **GLP-1** | Glucagon-Like Peptide-1 — a drug class for diabetes/obesity (Ozempic, Wegovy) |
| **Prodrug** | A drug that's inactive until your body metabolizes it into the active form |
| **Backend Services** | SMART on FHIR auth pattern for server-to-server (no user) communication |
| **EHR Launch** | SMART on FHIR auth pattern where an app launches from within the EHR |

---

*This document was created during CFIP development as a reference for understanding the healthcare integration ecosystem. It reflects the architecture decisions documented in requirement.md and architecture.md.*
