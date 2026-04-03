# Prescription to Payment — End-to-End Clinical-Financial Workflow

This document describes the full journey of a medication order from the point of prescribing
through to provider payment and patient billing, and shows where CFIP intervenes.

---

## Workflow Overview

```
[1] Order Entry
      ↓
[2] CFIP Check  ◄─── CFIP intervenes here (real-time, before signing)
      ↓
[3] Prior Authorization
      ↓
[4] Fulfillment
      ↓
[5] Claim Submission
      ↓
[6] Adjudication
      ↓
[7] Remittance & Payment
      ↓
[8] Patient Billing
```

---

## Stage-by-Stage Detail

### 1. Order Entry
- Clinician enters a medication or procedure order in Epic (EHR)
- Order status is **`draft`** — not yet signed by the clinician
- Epic automatically fires the **`order-select`** CDS Hook to CFIP at this exact moment
- If the clinician proceeds to sign, the **`order-sign`** hook can also fire (also supported by the CDS Hooks spec)

**Who:** Clinician  
**System:** Epic EHR

---

### 2. CFIP Check ⭐ *(CFIP intervention point)*
CFIP runs a real-time pipeline before the order is signed:

| Check | What it does |
|-------|-------------|
| **PGx Safety** | Checks patient's genomic data (CYP2C19, DPYD, etc.) — flags if drug is unsafe for this genotype |
| **PA Pre-check** | Evaluates whether the order will need prior authorization and if criteria are met |
| **Denial Risk** | Scores historical denial patterns for this drug + payer combination |
| **Cost Transparency** | Surfaces indicative monthly cost to patient |
| **Clinical Narrative** | AI-assisted summary (GPT-4o-mini) for GLP-1, oncology, and standard orders |

CFIP returns **CDS Hooks cards** rendered inside Epic — clinician can act on them before signing.

**If high denial risk:** CFIP pre-generates an appeal letter draft so it is ready if needed at stage 6.

**Who:** Automated (CFIP pipeline)  
**System:** CFIP Backend (SMART on FHIR) + Epic CDS Hooks

---

### 3. Prior Authorization
- If PA is required, the practice submits it to the payer (UHC, Aetna, Cigna, etc.)
- Payer reviews clinical criteria
- Outcomes: **Approved**, **Denied**, or **Additional Info Requested**
- Typical turnaround: hours to weeks depending on drug/payer

**Who:** Practice admin / clinician  
**System:** Payer portal or FHIR-based PA submission

---

### 4. Fulfillment
- PA approved → pharmacy dispenses or hospital administers the drug/procedure
- Prescription is now **active**

**Who:** Pharmacy / nursing  
**System:** Pharmacy system / ADT

---

### 5. Claim Submission
- Provider submits a **claim** to the payer via a clearinghouse (e.g. Change Healthcare)
- Claim contains: diagnosis codes (ICD-10), procedure/drug codes (NDC/CPT), NPI, PA reference number
- This is essentially an invoice to the payer

**Who:** Billing department  
**System:** Practice Management System → Clearinghouse → Payer

---

### 6. Adjudication
Payer processes the claim against coverage rules:

| Outcome | Meaning |
|---------|---------|
| **Paid** | Approved — full or partial payment |
| **Denied** | Rejected — wrong codes, missing PA, not covered, step therapy not met |
| **Pended** | On hold — needs additional documentation |

If denied: provider can file an **appeal** using the letter CFIP pre-generated at stage 2.

**Who:** Payer (automated + manual review)  
**System:** Payer adjudication engine

---

### 7. Remittance & Payment
- Payer sends an **ERA** (Electronic Remittance Advice) — details what is paid and why
- Payment transferred to provider via **EFT** (Electronic Funds Transfer)
- **835 transaction** (ANSI X12) carries the remittance data

**Who:** Payer → Provider  
**System:** Clearinghouse → Practice Management System

---

### 8. Patient Billing
- Whatever the payer did not cover (copay, coinsurance, deductible) is billed to the patient
- Provider sends a **patient statement**
- Patient pays via portal, phone, or mail

**Who:** Billing department → Patient  
**System:** Patient billing system

---

## Where CFIP Creates Value

| Problem (without CFIP) | Solution (with CFIP) |
|------------------------|----------------------|
| PA denied weeks later — rework | PA gaps caught at order entry |
| Wrong drug for patient's genotype | PGx alert fires before signing |
| Claim denied — appeal from scratch | Appeal letter pre-generated at point of prescribing |
| Clinician unaware of cost | Indicative cost surfaced in real time |
| Denial patterns unknown | Payer-specific denial risk scored before submission |

**CFIP intervenes at Stage 2 — the earliest and least costly point in the entire workflow.**
A denial caught at Stage 2 costs nothing. A denial discovered at Stage 6 costs weeks of rework
and risks no payment at all.

---

## Key Standards & Protocols

| Standard | Used for |
|----------|----------|
| SMART on FHIR (OAuth 2.0 + RS384 JWT) | Secure Epic authentication |
| HL7 FHIR R4 | Patient data (medications, observations, coverage) |
| CDS Hooks (`order-select`) | Real-time hook firing from Epic |
| CPIC Guidelines | PGx drug-gene interaction rules |
| ANSI X12 837/835 | Claim submission / remittance |
| ICD-10 / NDC / CPT | Medical coding on claims |
