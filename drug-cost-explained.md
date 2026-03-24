# Drug Cost Data in Healthcare — How It Really Works

> Reference document for understanding how patient drug costs are determined
> in real production systems vs. CFIP's current synthetic approach.

---

## 1. Why Drug Cost Is Complicated

When a doctor prescribes Ozempic, the question "what will this cost the patient?" seems simple. It's not. The answer depends on at least five variables that interact with each other:

- Which insurance plan the patient has
- What tier Ozempic is on in that plan's formulary
- Whether the patient has met their annual deductible
- Which pharmacy they use (in-network vs. out-of-network)
- Where they are in their benefit year (especially for Medicare)

Two patients on the same plan ordering the same drug can pay wildly different amounts.

---

## 2. The Insurance Formulary — Drug Tiers

Every insurance plan maintains a **formulary** — a list of covered drugs organized into cost tiers:

| Tier | What's In It | Typical Cost to Patient |
|------|-------------|------------------------|
| Tier 1 | Generic drugs (metformin, lisinopril) | $5–15 copay |
| Tier 2 | Preferred brand drugs | $25–50 copay |
| Tier 3 | Non-preferred brand drugs | $50–100 copay or 20–35% coinsurance |
| Tier 4 | Specialty drugs (Ozempic, Keytruda, biologics) | 25–50% coinsurance, often $200–500+ |

**Copay** = fixed dollar amount ($50 per fill).
**Coinsurance** = percentage of the drug cost (30% of $900 = $270).

The formulary tells you what tier. The plan's benefit design tells you what that tier costs. You need both pieces.

---

## 3. The Patient's Benefit Status

Even knowing the tier isn't enough. The patient's individual benefit status matters:

**Deductible:** Many plans require the patient to pay full price for drugs until they've spent a certain amount (e.g., $1,500/year). If the patient hasn't met their deductible yet, that Tier 3 drug isn't $50 — it's full retail price ($900+).

**Out-of-pocket maximum:** After the patient has spent a certain total amount (e.g., $8,000/year), insurance covers 100%. A patient who hit their max in September pays $0 for a December prescription.

**Medicare donut hole:** Medicare Part D has a unique "coverage gap" where after a certain spending threshold, the patient pays a higher percentage until they reach catastrophic coverage. This affects many elderly patients on specialty drugs.

**Network status:** Using an out-of-network pharmacy can double or triple the cost, or the drug might not be covered at all.

---

## 4. Where Real Production Systems Get Cost Data

### Source 1 — Real-Time Pharmacy Benefit Check (RTPBC)

**What it is:** An NCPDP standard where you send a drug + patient + pharmacy to the Pharmacy Benefit Manager (PBM) and get back the *exact* cost the patient would pay *today*.

**How it works:**
```
CFIP → Surescripts/PBM: "What would Patient X pay for Ozempic at CVS?"
PBM → CFIP: "$147.32 copay (deductible met, Tier 4, in-network)"
```

**Accuracy:** ±$0. This is the gold standard — it accounts for deductible status, formulary tier, network, everything.

**Who operates it:** Surescripts is the primary network. PBMs like Express Scripts, CVS Caremark, and OptumRx connect through it.

**Access requirements:** Production contract with Surescripts, which requires being a certified healthcare application. Not available in sandbox.

### Source 2 — FHIR ExplanationOfBenefit (Claims History)

**What it is:** The `ExplanationOfBenefit` FHIR resource contains the patient's past claims — what drugs they filled, what they paid, what insurance covered.

**How to use it for cost estimation:** Look at past fills for the same drug class and tier. If the patient paid $150 for a Tier 4 drug last month, they'll likely pay something similar this month (unless a deductible reset happened).

**Accuracy:** ±$20–50. Good estimate but doesn't account for benefit year changes or formulary updates.

**Access:** Available through Epic FHIR API. This is what CFIP can use in production without special contracts.

### Source 3 — Formulary APIs

**What it is:** Some PBMs and payers expose their formulary data (which drugs are on which tiers) via APIs.

**CMS requirement:** Medicare Part D plans are required to publish formularies in a standardized format. Commercial plans are less consistent.

**Accuracy:** Tells you the tier and general cost structure, but not the patient's specific deductible/OOP status. ±$50–100.

**Access:** Varies by payer. Medicare data is publicly available. Commercial plans often require partnerships.

### Source 4 — Drug Pricing Databases

**What they are:** Services that provide reference pricing for drugs:

- **AWP (Average Wholesale Price):** A benchmark price, often called "sticker price." Not what anyone actually pays, but widely used for calculations.
- **NADAC (National Average Drug Acquisition Cost):** What pharmacies actually pay to acquire the drug. Published by CMS weekly.
- **WAC (Wholesale Acquisition Cost):** Manufacturer's list price to wholesalers.

**Providers:** First Databank (FDB), Medi-Span, Medispan, GoodRx (consumer-facing).

**Accuracy:** Gives you a ballpark retail price (±$100–200). Useful as a last resort when plan-specific data isn't available.

---

## 5. What a Production CFIP Would Do

In a real deployment, cost estimation works in layers — try the most precise source first, fall back gracefully:

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: RTPBC (real-time PBM check)                    │
│ → Exact cost: $147.32                                   │
│ → Confidence: "Verified with pharmacy benefit manager"   │
│ → Requires: Surescripts contract                        │
├─────────────────────────────────────────────────────────┤
│ Layer 2: Claims history (FHIR ExplanationOfBenefit)     │
│ → Estimated cost: ~$150                                 │
│ → Confidence: "Based on recent claims history"           │
│ → Requires: FHIR access (already have this)             │
├─────────────────────────────────────────────────────────┤
│ Layer 3: Formulary + Coverage tier lookup                │
│ → Estimated cost: $135–175 range                        │
│ → Confidence: "Based on plan formulary"                  │
│ → Requires: Formulary API access or payer partnership   │
├─────────────────────────────────────────────────────────┤
│ Layer 4: Drug pricing database (AWP/NADAC)              │
│ → Estimated cost: ~$900 retail (before insurance)       │
│ → Confidence: "Average retail price — actual cost varies"│
│ → Requires: Pricing database subscription               │
└─────────────────────────────────────────────────────────┘
```

The CDS card would indicate the confidence level:
- "Exact cost: $147.32 (verified)" — from RTPBC
- "Estimated: ~$150/mo (based on plan formulary)" — from formulary lookup
- "Retail price: ~$900/mo (actual cost depends on your plan)" — from pricing database

---

## 6. What CFIP Does Now (Synthetic)

For our development and demo environment, none of the real sources are available:
- No Surescripts contract (RTPBC)
- Limited claims history in Epic sandbox (ExplanationOfBenefit)
- No formulary API access
- No pricing database subscription

So we hardcode cost estimates per scenario in our seed data:

```
Scenario A (Ozempic + UHC): $150/mo
Scenario B (Clopidogrel + generic): $15/mo
Scenario C (Keytruda + specialty): $500/mo
Scenario D (MRI + Aetna): $350 one-time
```

**The architecture supports upgrading.** The bridge pipeline calls a cost estimation function and gets back a number. Whether that function reads from our hardcoded table or queries a real-time PBM API is an implementation detail. The card format, the scoring model, and everything downstream stays the same. When CFIP moves to production, we swap the cost function internals — nothing else changes.

---

## 7. Key Industry Terms

| Term | What It Means |
|------|--------------|
| **PBM** | Pharmacy Benefit Manager — the middleman between insurers, pharmacies, and drug manufacturers. Processes claims, maintains formularies, negotiates prices. Big three: Express Scripts, CVS Caremark, OptumRx. |
| **Formulary** | An insurance plan's list of covered drugs, organized by cost tier |
| **AWP** | Average Wholesale Price — a benchmark/reference price for drugs (not what anyone actually pays) |
| **NADAC** | National Average Drug Acquisition Cost — what pharmacies pay to buy the drug |
| **RTPBC** | Real-Time Pharmacy Benefit Check — exact cost lookup from PBM at point of prescribing |
| **NCPDP** | National Council for Prescription Drug Programs — standards body for pharmacy transactions |
| **Surescripts** | The network that connects EHRs, pharmacies, and PBMs for e-prescribing and benefit checks |
| **Copay** | Fixed dollar amount the patient pays per prescription ($50) |
| **Coinsurance** | Percentage of drug cost the patient pays (30% of $900 = $270) |
| **Deductible** | Amount patient must pay out of pocket before insurance kicks in |
| **OOP Max** | Out-of-pocket maximum — annual spending cap after which insurance covers 100% |
| **Donut hole** | Medicare Part D coverage gap where patient temporarily pays more |
| **Rebate** | Secret discount manufacturers pay to PBMs/insurers — never visible to patients or doctors |

---

*This document was created during CFIP development as a reference for understanding drug cost estimation in healthcare.*
