# CFIP Demo Guide

## Prerequisites

- Python 3.12+ with `.venv` already set up
- `keys/privatekey.pem` present (shared with other Epic backend app)
- `.env` file with secrets filled in (see step 1 below)
- zrok installed and the `pcmsmartbackendapp1` tunnel available (optional — only needed if demoing real Epic FHIR calls)

---

## Step 1 — Set up `.env`

Copy the template and fill in the two `REPLACE_ME` values:

```
copy .env.template .env
```

Open `.env` and set:

```
EPIC_GROUP_ID=e3iabhmS8rsueyz7vaimuiaSmfGvi.QwjVXJANlPOgR83
OPENAI_API_KEY=<your OpenAI key>
```

Everything else in `.env.template` is already correct.

---

## Step 2 — Start the CFIP backend

```bash
.venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 5000
```

On first startup the server seeds the SQLite database (payer rules, CPIC drug-gene rules, synthetic patient overlay). You should see:

```
INFO  DB initialised
INFO  Payer rules seeded
INFO  CPIC rules seeded
INFO  Application startup complete
```

Verify it's running: http://localhost:5000/docs

---

## Step 3 — Start the demo harness

In a **second terminal**:

```bash
.venv\Scripts\activate
python tools/cds_hooks_harness/harness.py
```

Open the harness UI: **http://localhost:5000/harness/**

---

## Step 4 — Run the demo

### Walk through the 4 scenarios

| Button | What it shows |
|--------|---------------|
| **Scenario A — Semaglutide** | GLP-1 prior auth + high denial risk + AI appeal draft |
| **Scenario B — Clopidogrel** | PGx safety alert (CYP2C19 poor metaboliser) + drug switch suggestion |
| **Scenario C — Pembrolizumab** | Oncology NCCN pathway validation + PA bundle |
| **Scenario D — Standard Rx** | Low-risk standard prescription, informational card only |

Click **▶ Run** on any scenario to fire the CDS Hook and see the cards.

### Useful buttons

- **Run All Scenarios** — fires all 4 in parallel, shows side-by-side results
- **Compare All 4 Scenarios** — opens a modal comparing every card across scenarios
- **⬡ CFIP Flow** — opens the 3-tab explainer modal:
  - **① Overview** — what CFIP does step by step
  - **② Visual Flow** — swimlane diagram of the orchestrator pipeline
  - **③ Epic Production Flow** — how this works in real Epic Hyperspace (with animated flow)
- **View Full Analysis** on any card — shows the agent reasoning trace

---

## Step 5 — Optional: enable real Epic FHIR calls

By default the orchestrator uses the prefetch bundle + synthetic overlay.
To exercise the actual Epic FHIR API:

1. Start zrok tunnel in a third terminal:
   ```
   zrok share reserved pcmsmartbackendapp1
   ```
2. The tunnel URL `https://pcmsmartbackendapp1.share.zrok.io` is already registered with Epic.
3. The app will automatically attempt real FHIR calls (Patient, MedicationRequest, Observation, Coverage, Condition) and fall back to synthetic overlay for any missing data.

---

## What the demo proves

1. **Real Epic FHIR auth** — JWT RS384 signed token, Epic validates against registered public key
2. **Agentic orchestration** — PLAN → FHIR → EXECUTE → AI → COMPOSE, fully automated
3. **Deterministic safety** — CPIC drug-gene rules and NCCN pathway validation never touch an LLM
4. **AI where appropriate** — GPT-4o-mini for clinical narrative and PA appeal letter only
5. **CDS Hooks spec compliant** — any EHR supporting CDS Hooks 1.0 can consume CFIP cards

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Port 8000 already in use | Change `APP_PORT` in `.env` and update harness `BASE_URL` in `harness.py` |
| `CPIC rules table not found` | Server didn't complete startup — wait for "Application startup complete" log line |
| OpenAI cards show template fallback | `OPENAI_API_KEY` missing or invalid in `.env` — template fallback is intentional and still shows correct structure |
| Appeal draft link returns 404 | Appeal store is in-memory — restarting the server clears it; re-run the scenario |
| zrok tunnel not connecting | Run `zrok status` — if the share is reserved it just needs `zrok share reserved pcmsmartbackendapp1` |
