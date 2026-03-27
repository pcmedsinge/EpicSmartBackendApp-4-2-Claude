# Phase 6 Plan — Real SMART on FHIR Backend Integration

> Keep this file in your workspace root alongside requirement.md and architecture.md.
> Point Claude Code to this file when starting Phase 6.

---

## Goal

Make CFIP a genuine SMART on FHIR Backend App by wiring real Epic FHIR API calls.
Currently the JWT auth is registered with Epic but never exercised — the orchestrator reads
only from CDS Hooks prefetch and synthetic overlay. Phase 6 fixes this: when a hook fires,
CFIP authenticates with Epic M2M (JWT → access token), fetches the patient's real FHIR data,
and uses synthetic overlay only where real data is missing.

**This is what makes the demo honest** — and is the most impressive moment to show:
the processing animation displays "Authenticating with Epic… ✓ · Fetching FHIR record… ✓".

---

## "Done" Criteria

- CFIP obtains a real Epic access token using JWT client assertion (RS384) on every hook call
- CFIP calls Epic FHIR R4 API: Patient, MedicationRequest, Observation, Condition, Coverage
- Synthetic overlay fills only the fields not found in real Epic FHIR data
- Each CDS card shows a data source tag: "FHIR" or "Synthetic" per data point
- `GET /.well-known/jwks.json` returns the public key — Epic app JWK URL no longer returns 404
- Harness processing animation shows "Authenticating with Epic… ✓" and "Fetching FHIR record… ✓"
- All 4 scenarios still produce correct cards (data source may change, intelligence must not)
- Tests validate auth flow, FHIR fetch, and synthetic fallback independently
- Graceful degradation: if Epic FHIR call fails, synthetic overlay covers 100% — no card failure

---

## What Changes From Phase 5

```
Phase 5 (prefetch + synthetic only):
  Hook → orchestrator →
    reads prefetch data (patient, medications from hook payload)
    fills ALL clinical gaps from synthetic overlay
    runs evidence chain → cards

Phase 6 (real FHIR + selective synthetic):
  Hook → orchestrator →
    1. AUTH:  JWT assertion → Epic token endpoint → access_token
    2. FETCH: call Epic FHIR API for real patient data
    3. MERGE: real data + synthetic overlay for gaps only
    4. CHAIN: evidence chain (unchanged logic)
    5. TAG:   each evidence item tagged "fhir" or "synthetic"
    6. COMPOSE: cards + data source indicator
```

The evidence chain logic (denial scoring, NCCN validation, PGx checking, PA building)
is **unchanged**. Only the data layer changes — from prefetch-only to FHIR-first.

---

## SMART Backend Services Auth Flow

This is the M2M flow that justifies the "SMART on FHIR Backend App" claim:

```
CFIP                                    Epic Token Endpoint
  |                                           |
  |-- Build JWT client assertion:             |
  |   {                                       |
  |     "iss": client_id,                     |
  |     "sub": client_id,                     |
  |     "aud": token_endpoint_url,            |
  |     "jti": uuid (unique per request),     |
  |     "exp": now + 300s                     |
  |   }                                       |
  |   Signed with RS384 private key           |
  |                                           |
  |-- POST /oauth2/token ─────────────────────>
  |   grant_type=client_credentials           |
  |   client_assertion_type=urn:ietf:...jwt   |
  |   client_assertion=<signed JWT>           |
  |                                           |
  |<── { access_token, expires_in } ─────────|
  |                                           |
  |-- GET /api/FHIR/R4/Patient/{id} ─────────>
  |   Authorization: Bearer <access_token>    |
  |                                           |
  |<── { FHIR Patient resource } ────────────|
```

**Token caching:** Access tokens from Epic are valid for ~1 hour.
CFIP caches the token in memory and reuses it across hook calls until it expires.
This avoids a round-trip to the token endpoint on every hook.

---

## Architecture: FHIR Data Layer

### FhirDataBundle — what we fetch per patient

```python
@dataclass
class FhirDataBundle:
    """All FHIR data fetched for one patient. Fields are None if not found."""
    patient:          dict | None     # Patient demographics
    medications:      list[dict]      # MedicationRequest resources
    lab_observations: list[dict]      # Observation (category=laboratory)
    conditions:       list[dict]      # Condition resources (diagnoses)
    coverage:         list[dict]      # Coverage resources (insurance)
    fetch_errors:     list[str]       # Any 404/500 errors per resource type
    fetched_from:     str             # "epic_fhir" | "prefetch_only"
```

### Synthetic overlay strategy

The synthetic overlay from Phase 3/4 is preserved but becomes selective:

```
For each data field needed by the evidence chain:
  1. Check FhirDataBundle (real Epic data)
  2. If found and non-empty → use it, tag as "fhir"
  3. If missing or empty → check synthetic overlay, tag as "synthetic"
  4. If neither → flag as gap in evidence chain log
```

**What Epic sandbox will likely have (real data):**
- Patient demographics (name, DOB, gender) ✓
- Some MedicationRequest history ✓
- Some Observation (basic labs) — may or may not match demo needs

**What Epic sandbox will NOT have (synthetic fills these):**
- CYP2C19 genotype (PGx — Scenario B)
- PD-L1 score (Scenario C)
- Prior carboplatin regimen (Scenario C)
- Specific Aetna denial history (Scenario D)
- A1C of exactly 7.5% (Scenario A — real value may differ)

---

## Deliverables (Build Order)

### D1: JWK Set Endpoint
**File:** `app/api/jwks.py` + register in `app/main.py`

Add `GET /.well-known/jwks.json` endpoint that returns the public key in JWK format.
Epic calls this to verify CFIP's JWT signature. Currently returns 404 — this must be fixed
before any real auth call can succeed.

```python
@router.get("/.well-known/jwks.json")
async def jwks():
    """Return the public JWK Set so Epic can verify our JWT client assertions."""
    # Read public key from EPIC_PRIVATE_KEY_PATH (extract public part)
    # Format as JWK: { "keys": [{ "kty": "RSA", "kid": key_id, "n": ..., "e": ... }] }
```

**Why first:** Epic validates the JWK URL when the app is registered. Getting this working
unblocks all auth testing.

### D2: FHIR Client Service
**File:** `app/fhir/epic_client.py`

Async FHIR client with JWT auth, token caching, and graceful error handling.

```python
class EpicFHIRClient:
    def __init__(self):
        # Load settings: client_id, private_key_path, key_id, token_endpoint, fhir_base_url

    async def get_access_token(self) -> str:
        """
        JWT client assertion → POST to Epic token endpoint → access_token.
        Caches token until expiry. Thread-safe via asyncio.Lock.
        C# analogy: ITokenService with in-memory cache + refresh logic.
        """

    async def fetch_patient_bundle(self, patient_id: str) -> FhirDataBundle:
        """
        Fetch all relevant FHIR resources for a patient.
        Returns FhirDataBundle with whatever Epic has.
        Never raises — errors logged and returned in fetch_errors.
        """
        # Parallel fetch: Patient + MedicationRequest + Observation + Condition + Coverage
        # Use asyncio.gather() — all 5 calls in parallel, ~1 FHIR round-trip time total
```

**Error handling contract:** Every FHIR call is wrapped in try/except.
A 404 (patient not in Epic sandbox) is not an error — it means synthetic overlay covers everything.
A 401 means token expired — invalidate cache, re-auth, retry once.

**C# analogy:** `IEpicFhirClient` with `GetAccessTokenAsync()` using `IMemoryCache` for token.

### D3: Orchestrator Update — FHIR-First Data Layer
**File:** Update `app/agents/orchestrator.py`

Replace prefetch-only reads with FHIR-first fetch + synthetic overlay merge.

```python
# Phase 5:
patient_data = _extract_from_prefetch(hook_request.prefetch)
clinical_data = synthetic_overlay.fill(patient_id, drug_name)

# Phase 6:
fhir_bundle = await epic_client.fetch_patient_bundle(patient_id)
patient_data = _merge_fhir_and_prefetch(fhir_bundle, hook_request.prefetch)
clinical_data = synthetic_overlay.fill_gaps(patient_id, drug_name, fhir_bundle)
```

Data source tracking added to AgentResult:
```python
class AgentResult(BaseModel):
    ...
    data_sources: dict[str, str]  # field_name → "fhir" | "synthetic" | "prefetch"
    fhir_fetched: bool            # True if Epic FHIR call succeeded
```

### D4: Synthetic Overlay — Gap-Fill Mode
**File:** Update `app/data/seed_synthetic.py` + overlay logic

Change overlay from "always apply" to "fill gaps only":

```python
def fill_gaps(patient_id: str, drug_name: str, fhir_bundle: FhirDataBundle) -> SyntheticData:
    """
    Return synthetic data only for fields missing in fhir_bundle.
    Fields found in FHIR are passed through unchanged.
    """
```

The `USE_SYNTHETIC_OVERLAY` flag in `.env` is preserved:
- `True` (default) = synthetic fills gaps → demo always works
- `False` = pure FHIR only → for real hospital deployments

### D5: Harness Processing Steps Update
**File:** Update `tools/cds_hooks_harness/static/index.html`

Update `SCENARIO_META` steps to include auth and FHIR fetch:

```javascript
// Scenario A steps:
steps: ["Auth with Epic", "Fetch FHIR Record", "Payer Rules", "Denial Risk", "AI Narrative"]

// Scenario B steps:
steps: ["Auth with Epic", "Fetch FHIR Record", "PGx Genotype", "CPIC Guidelines", "Safety Alert"]

// Scenario C steps:
steps: ["Auth with Epic", "Fetch FHIR Record", "NCCN Pathway", "Biomarker Check", "PA Bundle", "AI Narrative"]

// Scenario D steps:
steps: ["Auth with Epic", "Fetch FHIR Record", "Payer History", "Denial Patterns", "Appeal Draft"]
```

### D6: Data Source Tags on Cards
**File:** Update `app/intelligence/card_composer.py`

Add a data source footnote to each card detail when Phase 6 FHIR client is active:

```
### Data Sources
✓ Patient demographics — Epic FHIR R4
✓ Medications — Epic FHIR R4
⚡ PD-L1 score — Synthetic overlay (not in Epic sandbox)
⚡ Prior regimens — Synthetic overlay (not in Epic sandbox)
```

This makes the demo honest and impressive — shows what's real vs what's filled in for demo purposes.

### D7: Tests
**File:** `tests/test_fhir_client.py` + updates to `tests/test_orchestrator.py`

```python
# test_fhir_client.py
- test_jwt_assertion_built_correctly()       # JWT has correct claims, signed with RS384
- test_token_cached_across_calls()          # Second call uses cached token
- test_token_refreshed_on_expiry()          # Expired token triggers re-auth
- test_fhir_fetch_returns_bundle()          # Successful fetch populates FhirDataBundle
- test_fhir_404_handled_gracefully()        # Missing patient → empty bundle, no exception
- test_fhir_401_triggers_reauth()           # 401 → invalidate cache → retry → success

# test_orchestrator.py additions
- test_fhir_data_tagged_as_fhir_source()    # Real FHIR data tagged correctly
- test_synthetic_fills_fhir_gaps()          # Missing FHIR field → synthetic fills it
- test_cards_produced_when_fhir_fails()     # Full Epic outage → synthetic covers 100%
```

---

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Token caching | In-memory `asyncio.Lock` | Epic tokens valid ~1hr; avoid token endpoint on every hook call |
| FHIR fetch strategy | Parallel `asyncio.gather()` | 5 resource types in one round-trip time instead of 5 sequential calls |
| Synthetic overlay | Gap-fill mode (not replace mode) | Real data takes priority; synthetic only patches what's missing |
| Error handling | Never raise, always fallback | CDS Hooks must respond in <5s; FHIR failure must not break the hook |
| JWK endpoint | Serve from same FastAPI app | No separate service needed; same zrok URL serves both CDS Hooks and JWK |
| Data source tagging | Per-field in card detail | Transparency for demo; shows reviewers exactly what came from Epic |

---

## Epic Sandbox Expectations

The two demo patients exist in Epic's public sandbox:
- `erXuFYUfucBZaryVksYEcMg3` — Derrick Lin (Scenarios A, B)
- `eAB3mDIBBcyUKviyzrxsnAw3` — Alex Garcia (Scenarios C, D)

**What Epic sandbox will likely return:**
- Patient demographics ✓ (name, DOB, gender)
- Some MedicationRequest history ✓ (may not include Ozempic or Keytruda)
- Some Observation labs ✓ (basic labs; A1C value may differ from demo)
- Some Condition entries ✓ (diagnoses may or may not match demo)
- Coverage ✗ (Epic public sandbox often returns no Coverage)

**What synthetic overlay will always cover:**
- CYP2C19 genotype (no real genomic data in public sandbox)
- PD-L1 score (not a standard lab in sandbox)
- Prior carboplatin regimen (not in sandbox medication history)
- Aetna denial history (no claims data in sandbox)
- A1C = 7.5% if real value is missing or different

**This is expected and correct.** The demo is designed to show what CFIP does
when real data is available AND when it's not. The data source tags make this visible.

---

## Files Created/Modified in Phase 6

New files:
- `app/fhir/epic_client.py` — FHIR client with JWT auth + token cache
- `app/fhir/__init__.py`
- `app/api/jwks.py` — JWK Set endpoint
- `app/models/fhir_bundle.py` — FhirDataBundle dataclass
- `tests/test_fhir_client.py`

Modified files:
- `app/main.py` — register JWK Set router
- `app/agents/orchestrator.py` — FHIR-first data layer, data_sources tracking
- `app/data/seed_synthetic.py` — gap-fill mode
- `app/models/domain.py` — add data_sources, fhir_fetched to AgentResult
- `app/intelligence/card_composer.py` — data source footnote on cards
- `tools/cds_hooks_harness/static/index.html` — auth + FHIR steps in animation

---

## How to Verify Phase 6 Is Working

1. Start server: `python -m app.main`
2. Verify JWK endpoint: `curl http://localhost:5000/.well-known/jwks.json`
   → Should return JSON with RSA public key. No 404.
3. Open harness: `http://localhost:5000/harness/`
4. Select Scenario A → click "Analyze Order"
5. Watch processing animation:
   - "Auth with Epic... ✓" — real JWT exchange happened
   - "Fetch FHIR Record... ✓" — real Epic FHIR API called
6. Check cards — look for data source footnote:
   - "Patient demographics — Epic FHIR R4" = real data
   - "A1C score — Synthetic overlay" = gap filled
7. In server logs, verify:
   - `INFO: Epic access token obtained` appears
   - `INFO: FHIR Patient fetched: erXuFYUfucBZaryVksYEcMg3`
   - `INFO: Synthetic overlay filled 3 fields for Scenario A`
8. Run tests: `pytest tests/test_fhir_client.py -v`
9. Test graceful degradation: set `EPIC_FHIR_BASE_URL` to invalid URL in .env → restart →
   cards should still work (100% synthetic) with a log warning

---

## Prompt for Claude Code

Paste this when starting Phase 6 in CLI:

> Read requirement.md, architecture.md, and phase6-plan.md from this workspace.
> These are your ground truth.
>
> We're starting Phase 6 — Real SMART on FHIR Backend Integration.
> Follow the deliverable order in phase6-plan.md (D1 through D7).
>
> Key decisions already made:
> - Start with D1 (JWK Set endpoint) — Epic app currently shows 404 for JWK URL
> - FHIR client uses asyncio.gather() for parallel resource fetching
> - Token cached in memory with asyncio.Lock (not Redis, not DB — single process dev server)
> - Synthetic overlay switches to gap-fill mode (not replace mode)
> - Error handling contract: FHIR failures never raise — always fall back to synthetic
> - Data source tagged per field on cards (transparent about real vs synthetic)
>
> Epic sandbox patients: erXuFYUfucBZaryVksYEcMg3 (Derrick Lin), eAB3mDIBBcyUKviyzrxsnAw3 (Alex Garcia)
> Server port: 5000 (set in .env APP_PORT=5000)
>
> Rules: One file at a time. Explain before writing. Wait for my approval before proceeding.
> Comment Python idioms that wouldn't be obvious to a C# developer.
