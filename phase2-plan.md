# Phase 2 Plan — CDS Hooks Service

> Keep this file in your workspace root alongside requirement.md and architecture.md.
> Point Claude Code to this file when starting Phase 2.

---

## Goal

Wire up the CDS Hooks plumbing so that a test harness can fire a synthetic `order-select` hook at CFIP and get back a valid CDS card — no agent logic yet, just correct request/response shape.

## "Done" Criteria

- `GET /cds-services` returns valid discovery JSON per CDS Hooks spec
- `POST /cds-services/cfip-order-intelligence` accepts a hook request and returns a stub card
- Test harness builds a realistic hook request (with Epic sandbox prefetch data), POSTs it, and prints the card
- Browser UI renders the card visually
- Tests validate spec compliance

---

## What Are CDS Hooks? (Quick Reference)

CDS Hooks is a standard that lets an EHR (like Epic) call out to external services at key moments in a clinician's workflow. Think of it as event-driven webhooks for healthcare.

### How It Works

1. **EHR registers hooks** — Epic knows about our service because we publish a discovery endpoint
2. **Clinician does something** — e.g., selects a medication in the order entry screen
3. **Epic fires a hook** — sends an HTTP POST to our service with context (patient, what was ordered, etc.)
4. **We respond with cards** — JSON objects that Epic renders as visual cards in the clinician's screen

### The Three Hooks CFIP Will Support

| Hook | When It Fires | What CFIP Does |
|------|--------------|----------------|
| `patient-view` | Clinician opens a patient chart | Show pre-emptive denial risks, PGx alerts, coverage summary |
| `order-select` | Clinician picks a drug/procedure in order entry | **This is Phase 2's focus.** Show denial risk, cost, PGx for the selected order |
| `order-sign` | Clinician is about to sign/submit the order | Last chance: submit PA, block unsafe orders, confirm everything |

### Key Concepts

- **Discovery endpoint** (`GET /cds-services`): Returns a JSON list of services we offer — tells Epic "I handle order-select hooks, and when you call me, please include Patient and MedicationRequest data."
- **Hook request**: The POST body Epic sends us. Contains `hook` (which event), `hookInstance` (unique ID), `context` (what triggered it — e.g., selected medication), and optionally `prefetch` (FHIR data Epic pre-fetched for us).
- **Prefetch**: We declare FHIR queries in our discovery response. Epic runs them and includes the results in the hook request — saves us from making separate FHIR calls. If Epic doesn't send prefetch, we fetch the data ourselves.
- **Cards**: Our response. Each card has a `summary` (one-line text), `detail` (markdown), `indicator` (info/warning/critical), `source`, optional `suggestions` (actions the clinician can take), and `links` (e.g., launch SMART app).
- **Suggestions**: Actionable buttons on the card — e.g., "Submit PA Now" or "Switch to alternative drug."
- **Links**: URLs on the card — e.g., "View Full Analysis" launches our SMART companion app.

### Example Flow (What Phase 2 Builds)

```
Clinician selects Ozempic in Epic
        ↓
Epic POSTs to our /cds-services/cfip-order-intelligence
  {
    hook: "order-select",
    context: { patientId: "abc123", selections: [{resource: MedicationRequest for Ozempic}] },
    prefetch: { patient: {Patient resource}, medications: {MedicationRequest resource} }
  }
        ↓
CFIP validates the request, returns:
  {
    cards: [{
      summary: "Ozempic: 87% approval | $150/mo | No PGx issues",
      indicator: "info",
      detail: "Step therapy met (metformin 6mo). UHC prior auth required.",
      source: { label: "CFIP" },
      suggestions: [{ label: "Submit PA Now", ... }],
      links: [{ label: "View Full Analysis", type: "smart", ... }]
    }]
  }
        ↓
Epic renders the card in the clinician's screen
(For us: test harness renders it in browser)
```

### CDS Hooks Spec Reference

- Full spec: https://cds-hooks.hl7.org/2.0/
- Hook catalog: https://cds-hooks.hl7.org/hooks/
- Card attributes: https://cds-hooks.hl7.org/2.0/#card-attributes

---

## Deliverables (Build Order)

### D1: CDS Hooks Pydantic Models
**File:** `app/models/cds_hooks.py`

Request and response models per CDS Hooks 2.0 spec:
- `HookRequest` — hook, hookInstance, context, prefetch (optional), fhirServer, fhirAuthorization
- `Card` — summary, detail, indicator (info/warning/critical), source, suggestions, links
- `Suggestion` — label, uuid, actions
- `Link` — label, url, type (absolute/smart)
- `CdsResponse` — cards list

These are the contract. Everything else depends on them.

### D2: Discovery Endpoint
**File:** `app/api/cds_hooks.py`
**Endpoint:** `GET /cds-services`

Returns JSON listing our service:
```json
{
  "services": [{
    "hook": "order-select",
    "id": "cfip-order-intelligence",
    "title": "CFIP Order Intelligence",
    "description": "Clinical-financial intelligence for medication orders",
    "prefetch": {
      "patient": "Patient/{{context.patientId}}",
      "medications": "MedicationRequest?patient={{context.patientId}}&_count=50"
    }
  }]
}
```

The `prefetch` templates tell Epic what FHIR data to include in hook requests.

### D3: Order-Select Hook Handler
**File:** same `app/api/cds_hooks.py`
**Endpoint:** `POST /cds-services/cfip-order-intelligence`

- Validates incoming request against HookRequest model
- Extracts patient info and medication from context/prefetch
- If prefetch is missing, falls back to FHIR client (from Phase 1) to fetch Patient
- Returns a **stub card** with hardcoded but realistic content
- Stub content: "87% approval | $150/mo | No PGx issues" — proves the shape works

No agent logic, no denial scoring, no real intelligence. That's Phase 3+.

### D4: Test Harness — Backend Script
**File:** `tools/cds_hooks_harness/harness.py`
**File:** `tools/cds_hooks_harness/scenarios.py`

`scenarios.py` defines demo scenario data:
- Scenario A (Ozempic): patient ID, medication code, what prefetch data looks like
- Uses real FHIR data from Epic sandbox where possible

`harness.py`:
- Builds a spec-compliant `order-select` hook request for a chosen scenario
- POSTs it to `http://localhost:8000/cds-services/cfip-order-intelligence`
- Prints the card response to terminal
- Can optionally fetch real prefetch data from Epic sandbox first

### D5: Test Harness — Browser UI
**File:** `tools/cds_hooks_harness/static/index.html`
**Served at:** `http://localhost:8000/harness` (static file served by FastAPI)

Simple HTML page that:
- Has a dropdown to pick a scenario
- Fires the hook request via JavaScript fetch()
- Renders returned cards with colored indicator bars (info=blue, warning=orange, critical=red)
- Shows summary, detail, suggestion buttons, links

Minimal styling — functional, not polished. Phase 6 will make it pretty.

### D6: Tests
**File:** `tests/test_cds_hooks.py`

- Discovery endpoint returns valid service list
- Hook handler accepts valid order-select request → returns cards
- Hook handler rejects malformed request → returns 400
- Card response has required fields per spec
- Prefetch fallback works (request without prefetch triggers FHIR fetch)

---

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Prefetch strategy | Declare prefetch, use if present, fetch ourselves if missing | Production-correct pattern; barely more work |
| Harness UI | Minimal HTML | Polishing happens in Phase 6 |
| Hook handler logic | Stub (hardcoded card) | Real intelligence comes in Phase 3-5 |
| Which hook first | order-select | Most valuable for demo scenarios; order-sign and patient-view added later |

---

## Files Created/Modified in Phase 2

New files:
- `app/models/cds_hooks.py`
- `app/api/__init__.py`
- `app/api/cds_hooks.py`
- `tools/cds_hooks_harness/harness.py`
- `tools/cds_hooks_harness/scenarios.py`
- `tools/cds_hooks_harness/static/index.html`
- `tests/test_cds_hooks.py`

Modified files:
- `app/main.py` (register CDS Hooks routes, serve static harness)

---

## Prompt for Claude Code

Paste this when starting Phase 2 in CLI:

> Read requirement.md, architecture.md, and phase2-plan.md from this workspace. These are your ground truth.
>
> We're starting Phase 2 — CDS Hooks Service. Follow the deliverable order in phase2-plan.md (D1 through D6).
>
> Rules: One file at a time. Explain before writing. Wait for my approval before proceeding. Comment Python idioms that wouldn't be obvious to a C# developer.
