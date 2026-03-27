"""
Tests for the Phase 5 agentic orchestrator and supporting components.

Coverage:
  - should_generate_appeal(): guard logic for all risk tiers
  - AppealGenerator.generate(): letter fields, source attribution, denial reason inference
  - Orchestrator.process(): routing to correct chain for all 4 drug classes
  - Scenario cards: expected indicators + content for all 4 demo scenarios
  - Narrative fallback: template used when OpenAI unavailable

Run with:
    pytest tests/test_orchestrator.py -v

C# analogy: a test class per major component, each using xUnit Facts
and Theories. TestClient mirrors WebApplicationFactory<Program>.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.agents.orchestrator import Orchestrator
from app.intelligence.appeal_generator import AppealGenerator, should_generate_appeal
from app.main import app
from app.models.domain import AgentResult, AppealLetter, DenialRiskResult, PipelineError


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """
    FastAPI TestClient — runs the full app in-process.
    scope="module" reuses the client across all tests in this file.
    C# analogy: IClassFixture<WebApplicationFactory<Program>>.
    """
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def _post_hook(client, drug_text: str, rxnorm_code: str,
               patient_id: str = "erXuFYUfucBZaryVksYEcMg3") -> dict:
    """
    POST a hook request and return the parsed JSON body.
    Asserts 200 OK — any non-200 fails the test immediately.
    """
    payload = {
        "hook": "order-select",
        "hookInstance": str(uuid.uuid4()),
        "context": {
            "patientId": patient_id,
            "userId": "Practitioner/demo",
            "selections": ["MedicationRequest/demo-order"],
            "draftOrders": {
                "resourceType": "Bundle",
                "entry": [{
                    "resource": {
                        "resourceType": "MedicationRequest",
                        "id": "demo-order",
                        "status": "draft",
                        "intent": "proposal",
                        "subject": {"reference": f"Patient/{patient_id}"},
                        "medicationCodeableConcept": {
                            "coding": [{
                                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                "code": rxnorm_code,
                                "display": drug_text,
                            }],
                            "text": drug_text,
                        },
                    }
                }],
            },
        },
        "prefetch": {
            "patient": {
                "resourceType": "Patient",
                "id": patient_id,
                "name": [{"use": "official", "family": "Test", "given": ["Patient"]}],
            }
        },
    }
    response = client.post("/cds-services/cfip-order-intelligence", json=payload)
    assert response.status_code == 200
    return response.json()


def _make_denial_risk(prob: int, unmet: list[str] | None = None) -> DenialRiskResult:
    """Helper: build a minimal DenialRiskResult for appeal guard tests."""
    risk = "high" if prob < 50 else ("moderate" if prob < 80 else "low")
    indicator = "critical" if prob < 50 else ("warning" if prob < 80 else "info")
    return DenialRiskResult(
        approval_probability=prob,
        risk_level=risk,
        indicator=indicator,
        unmet_criteria=unmet or [],
        drug_class="glp1",
        drug_name="Ozempic",
        payer="UHC",
    )


# ---------------------------------------------------------------------------
# should_generate_appeal() — guard function unit tests
# ---------------------------------------------------------------------------

class TestShouldGenerateAppeal:
    """
    Unit tests for the appeal-generation guard function.

    Rules under test:
      score < 50  (critical)  → always generate
      50 ≤ score < 80 (moderate) → generate only if prior denial in unmet_criteria
      score ≥ 80  (low)      → never generate
    """

    def test_returns_false_for_no_denial_risk(self):
        """No denial_risk field → no appeal needed."""
        result = AgentResult(drug="Ozempic", drug_class="glp1")
        assert should_generate_appeal(result) is False

    def test_returns_false_when_denial_risk_is_none(self):
        """None denial_risk (e.g. when bridge failed) → no appeal (can't generate without data)."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=None,
        )
        assert should_generate_appeal(result) is False

    def test_high_risk_score_always_generates(self):
        """Score of 25 (critical) → always generate, regardless of unmet criteria."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(25),
        )
        assert should_generate_appeal(result) is True

    def test_score_just_below_threshold_generates(self):
        """Score of 49 is still < 50 → generate."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(49),
        )
        assert should_generate_appeal(result) is True

    def test_low_risk_score_never_generates(self):
        """Score of 87 (low) → no appeal needed."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(87),
        )
        assert should_generate_appeal(result) is False

    def test_score_exactly_at_low_threshold_no_appeal(self):
        """Score of 80 is ≥ 80 → low risk, no appeal."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(80),
        )
        assert should_generate_appeal(result) is False

    def test_moderate_risk_with_prior_denial_generates(self):
        """Score 65 (moderate) + 'prior denial history' in unmet criteria → generate."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(65, unmet=["Prior denial history with UHC"]),
        )
        assert should_generate_appeal(result) is True

    def test_moderate_risk_without_prior_denial_no_appeal(self):
        """Score 65 (moderate) but no prior denial mention → no appeal."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(65, unmet=["Step therapy not met"]),
        )
        assert should_generate_appeal(result) is False

    def test_moderate_risk_with_denial_keyword_generates(self):
        """'denial' anywhere in unmet criterion string triggers appeal at moderate risk."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(70, unmet=["2 past denials for step therapy"]),
        )
        assert should_generate_appeal(result) is True

    def test_moderate_risk_with_prior_keyword_generates(self):
        """'prior' anywhere in unmet criterion string triggers appeal at moderate risk."""
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=_make_denial_risk(70, unmet=["Prior auth required but missing"]),
        )
        assert should_generate_appeal(result) is True


# ---------------------------------------------------------------------------
# AppealGenerator — unit tests (async, no HTTP server)
# ---------------------------------------------------------------------------

class TestAppealGenerator:
    """
    Unit tests for AppealGenerator and its private helpers.

    Tests run against the real generator with the real OpenAI client.
    When no API key is configured (CI/test environment), the client
    falls back to the template path — both paths are valid.

    C# analogy: unit tests on a service class, not mocking internals —
    testing observable output, not internal calls.
    """

    @pytest.mark.asyncio
    async def test_generate_returns_appeal_letter(self):
        """generate() always returns an AppealLetter — never raises."""
        denial = _make_denial_risk(30, unmet=["Step therapy: metformin <90 days"])
        result = AgentResult(
            drug="Ozempic",
            drug_class="glp1",
            denial_risk=denial,
            narrative="Patient has T2DM with A1C 7.5%.",
        )
        generator = AppealGenerator()
        letter = await generator.generate(result)

        assert isinstance(letter, AppealLetter)
        assert letter.drug == "Ozempic"
        assert len(letter.content) > 50    # substantive letter, not empty

    @pytest.mark.asyncio
    async def test_generate_source_is_openai_or_template(self):
        """source must be 'openai' or 'template' — never an unknown value."""
        denial = _make_denial_risk(30)
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=denial)
        letter = await AppealGenerator().generate(result)
        assert letter.source in ("openai", "template")

    @pytest.mark.asyncio
    async def test_generate_with_explicit_denial_reason(self):
        """Explicit denial_reason overrides inferred reason."""
        denial = _make_denial_risk(30)
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=denial)
        letter = await AppealGenerator().generate(result, denial_reason="my_custom_reason")
        assert letter.denial_reason == "my_custom_reason"

    @pytest.mark.asyncio
    async def test_generate_addressed_to_medical_director(self):
        """All appeal letters are addressed to the medical director."""
        denial = _make_denial_risk(30)
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=denial)
        letter = await AppealGenerator().generate(result)
        assert letter.addressed_to == "Medical Director"

    @pytest.mark.asyncio
    async def test_generate_infers_denial_reason_step_therapy(self):
        """Step therapy in unmet criteria → step_therapy_not_met reason code."""
        denial = _make_denial_risk(30, unmet=["Step therapy: metformin <90 days"])
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=denial)
        letter = await AppealGenerator().generate(result)
        assert letter.denial_reason == "step_therapy_not_met"

    @pytest.mark.asyncio
    async def test_generate_evidence_references_from_met_criteria(self):
        """evidence_references populated from denial_result.met_criteria (top 5)."""
        denial = _make_denial_risk(30)
        denial = denial.model_copy(update={
            "met_criteria": ["A1C 7.5% — meets threshold", "BMI 33", "Metformin 180 days"]
        })
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=denial)
        letter = await AppealGenerator().generate(result)
        assert len(letter.evidence_references) >= 1
        assert any("A1C" in ref or "BMI" in ref or "Metformin" in ref for ref in letter.evidence_references)

    @pytest.mark.asyncio
    async def test_generate_with_no_denial_risk_returns_letter(self):
        """
        generate() is only called when should_generate_appeal() is True,
        but generate() itself must still be robust to missing denial data.
        """
        result = AgentResult(drug="Ozempic", drug_class="glp1", denial_risk=None)
        letter = await AppealGenerator().generate(result)
        # Should return a template letter — never raise
        assert isinstance(letter, AppealLetter)
        assert len(letter.content) > 0


# ---------------------------------------------------------------------------
# Orchestrator routing — verifies each drug class hits the right chain
# ---------------------------------------------------------------------------

class TestOrchestratorRouting:
    """
    Integration tests that verify the orchestrator routes each drug class
    to the correct evidence chain and populates AgentResult fields.

    Calls Orchestrator.process() directly — no HTTP layer.

    C# analogy: calling the handler directly (bypassing controller routing)
    to test MediatR pipeline logic in isolation.
    """

    @staticmethod
    def _make_request(patient_id: str, drug_text: str, rxnorm_code: str):
        """Build a minimal HookRequest-compatible dict for the orchestrator."""
        from app.models.cds_hooks import HookRequest
        return HookRequest(**{
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": patient_id,
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [{
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [{
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": rxnorm_code,
                                    "display": drug_text,
                                }],
                                "text": drug_text,
                            },
                        }
                    }],
                },
            },
        })

    @pytest.mark.asyncio
    async def test_glp1_chain_name(self):
        """Ozempic routes to the GLP-1 Prior Authorization chain."""
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "semaglutide 0.5 MG/DOSE subcutaneous injection",
            "2200750",
        )
        result = await Orchestrator().process(request)
        assert "GLP-1" in result.chain_name or result.drug_class == "glp1"

    @pytest.mark.asyncio
    async def test_glp1_populates_denial_risk(self):
        """GLP-1 chain produces a DenialRiskResult in AgentResult.denial_risk."""
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "semaglutide 0.5 MG/DOSE subcutaneous injection",
            "2200750",
        )
        result = await Orchestrator().process(request)
        assert isinstance(result.denial_risk, DenialRiskResult)

    @pytest.mark.asyncio
    async def test_pgx_chain_name(self):
        """Clopidogrel routes to the PGx safety chain."""
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "clopidogrel 75 MG oral tablet",
            "309362",
        )
        result = await Orchestrator().process(request)
        assert result.drug_class == "pgx_sensitive"

    @pytest.mark.asyncio
    async def test_pgx_narrative_is_template(self):
        """
        PGx chain must NEVER call OpenAI — narrative_source is always 'template'.
        Safety rule: deterministic text only for safety-critical drug-gene alerts.
        """
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "clopidogrel 75 MG oral tablet",
            "309362",
        )
        result = await Orchestrator().process(request)
        assert result.narrative_source == "template"

    @pytest.mark.asyncio
    async def test_oncology_chain_name(self):
        """Keytruda routes to the Oncology Pathway Validation chain."""
        request = self._make_request(
            "eAB3mDIBBcyUKviyzrxsnAw3",
            "pembrolizumab 100 MG/4ML injection",
            "1547545",
        )
        result = await Orchestrator().process(request)
        assert result.drug_class == "oncology"

    @pytest.mark.asyncio
    async def test_oncology_populates_pa_bundle(self):
        """Oncology chain assembles a PABundle in AgentResult.pa_bundle."""
        from app.models.domain import PABundle
        request = self._make_request(
            "eAB3mDIBBcyUKviyzrxsnAw3",
            "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
            "1547545",
        )
        result = await Orchestrator().process(request)
        assert isinstance(result.pa_bundle, PABundle)

    @pytest.mark.asyncio
    async def test_standard_chain_name(self):
        """MRI Lumbar Spine routes to the Denial Prevention (standard) chain."""
        request = self._make_request(
            "eAB3mDIBBcyUKviyzrxsnAw3",
            "MRI Lumbar Spine",
            "72148",
        )
        result = await Orchestrator().process(request)
        assert result.drug_class == "standard"

    @pytest.mark.asyncio
    async def test_evidence_chain_log_populated(self):
        """Orchestrator always populates evidence_chain_log with at least one entry."""
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "semaglutide 0.5 MG/DOSE subcutaneous injection",
            "2200750",
        )
        result = await Orchestrator().process(request)
        assert len(result.evidence_chain_log) >= 1

    @pytest.mark.asyncio
    async def test_process_always_returns_cards(self):
        """Orchestrator always returns at least one card — never an empty list."""
        request = self._make_request(
            "erXuFYUfucBZaryVksYEcMg3",
            "semaglutide 0.5 MG/DOSE subcutaneous injection",
            "2200750",
        )
        result = await Orchestrator().process(request)
        assert len(result.cards) >= 1


# ---------------------------------------------------------------------------
# Scenario card content — integration tests via HTTP
# ---------------------------------------------------------------------------

class TestScenarioACards:
    """
    Scenario A: Ozempic (GLP-1) — denial risk + PA bundle.
    Patient: Derrick Lin (erXuFYUfucBZaryVksYEcMg3)
    Expected: info or warning card with approval probability.
    """

    def test_returns_at_least_one_card(self, client):
        body = _post_hook(client, "semaglutide 0.5 MG/DOSE subcutaneous injection", "2200750")
        assert len(body["cards"]) >= 1

    def test_card_has_valid_indicator(self, client):
        body = _post_hook(client, "semaglutide 0.5 MG/DOSE subcutaneous injection", "2200750")
        assert body["cards"][0]["indicator"] in ("info", "warning", "critical")

    def test_card_summary_mentions_approval_probability(self, client):
        """GLP-1 denial card always includes approval probability."""
        body = _post_hook(client, "semaglutide 0.5 MG/DOSE subcutaneous injection", "2200750")
        summary = body["cards"][0]["summary"]
        # Score is shown as "87%" or similar
        assert "%" in summary

    def test_card_detail_has_denial_risk_section(self, client):
        body = _post_hook(client, "semaglutide 0.5 MG/DOSE subcutaneous injection", "2200750")
        detail = body["cards"][0].get("detail", "")
        assert "Denial Risk" in detail or "Approval" in detail or "PA" in detail

    def test_card_has_source_label(self, client):
        body = _post_hook(client, "semaglutide 0.5 MG/DOSE subcutaneous injection", "2200750")
        assert "label" in body["cards"][0]["source"]


class TestScenarioBCards:
    """
    Scenario B: Clopidogrel (PGx) — CPIC safety alert.
    Patient: Derrick Lin (CYP2C19 *2/*2 poor metabolizer via synthetic overlay)
    Expected: critical card warning about clopidogrel ineffectiveness.
    """

    def test_returns_critical_card(self, client):
        body = _post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        assert body["cards"][0]["indicator"] == "critical"

    def test_card_mentions_clopidogrel(self, client):
        body = _post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        summary = body["cards"][0]["summary"].lower()
        assert "clopidogrel" in summary

    def test_card_suggests_alternative(self, client):
        """PGx alert must include at least one suggestion (alternative drug)."""
        body = _post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        assert len(body["cards"][0].get("suggestions", [])) >= 1

    def test_no_narrative_injection_on_pgx_card(self, client):
        """
        PGx cards must NOT have LLM narrative injection.
        Safety rule: every word on a PGx alert is deterministic template text.
        """
        body = _post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        detail = body["cards"][0].get("detail", "")
        # "Clinical Summary" header appears only when narrative is injected
        assert "### Clinical Summary" not in detail


class TestScenarioCCards:
    """
    Scenario C: Keytruda (Oncology) — NCCN pathway validation.
    Patient: Alex Garcia (eAB3mDIBBcyUKviyzrxsnAw3)
    Expected: info card (pathway approved) or warning card (gaps) with PA bundle.
    """

    PATIENT_ID = "eAB3mDIBBcyUKviyzrxsnAw3"

    def test_returns_at_least_one_card(self, client):
        body = _post_hook(
            client,
            "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
            "1547545",
            patient_id=self.PATIENT_ID,
        )
        assert len(body["cards"]) >= 1

    def test_card_has_valid_indicator(self, client):
        body = _post_hook(
            client,
            "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
            "1547545",
            patient_id=self.PATIENT_ID,
        )
        assert body["cards"][0]["indicator"] in ("info", "warning", "critical")

    def test_card_mentions_pathway_or_oncology(self, client):
        """Oncology card detail should reference pathway or oncology context."""
        body = _post_hook(
            client,
            "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
            "1547545",
            patient_id=self.PATIENT_ID,
        )
        detail = body["cards"][0].get("detail", "").lower()
        summary = body["cards"][0]["summary"].lower()
        assert any(kw in detail + summary for kw in ("nccn", "pathway", "oncology", "keytruda", "pembrolizumab"))

    def test_card_has_links(self, client):
        """Oncology card includes at least one link (PA bundle or analysis link)."""
        body = _post_hook(
            client,
            "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
            "1547545",
            patient_id=self.PATIENT_ID,
        )
        assert len(body["cards"][0].get("links", [])) >= 1


class TestScenarioDCards:
    """
    Scenario D: MRI Lumbar Spine (Standard) — denial prevention.
    Patient: Alex Garcia (eAB3mDIBBcyUKviyzrxsnAw3)
    Synthetic overlay: 2 past Aetna denials + 2 missing documents.
    Expected: critical card (25% approval) + appeal draft card.
    """

    PATIENT_ID = "eAB3mDIBBcyUKviyzrxsnAw3"

    def test_returns_at_least_one_card(self, client):
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        assert len(body["cards"]) >= 1

    def test_primary_card_is_critical(self, client):
        """
        SCENARIO_D: 0/40 past denials + 0/35 docs + 25/25 coverage = 25%
        → critical indicator (approval_probability < 50).
        """
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        assert body["cards"][0]["indicator"] == "critical"

    def test_appeal_card_generated(self, client):
        """
        Score 25% triggers appeal generation → second card with 'Appeal Draft' summary.
        """
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        assert len(body["cards"]) >= 2
        appeal_card = body["cards"][1]
        assert "appeal" in appeal_card["summary"].lower() or "draft" in appeal_card["summary"].lower()

    def test_appeal_card_has_view_link(self, client):
        """Appeal card includes a 'View Appeal Draft' link to retrieve the letter."""
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        assert len(body["cards"]) >= 2
        links = body["cards"][1].get("links", [])
        assert len(links) >= 1
        link_labels = [l["label"] for l in links]
        assert any("appeal" in label.lower() or "draft" in label.lower() for label in link_labels)

    def test_appeal_link_is_retrievable(self, client):
        """
        The appeal letter URL from the card link returns the letter content as plain text.
        Validates the full round-trip: generate → store → retrieve.
        """
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        assert len(body["cards"]) >= 2
        links = body["cards"][1].get("links", [])
        assert links, "Appeal card has no links"

        # Retrieve the appeal letter — URL is http://localhost:8000/cds-services/appeals/{id}
        appeal_url = links[0]["url"]
        # Strip host — TestClient handles routing by path only
        path = "/" + "/".join(appeal_url.split("/")[3:])  # /cds-services/appeals/{id}
        response = client.get(path)
        assert response.status_code == 200
        content = response.text
        assert len(content) > 50    # substantive letter content

    def test_primary_card_detail_mentions_denial_risk(self, client):
        """Standard chain card detail surfaces denial risk evidence."""
        body = _post_hook(client, "MRI Lumbar Spine", "72148", patient_id=self.PATIENT_ID)
        detail = body["cards"][0].get("detail", "").lower()
        assert any(kw in detail for kw in ("denial", "approval", "risk", "documentation"))


# ---------------------------------------------------------------------------
# Narrative fallback — template used when OpenAI is unavailable
# ---------------------------------------------------------------------------

class TestNarrativeFallback:
    """
    Verifies that the orchestrator uses template narratives when OpenAI
    is unavailable. In the test environment, the OpenAI API key is either
    absent or set to 'placeholder', so all narrative generation uses the
    template path.

    C# analogy: tests that verify the fallback branch of a retry policy.
    """

    @pytest.mark.asyncio
    async def test_narrative_source_is_valid(self):
        """narrative_source must always be 'openai' or 'template' — never None or unknown."""
        from app.models.cds_hooks import HookRequest
        request = HookRequest(**{
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": "erXuFYUfucBZaryVksYEcMg3",
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [{
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [{"system": "rxnorm", "code": "2200750", "display": "semaglutide"}],
                                "text": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                            },
                        }
                    }],
                },
            },
        })
        result = await Orchestrator().process(request)
        assert result.narrative_source in ("openai", "template")

    @pytest.mark.asyncio
    async def test_narrative_is_non_empty_string(self):
        """narrative field is always a non-empty string — never None."""
        from app.models.cds_hooks import HookRequest
        request = HookRequest(**{
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": "erXuFYUfucBZaryVksYEcMg3",
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [{
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [{"system": "rxnorm", "code": "2200750", "display": "semaglutide"}],
                                "text": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                            },
                        }
                    }],
                },
            },
        })
        result = await Orchestrator().process(request)
        assert isinstance(result.narrative, str)
        assert len(result.narrative) > 0


# ---------------------------------------------------------------------------
# Phase 6 FHIR integration — data source tagging, gap-fill, graceful degradation
# ---------------------------------------------------------------------------

class TestFhirIntegration:
    """
    Phase 6 tests verifying the FHIR-first data layer in the orchestrator.

    Three contracts under test:
      1. When Epic returns real data, AgentResult.fhir_fetched is True and
         data_sources tracks which steps used FHIR vs synthetic.
      2. When Epic returns an empty bundle (patient not found), the synthetic
         overlay fills all gaps so cards are still produced.
      3. When Epic is completely unreachable (auth/network failure), the
         orchestrator degrades gracefully — cards produced, no exception raised.

    All tests mock EpicFHIRClient so no real network calls are made.

    C# analogy: unit tests with Moq replacing IFhirClientService,
    asserting that the result DTO reflects the injected stub data.
    """

    @staticmethod
    def _glp1_request(patient_id: str = "erXuFYUfucBZaryVksYEcMg3"):
        """Build a minimal GLP-1 HookRequest for the orchestrator."""
        from app.models.cds_hooks import HookRequest
        return HookRequest(**{
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": patient_id,
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [{
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [{
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "2200750",
                                    "display": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                                }],
                                "text": "semaglutide 0.5 MG/DOSE subcutaneous injection",
                            },
                        }
                    }],
                },
            },
        })

    @staticmethod
    def _oncology_request(patient_id: str = "eAB3mDIBBcyUKviyzrxsnAw3"):
        """Build a minimal oncology HookRequest (pembrolizumab) for the orchestrator."""
        from app.models.cds_hooks import HookRequest
        return HookRequest(**{
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": patient_id,
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [{
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "medicationCodeableConcept": {
                                "coding": [{
                                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                    "code": "1547545",
                                    "display": "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
                                }],
                                "text": "Keytruda (pembrolizumab) 200mg IV every 3 weeks",
                            },
                        }
                    }],
                },
            },
        })

    @pytest.mark.asyncio
    async def test_fhir_data_tagged_as_fhir_source(self):
        """
        When EpicFHIRClient returns a real FHIR bundle containing an ICD-10 C34.x
        lung cancer condition, AgentResult.fhir_fetched is True and the
        fetch_condition step is tagged 'fhir' in data_sources.

        The oncology chain is used because it has FHIR-first steps that set
        data_source = 'fhir' when real data is found (fetch_condition, fetch_biomarkers,
        fetch_prior_regimens). The GLP-1 chain delegates lab/coverage to run_bridge()
        and does not populate data_sources.

        C# analogy: assert that DTO.FhirFetched == true and DTO.DataSources
        contains at least one entry with value "fhir".
        """
        from unittest.mock import AsyncMock, patch
        from app.models.fhir_bundle import FhirDataBundle

        # Bundle with NSCLC condition (ICD-10 C34.11 — right upper lobe NSCLC)
        # _extract_tumor_from_fhir() looks for ICD-10 codes starting with C34 or C33
        fhir_bundle = FhirDataBundle(
            patient={
                "resourceType": "Patient",
                "id": "eAB3mDIBBcyUKviyzrxsnAw3",
                "name": [{"use": "official", "family": "Garcia", "given": ["Alex"]}],
            },
            medications=[],
            lab_observations=[],
            conditions=[
                {
                    "resourceType": "Condition",
                    "id": "cond-1",
                    "code": {
                        "coding": [{
                            "system": "http://hl7.org/fhir/sid/icd-10-cm",
                            "code": "C34.11",
                            "display": "Malignant neoplasm of upper lobe, right bronchus or lung",
                        }]
                    },
                    "clinicalStatus": {
                        "coding": [{"code": "active"}]
                    },
                }
            ],
            coverage=[],
            fetch_errors=[],
            fetched_from="epic_fhir",
        )

        # Patch EpicFHIRClient as imported inside the orchestrator module
        # C# analogy: Moq.Setup(x => x.FetchPatientBundleAsync(...)).ReturnsAsync(fhirBundle)
        with patch(
            "app.agents.orchestrator.EpicFHIRClient",
            autospec=True,
        ) as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.fetch_patient_bundle = AsyncMock(return_value=fhir_bundle)

            result = await Orchestrator().process(self._oncology_request())

        assert result.fhir_fetched is True, "fhir_fetched must be True when Epic returned data"
        assert isinstance(result.data_sources, dict), "data_sources must be a dict"
        # fetch_condition should be tagged 'fhir' because the bundle has an ICD-10 C34.11 condition
        fhir_tagged = [k for k, v in result.data_sources.items() if v == "fhir"]
        assert len(fhir_tagged) >= 1, (
            f"Expected at least one 'fhir' step in data_sources, got: {result.data_sources}"
        )
        assert "fetch_condition" in fhir_tagged, (
            f"fetch_condition must be tagged 'fhir' when ICD-10 C34.x is in FHIR Conditions"
        )

    @pytest.mark.asyncio
    async def test_synthetic_fills_fhir_gaps(self):
        """
        When Epic returns an empty bundle (patient not found in sandbox),
        the synthetic overlay must fill all gaps so the pipeline produces cards.

        This is the 'graceful degradation to synthetic' contract:
        FHIR absence never blocks card generation.

        C# analogy: assert that result.Cards.Count >= 1 even when
        mock IFhirClientService returns an empty DTO.
        """
        from unittest.mock import AsyncMock, patch
        from app.models.fhir_bundle import FhirDataBundle

        # Empty bundle — simulates 'patient not in Epic sandbox' (404 path)
        empty_bundle = FhirDataBundle(
            patient=None,
            medications=[],
            lab_observations=[],
            conditions=[],
            coverage=[],
            fetch_errors=[],
            fetched_from="prefetch_only",
        )

        with patch(
            "app.agents.orchestrator.EpicFHIRClient",
            autospec=True,
        ) as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.fetch_patient_bundle = AsyncMock(return_value=empty_bundle)

            result = await Orchestrator().process(self._glp1_request())

        # Synthetic overlay must have filled all gaps
        assert result.fhir_fetched is False,    "No real FHIR data → fhir_fetched must be False"
        assert len(result.cards) >= 1,          "Synthetic overlay must produce cards even without FHIR"
        assert result.denial_risk is not None,  "Synthetic data must produce a denial risk score"

        # All data_sources (if any) should be 'synthetic' — no FHIR data was available
        fhir_tagged = [k for k, v in result.data_sources.items() if v == "fhir"]
        assert fhir_tagged == [],  (
            f"Empty FHIR bundle → no steps should be tagged 'fhir', got: {result.data_sources}"
        )

    @pytest.mark.asyncio
    async def test_cards_produced_when_fhir_fails(self):
        """
        When EpicFHIRClient raises (network down, bad credentials, timeout),
        the orchestrator must NOT raise — it must return cards using the
        synthetic overlay.

        This is the Phase 6 'Epic outage' contract: the CDS hook always
        responds, even if Epic is completely unreachable.

        C# analogy: assert no exception propagates when
        IFhirClientService.FetchPatientBundleAsync() throws IOException.
        """
        from unittest.mock import AsyncMock, patch

        # Simulate a complete Epic outage at the FHIR client level
        with patch(
            "app.agents.orchestrator.EpicFHIRClient",
            autospec=True,
        ) as mock_client_cls:
            mock_instance = mock_client_cls.return_value
            mock_instance.fetch_patient_bundle = AsyncMock(
                side_effect=ConnectionError("Epic FHIR API unreachable")
            )

            # Must not raise — graceful degradation is the contract
            result = await Orchestrator().process(self._glp1_request())

        assert result.fhir_fetched is False,    "FHIR failure → fhir_fetched must be False"
        assert len(result.cards) >= 1,          "Epic outage must not prevent card generation"
        assert len(result.evidence_chain_log) >= 1, "Evidence chain must still run on synthetic data"
