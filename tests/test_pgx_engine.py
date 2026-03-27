"""
Tests for Phase 4 — PGx Safety, PA Builder, Drug Classifier extensions, and routing.

Coverage:
  TestCpicEngine           — check_pgx() all three outcome paths
  TestDrugClassifierPhase4 — pgx_sensitive classification, FHIR display strings, RxNorm codes
  TestPaBundleBuilder      — build_pa_bundle() readiness, evidence items, appeal notes
  TestPgxCardComposer      — compose_pgx_card() critical alert, recommend testing, no card
  TestPhase4Routing        — hook handler routes clopidogrel → critical card, unknown drug → info card

No FHIR calls, no live HTTP. Pure unit tests except TestPhase4Routing which uses TestClient.

Run with:
    pytest tests/test_pgx_engine.py -v
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.data.db import init_db
from app.data.seed_cpic import seed as seed_cpic
from app.data.seed_payer_rules import seed as seed_payer_rules
from app.intelligence.card_composer import compose_pgx_card
from app.main import app
from app.models.cds_hooks import HookRequest
from app.models.domain import DenialRiskResult
from app.rules.cpic_engine import PgxResult, check_pgx
from app.rules.drug_classifier import classify_drug
from app.agents.specialty_pa import build_pa_bundle


# ---------------------------------------------------------------------------
# Module-level DB fixture — seeds tables once before any test in this file.
# autouse=True means every test class gets a seeded DB without declaring it.
# scope="module" means it runs once per file, not once per test.
# C# analogy: [AssemblyInitialize] in MSTest.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    """Ensure both payer rules and CPIC rules are present before tests run."""
    init_db()
    seed_payer_rules()
    seed_cpic()


# ---------------------------------------------------------------------------
# Shared fixture — TestClient (used by TestPhase4Routing only)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Shared helper — build a minimal HookRequest for a given drug
# ---------------------------------------------------------------------------

def _make_hook_request(
    drug_text: str,
    rxnorm_code: str,
    patient_id: str = "erXuFYUfucBZaryVksYEcMg3",
) -> HookRequest:
    """
    Build a HookRequest with a single draftOrder MedicationRequest.

    draftOrders lives in context per the CDS Hooks order-select spec.
    This mirrors the real harness payload shape.
    C# analogy: a test builder method — assembles a valid request DTO.
    """
    return HookRequest(
        hook="order-select",
        hookInstance=str(uuid.uuid4()),
        context={
            "patientId": patient_id,
            "userId": "Practitioner/demo",
            "selections": ["MedicationRequest/demo-order"],
            "draftOrders": {
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "MedicationRequest",
                            "id": "demo-order",
                            "status": "draft",
                            "intent": "proposal",
                            "subject": {"reference": f"Patient/{patient_id}"},
                            "medicationCodeableConcept": {
                                "coding": [
                                    {
                                        "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                        "code": rxnorm_code,
                                        "display": drug_text,
                                    }
                                ],
                                "text": drug_text,
                            },
                        }
                    }
                ],
            },
        },
        prefetch={
            "patient": {
                "resourceType": "Patient",
                "id": patient_id,
                "name": [{"use": "official", "family": "Lin", "given": ["Derrick"]}],
            }
        },
    )


# ---------------------------------------------------------------------------
# Helper — build a minimal DenialRiskResult for PA builder tests
# ---------------------------------------------------------------------------

def _denial_result(met: list[str], unmet: list[str]) -> DenialRiskResult:
    """
    Build a minimal DenialRiskResult with the given met/unmet criteria lists.
    Used by TestPaBundleBuilder to test the PA builder in isolation.
    """
    score = 100 - (25 * len(unmet))  # rough proxy
    risk = "low" if score >= 80 else "moderate" if score >= 50 else "high"
    indicator = "info" if risk == "low" else "warning" if risk == "moderate" else "critical"
    return DenialRiskResult(
        approval_probability=max(score, 10),
        risk_level=risk,
        indicator=indicator,
        met_criteria=met,
        unmet_criteria=unmet,
        suggested_actions=[f"Fix: {u}" for u in unmet],
        drug_class="glp1",
        drug_name="Ozempic",
        payer="UnitedHealthcare",
        cost_estimate_monthly=150.0,
        data_source="synthetic",
        patient_id="erXuFYUfucBZaryVksYEcMg3",
    )


# ---------------------------------------------------------------------------
# TestCpicEngine
# ---------------------------------------------------------------------------

class TestCpicEngine:
    """
    Tests for check_pgx() — the CPIC pharmacogenomics rule engine.

    Three outcome paths:
      1. Drug not PGx-sensitive → severity "none", no interaction
      2. No genomic data        → recommend testing, pgx_data_available=False
      3. Data available         → interaction result with severity from DB
    """

    # --- Path 1: drug not PGx-sensitive ---

    def test_non_pgx_drug_returns_severity_none(self):
        result = check_pgx(drug="metformin", genomic_data={"CYP2C19": "*2/*2"})
        assert result.severity == "none"

    def test_non_pgx_drug_has_no_interaction(self):
        result = check_pgx(drug="metformin", genomic_data={"CYP2C19": "*2/*2"})
        assert result.has_interaction is False

    def test_non_pgx_drug_returns_drug_name(self):
        result = check_pgx(drug="metformin", genomic_data=None)
        assert result.drug_name == "metformin"

    # --- Path 2: no genomic data ---

    def test_no_genomic_data_pgx_data_available_false(self):
        result = check_pgx(drug="clopidogrel", genomic_data=None)
        assert result.pgx_data_available is False

    def test_no_genomic_data_no_interaction(self):
        # "no data" is not an interaction — it's an unknown state
        result = check_pgx(drug="clopidogrel", genomic_data=None)
        assert result.has_interaction is False

    def test_no_genomic_data_severity_is_low(self):
        # Drug IS pgx-sensitive but we just don't have data — not "none"
        result = check_pgx(drug="clopidogrel", genomic_data=None)
        assert result.severity == "low"

    def test_no_genomic_data_gene_is_set(self):
        # Should still name the relevant gene for the "recommend testing" card
        result = check_pgx(drug="clopidogrel", genomic_data=None)
        assert result.gene == "CYP2C19"

    def test_no_genomic_data_recommendation_mentions_testing(self):
        result = check_pgx(drug="clopidogrel", genomic_data=None)
        assert "genomic" in result.recommendation.lower() or "testing" in result.recommendation.lower()

    # --- Path 3: data available — poor metabolizer ---

    def test_poor_metabolizer_has_interaction(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.has_interaction is True

    def test_poor_metabolizer_severity_is_high(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.severity == "high"

    def test_poor_metabolizer_pgx_data_available(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.pgx_data_available is True

    def test_poor_metabolizer_gene_is_cyp2c19(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.gene == "CYP2C19"

    def test_poor_metabolizer_diplotype_preserved(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.diplotype == "*2/*2"

    def test_poor_metabolizer_recommends_prasugrel(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.alternative_drug is not None
        assert "prasugrel" in result.alternative_drug.lower()

    def test_poor_metabolizer_evidence_level_1a(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        assert result.evidence_level == "1A"

    # --- Intermediate metabolizer ---

    def test_intermediate_metabolizer_severity_is_moderate(self):
        # *1/*2 = intermediate metabolizer per CPIC seed data
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*1/*2"})
        assert result.severity == "moderate"

    def test_intermediate_metabolizer_has_interaction(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*1/*2"})
        assert result.has_interaction is True

    # --- Normal metabolizer ---

    def test_normal_metabolizer_no_interaction(self):
        # *1/*1 = normal metabolizer — clopidogrel works as expected
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*1/*1"})
        assert result.has_interaction is False

    def test_normal_metabolizer_severity_is_low(self):
        result = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*1/*1"})
        assert result.severity == "low"

    # --- Diplotype ordering ---

    def test_reversed_diplotype_matches_same_rule(self):
        # *2/*1 should match the same rule as *1/*2 (intermediate metabolizer)
        result_fwd = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*1/*2"})
        result_rev = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*1"})
        assert result_fwd.severity == result_rev.severity
        assert result_fwd.metabolizer_status == result_rev.metabolizer_status

    # --- Drug name normalization ---

    def test_full_fhir_display_string_normalizes_correctly(self):
        # "clopidogrel 75 MG oral tablet" should resolve to clopidogrel rules
        result = check_pgx(drug="clopidogrel 75 MG oral tablet", genomic_data={"CYP2C19": "*2/*2"})
        assert result.has_interaction is True
        assert result.severity == "high"

    def test_brand_name_plavix_resolves(self):
        # "plavix" is a key in _DRUG_GENE_MAP
        result = check_pgx(drug="plavix", genomic_data={"CYP2C19": "*2/*2"})
        assert result.has_interaction is True

    def test_case_insensitive_drug_name(self):
        result_lower = check_pgx(drug="clopidogrel", genomic_data={"CYP2C19": "*2/*2"})
        result_upper = check_pgx(drug="CLOPIDOGREL", genomic_data={"CYP2C19": "*2/*2"})
        assert result_lower.severity == result_upper.severity

    # --- Warfarin (second drug class, seeded) ---

    def test_warfarin_no_data_recommends_testing(self):
        result = check_pgx(drug="warfarin", genomic_data=None)
        assert result.pgx_data_available is False
        assert result.gene == "CYP2C9"

    def test_warfarin_poor_metabolizer_has_interaction(self):
        # CYP2C9 *3/*3 = poor metabolizer for warfarin
        result = check_pgx(drug="warfarin", genomic_data={"CYP2C9": "*3/*3"})
        assert result.has_interaction is True
        assert result.severity in ("high", "moderate")


# ---------------------------------------------------------------------------
# TestDrugClassifierPhase4
# ---------------------------------------------------------------------------

class TestDrugClassifierPhase4:
    """
    Phase 4 additions to drug classifier coverage:
    pgx_sensitive class, FHIR display string substring matching, clinical RxNorm codes.
    """

    # --- pgx_sensitive by name ---

    def test_clopidogrel_is_pgx_sensitive(self):
        assert classify_drug("clopidogrel") == "pgx_sensitive"

    def test_plavix_is_pgx_sensitive(self):
        assert classify_drug("Plavix") == "pgx_sensitive"

    def test_warfarin_is_pgx_sensitive(self):
        assert classify_drug("warfarin") == "pgx_sensitive"

    def test_coumadin_is_pgx_sensitive(self):
        assert classify_drug("Coumadin") == "pgx_sensitive"

    # --- pgx_sensitive by RxNorm code ---

    def test_rxnorm_309362_clopidogrel_tablet(self):
        # 309362 = clopidogrel 75 MG oral tablet — clinical drug code
        assert classify_drug(rxnorm_code="309362") == "pgx_sensitive"

    def test_rxnorm_32968_clopidogrel_ingredient(self):
        # 32968 = clopidogrel ingredient concept
        assert classify_drug(rxnorm_code="32968") == "pgx_sensitive"

    # --- glp1 by clinical RxNorm code ---

    def test_rxnorm_2200750_semaglutide_injection(self):
        # 2200750 = semaglutide 0.5 MG/DOSE subcutaneous injection (Ozempic)
        assert classify_drug(rxnorm_code="2200750") == "glp1"

    # --- Full FHIR display string substring matching ---

    def test_fhir_display_clopidogrel_classified(self):
        # Exactly as returned by harness / Epic FHIR
        assert classify_drug("clopidogrel (Plavix) 75mg tablet") == "pgx_sensitive"

    def test_fhir_display_ozempic_classified(self):
        assert classify_drug("Ozempic (semaglutide) 0.5mg injection") == "glp1"

    def test_fhir_display_semaglutide_injection_classified(self):
        assert classify_drug("semaglutide 0.5 MG/DOSE subcutaneous injection") == "glp1"

    def test_unknown_drug_returns_standard(self):
        assert classify_drug("ibuprofen 400mg tablet") == "standard"

    def test_rxnorm_priority_over_name(self):
        # Even if the name is unknown, a known RxNorm code should classify it
        assert classify_drug(drug_name="some unknown brand", rxnorm_code="309362") == "pgx_sensitive"


# ---------------------------------------------------------------------------
# TestPaBundleBuilder
# ---------------------------------------------------------------------------

class TestPaBundleBuilder:
    """
    Tests for build_pa_bundle() — assembles a PABundle from a DenialRiskResult.

    Tests use a minimal HookRequest (no real FHIR data needed — PA builder
    only reads patient name from prefetch, which can be empty).
    """

    @pytest.fixture
    def hook_request_with_name(self) -> HookRequest:
        """HookRequest with patient name in prefetch — used by name extraction test."""
        return HookRequest(
            hook="order-select",
            hookInstance=str(uuid.uuid4()),
            context={"patientId": "erXuFYUfucBZaryVksYEcMg3"},
            prefetch={
                "patient": {
                    "resourceType": "Patient",
                    "id": "erXuFYUfucBZaryVksYEcMg3",
                    "name": [{"use": "official", "family": "Lin", "given": ["Derrick"]}],
                }
            },
        )

    @pytest.fixture
    def hook_request_no_prefetch(self) -> HookRequest:
        return HookRequest(
            hook="order-select",
            hookInstance=str(uuid.uuid4()),
            context={"patientId": "erXuFYUfucBZaryVksYEcMg3"},
        )

    # --- Readiness ---

    def test_ready_when_no_unmet_criteria(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met", "A1C met", "BMI met"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert bundle.ready_to_submit is True

    def test_not_ready_when_unmet_criteria_exist(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met"], unmet=["Clinical criteria NOT met: A1C low"])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert bundle.ready_to_submit is False

    # --- Evidence items ---

    def test_evidence_items_count_matches_criteria_total(self, hook_request_no_prefetch):
        # 2 met + 1 unmet = 3 evidence items
        result = _denial_result(
            met=["Step therapy met: metformin 180 days", "Coverage active: confirmed"],
            unmet=["Clinical criteria NOT met: A1C 5.1%"],
        )
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert len(bundle.clinical_evidence) == 3

    def test_met_evidence_items_flagged_true(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met: metformin 180 days"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        # All met_criteria should produce EvidenceItems with met=True
        met_items = [e for e in bundle.clinical_evidence if e.met]
        assert len(met_items) == 1
        assert met_items[0].met is True

    def test_unmet_evidence_items_flagged_false(self, hook_request_no_prefetch):
        result = _denial_result(met=[], unmet=["Clinical criteria NOT met: A1C 5.1%"])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        unmet_items = [e for e in bundle.clinical_evidence if not e.met]
        assert len(unmet_items) == 1
        assert unmet_items[0].met is False

    def test_evidence_item_criterion_stripped_at_colon(self, hook_request_no_prefetch):
        # "Step therapy met: metformin 180 days" → criterion="Step therapy met"
        result = _denial_result(met=["Step therapy met: metformin 180 days"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert bundle.clinical_evidence[0].criterion == "Step therapy met"

    # --- Appeal notes ---

    def test_appeal_notes_none_when_all_met(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met", "A1C met"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert bundle.appeal_notes is None

    def test_appeal_notes_present_when_unmet(self, hook_request_no_prefetch):
        result = _denial_result(met=[], unmet=["Clinical criteria NOT met: A1C 5.1%"])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert bundle.appeal_notes is not None
        assert len(bundle.appeal_notes) > 0

    def test_appeal_notes_contain_drug_name(self, hook_request_no_prefetch):
        result = _denial_result(met=[], unmet=["Clinical criteria NOT met"])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        # result.drug_name = "Ozempic" — should appear in the appeal text
        assert "Ozempic" in bundle.appeal_notes

    # --- Supporting documents ---

    def test_supporting_documents_populated_for_glp1(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        assert len(bundle.supporting_documents) > 0

    def test_supporting_documents_contains_step_therapy_doc_when_unmet(self, hook_request_no_prefetch):
        result = _denial_result(met=[], unmet=["Step therapy NOT met: no metformin history"])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        # The medication history document should be present (either from base list or ACTION REQUIRED note)
        all_docs_text = " ".join(bundle.supporting_documents).lower()
        assert "metformin" in all_docs_text or "medication history" in all_docs_text

    # --- Patient name extraction ---

    def test_patient_name_extracted_from_prefetch(self, hook_request_with_name):
        result = _denial_result(met=["Step therapy met"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_with_name)
        # "Derrick Lin" — given + family
        assert "Derrick" in bundle.patient_name or "Lin" in bundle.patient_name

    def test_patient_name_empty_when_no_prefetch(self, hook_request_no_prefetch):
        result = _denial_result(met=["Step therapy met"], unmet=[])
        bundle = build_pa_bundle(result, hook_request_no_prefetch)
        # No patient in prefetch → empty string, not an error
        assert isinstance(bundle.patient_name, str)


# ---------------------------------------------------------------------------
# TestPgxCardComposer
# ---------------------------------------------------------------------------

class TestPgxCardComposer:
    """
    Tests for compose_pgx_card() — converts PgxResult into a CDS Card or None.

    Three outcomes:
      has_interaction=True   → critical safety alert card
      pgx_data_available=False → warning "recommend testing" card
      has_interaction=False + data ok + severity not "none" → None (no card)
      severity="none"        → None (drug not PGx-sensitive)
    """

    @pytest.fixture
    def poor_metabolizer_result(self) -> PgxResult:
        """CYP2C19 poor metabolizer — highest severity interaction."""
        return PgxResult(
            has_interaction=True,
            gene="CYP2C19",
            metabolizer_status="poor_metabolizer",
            diplotype="*2/*2",
            recommendation=(
                "CYP2C19 poor metabolizer. Clopidogrel is a prodrug that requires CYP2C19 "
                "for activation. This patient cannot convert clopidogrel to its active form — "
                "the drug will be ineffective. Use an alternative antiplatelet agent."
            ),
            alternative_drug="prasugrel, ticagrelor",
            severity="high",
            evidence_level="1A",
            pgx_data_available=True,
            drug_name="clopidogrel",
        )

    @pytest.fixture
    def no_data_result(self) -> PgxResult:
        """No genomic data on file — triggers recommend-testing card."""
        return PgxResult(
            has_interaction=False,
            gene="CYP2C19",
            metabolizer_status=None,
            diplotype=None,
            recommendation=(
                "No CYP2C19 genomic data on file for this patient. "
                "Consider ordering a CYP2C19 PGx panel before initiating clopidogrel therapy."
            ),
            alternative_drug=None,
            severity="low",
            evidence_level=None,
            pgx_data_available=False,
            drug_name="clopidogrel",
        )

    @pytest.fixture
    def normal_metabolizer_result(self) -> PgxResult:
        """Normal metabolizer — no card should be returned."""
        return PgxResult(
            has_interaction=False,
            gene="CYP2C19",
            metabolizer_status="normal_metabolizer",
            diplotype="*1/*1",
            recommendation="CYP2C19 normal metabolizer. Use clopidogrel as prescribed.",
            alternative_drug=None,
            severity="low",
            evidence_level="1A",
            pgx_data_available=True,
            drug_name="clopidogrel",
        )

    @pytest.fixture
    def not_sensitive_result(self) -> PgxResult:
        """Drug not PGx-sensitive — no card should be returned."""
        return PgxResult(
            has_interaction=False,
            gene=None,
            metabolizer_status=None,
            diplotype=None,
            recommendation="No PGx checking required for metformin.",
            alternative_drug=None,
            severity="none",
            evidence_level=None,
            pgx_data_available=False,
            drug_name="metformin",
        )

    # --- Critical alert card ---

    def test_poor_metabolizer_returns_card(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert card is not None

    def test_poor_metabolizer_indicator_is_critical(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert card.indicator == "critical"

    def test_poor_metabolizer_summary_mentions_drug(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert "clopidogrel" in card.summary.lower()

    def test_poor_metabolizer_summary_within_140_chars(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert len(card.summary) <= 140

    def test_poor_metabolizer_suggestions_include_prasugrel(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        labels = [s.label.lower() for s in card.suggestions]
        assert any("prasugrel" in label for label in labels)

    def test_poor_metabolizer_first_suggestion_is_recommended(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert card.suggestions[0].isRecommended is True

    def test_poor_metabolizer_detail_mentions_gene(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert "CYP2C19" in card.detail

    def test_poor_metabolizer_detail_mentions_diplotype(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert "*2/*2" in card.detail

    def test_poor_metabolizer_has_link(self, poor_metabolizer_result):
        card = compose_pgx_card(poor_metabolizer_result)
        assert len(card.links) >= 1

    # --- Recommend-testing card ---

    def test_no_data_returns_card(self, no_data_result):
        card = compose_pgx_card(no_data_result)
        assert card is not None

    def test_no_data_indicator_is_warning(self, no_data_result):
        card = compose_pgx_card(no_data_result)
        assert card.indicator == "warning"

    def test_no_data_summary_within_140_chars(self, no_data_result):
        card = compose_pgx_card(no_data_result)
        assert len(card.summary) <= 140

    def test_no_data_suggests_ordering_pgx_panel(self, no_data_result):
        card = compose_pgx_card(no_data_result)
        labels = [s.label.lower() for s in card.suggestions]
        assert any("pgx" in label or "panel" in label or "cyp2c19" in label.lower() for label in labels)

    # --- No card cases ---

    def test_normal_metabolizer_returns_none(self, normal_metabolizer_result):
        # All clear — no card warranted
        card = compose_pgx_card(normal_metabolizer_result)
        assert card is None

    def test_not_sensitive_drug_returns_none(self, not_sensitive_result):
        # Drug not in PGx scope — no card
        card = compose_pgx_card(not_sensitive_result)
        assert card is None


# ---------------------------------------------------------------------------
# TestPhase4Routing (integration — uses TestClient)
# ---------------------------------------------------------------------------

class TestPhase4Routing:
    """
    Integration tests: hook handler routes by drug class and returns
    the correct card type and indicator.

    Uses TestClient to fire real HTTP requests against the in-process FastAPI app.
    These tests are slower than unit tests but validate the full request → response chain.
    """

    def _post_hook(self, client, drug_text: str, rxnorm_code: str, patient_id: str = "erXuFYUfucBZaryVksYEcMg3"):
        """Helper: POST a hook request and return the parsed JSON body."""
        payload = {
            "hook": "order-select",
            "hookInstance": str(uuid.uuid4()),
            "context": {
                "patientId": patient_id,
                "userId": "Practitioner/demo",
                "selections": ["MedicationRequest/demo-order"],
                "draftOrders": {
                    "resourceType": "Bundle",
                    "entry": [
                        {
                            "resource": {
                                "resourceType": "MedicationRequest",
                                "id": "demo-order",
                                "status": "draft",
                                "intent": "proposal",
                                "subject": {"reference": f"Patient/{patient_id}"},
                                "medicationCodeableConcept": {
                                    "coding": [
                                        {
                                            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                            "code": rxnorm_code,
                                            "display": drug_text,
                                        }
                                    ],
                                    "text": drug_text,
                                },
                            }
                        }
                    ],
                },
            },
            "prefetch": {
                "patient": {
                    "resourceType": "Patient",
                    "id": patient_id,
                    "name": [{"use": "official", "family": "Lin", "given": ["Derrick"]}],
                }
            },
        }
        response = client.post("/cds-services/cfip-order-intelligence", json=payload)
        assert response.status_code == 200
        return response.json()

    # --- Scenario B: clopidogrel → PGx pipeline ---

    def test_clopidogrel_returns_at_least_one_card(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        # Synthetic overlay supplies CYP2C19 *2/*2 for this patient
        assert len(body["cards"]) >= 1

    def test_clopidogrel_card_is_critical(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        # Poor metabolizer (*2/*2) should produce a critical card
        assert body["cards"][0]["indicator"] == "critical"

    def test_clopidogrel_card_mentions_cyp2c19(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        detail = body["cards"][0]["detail"]
        assert "CYP2C19" in detail

    def test_clopidogrel_card_suggests_alternative(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        suggestions = body["cards"][0].get("suggestions", [])
        assert len(suggestions) >= 1

    # --- Unknown drug → generic info card ---

    def test_unknown_drug_returns_card(self, client):
        # Ibuprofen is "standard" class → orchestrator denial-prevention chain
        # Phase 5: returns a denial-risk card (info/warning) rather than Phase 4's generic info card
        body = self._post_hook(client, "ibuprofen 400 MG oral tablet", "310965")
        assert len(body["cards"]) >= 1
        assert body["cards"][0]["indicator"] in ("info", "warning", "critical")

    def test_unknown_drug_card_has_summary(self, client):
        # Phase 5: standard chain produces a denial-risk card with the drug name
        body = self._post_hook(client, "ibuprofen 400 MG oral tablet", "310965")
        summary = body["cards"][0]["summary"].lower()
        assert len(summary) > 0
        # Denial-risk card includes approval probability
        assert "%" in summary or "ibuprofen" in summary

    # --- Spec compliance ---

    def test_all_cards_have_required_fields(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        for card in body["cards"]:
            assert "summary" in card
            assert "indicator" in card
            assert "source" in card

    def test_all_card_summaries_within_140_chars(self, client):
        body = self._post_hook(client, "clopidogrel 75 MG oral tablet", "309362")
        for card in body["cards"]:
            assert len(card["summary"]) <= 140, (
                f"Summary too long ({len(card['summary'])} chars): {card['summary']}"
            )
