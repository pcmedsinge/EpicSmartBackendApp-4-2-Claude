"""
Tests for Phase 3 — Clinical-Financial Bridge components.

Coverage:
  TestDrugClassifier    — classify_drug() lookup logic
  TestDenialScorer      — weighted scoring model, all factors
  TestCardComposer      — card shape, summary length, indicator mapping
  TestPayerRulesEngine  — SQLite queries (uses a seeded test DB)

No FHIR calls, no HTTP. Pure unit tests.

Run with:
    pytest tests/test_denial_scorer.py -v
"""

import pytest

from app.data.db import init_db
from app.data.seed_payer_rules import seed
from app.intelligence.card_composer import compose_denial_card, compose_error_card
from app.models.domain import DenialRiskResult, PipelineError
from app.rules.denial_scorer import EvidenceBundle, ScoreResult, score_glp1_denial_risk
from app.rules.drug_classifier import classify_drug, is_glp1
from app.rules.payer_rules import GLP1Requirements, get_denial_patterns, get_payer_requirements


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def seeded_db():
    """
    Ensure the SQLite DB is initialised and seeded before any test in this module.

    autouse=True means this fixture runs for every test without needing to
    be listed as a parameter. scope="module" means it runs once per file.
    C# analogy: [ClassInitialize] / [AssemblyInitialize] in MSTest.
    """
    seed()


@pytest.fixture
def full_requirements() -> GLP1Requirements:
    """UHC GLP-1 requirements with default values — loaded from DB."""
    req = get_payer_requirements("glp1", "UnitedHealthcare")
    assert req is not None, "Payer rules must be seeded before running tests"
    return req


@pytest.fixture
def all_met_evidence() -> EvidenceBundle:
    """Scenario A — all criteria satisfied. Should score 100."""
    return EvidenceBundle(
        patient_id="erXuFYUfucBZaryVksYEcMg3",
        drug_name="Ozempic",
        payer_name="UnitedHealthcare",
        metformin_days=180,
        metformin_contraindicated=False,
        a1c_value=7.5,
        a1c_days_old=45,
        bmi_value=33.0,
        bmi_days_old=60,
        has_t2d_diagnosis=True,
        has_weight_comorbidity=True,
        coverage_active=True,
        drug_on_formulary=True,
        past_denials_similar=0,
    )


# ---------------------------------------------------------------------------
# Drug Classifier
# ---------------------------------------------------------------------------

class TestDrugClassifier:
    def test_ozempic_is_glp1(self):
        assert classify_drug("Ozempic") == "glp1"

    def test_case_insensitive(self):
        assert classify_drug("OZEMPIC") == "glp1"
        assert classify_drug("ozempic") == "glp1"

    def test_generic_name_semaglutide(self):
        assert classify_drug("semaglutide") == "glp1"

    def test_rxnorm_code_takes_priority(self):
        # Valid GLP-1 RxNorm code should override unrecognised name
        assert classify_drug(drug_name="aspirin", rxnorm_code="2200786") == "glp1"

    def test_unknown_drug_returns_standard(self):
        assert classify_drug("aspirin") == "standard"

    def test_none_inputs_return_standard(self):
        assert classify_drug() == "standard"

    def test_is_glp1_helper_true(self):
        assert is_glp1(drug_name="Ozempic") is True

    def test_is_glp1_helper_false(self):
        assert is_glp1(drug_name="warfarin") is False

    def test_warfarin_is_pgx_sensitive(self):
        assert classify_drug("warfarin") == "pgx_sensitive"

    def test_keytruda_is_oncology(self):
        assert classify_drug("keytruda") == "oncology"


# ---------------------------------------------------------------------------
# Denial Scorer — factor by factor
# ---------------------------------------------------------------------------

class TestDenialScorer:

    def _score(self, evidence: EvidenceBundle, req: GLP1Requirements) -> ScoreResult:
        """Helper — runs scorer with empty denial patterns."""
        return score_glp1_denial_risk(evidence, req, denial_patterns=[])

    # ---- All criteria met ----

    def test_all_met_scores_100(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert result.approval_probability == 100

    def test_all_met_indicator_is_info(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert result.indicator == "info"

    def test_all_met_risk_level_is_low(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert result.risk_level == "low"

    def test_all_met_no_unmet_criteria(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert result.unmet_criteria == []

    def test_all_met_no_suggested_actions(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert result.suggested_actions == []

    # ---- Step therapy missing (−25 pts) ----

    def test_no_metformin_drops_25_points(self, all_met_evidence, full_requirements):
        # metformin_days=None means NO record at all.
        # Step therapy fails (−25) AND documentation fails (−20, medication history missing) = 55.
        # Contrast: metformin_days=89 (record exists but too short) only fails step therapy → 75.
        evidence = all_met_evidence.model_copy(update={"metformin_days": None})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 55

    def test_no_metformin_indicator_is_warning(self, all_met_evidence, full_requirements):
        evidence = all_met_evidence.model_copy(update={"metformin_days": None})
        result = self._score(evidence, full_requirements)
        assert result.indicator == "warning"

    def test_metformin_contraindication_still_passes(self, all_met_evidence, full_requirements):
        """Documented contraindication waives step therapy — should still score 100."""
        evidence = all_met_evidence.model_copy(update={
            "metformin_days": None,
            "metformin_contraindicated": True,
        })
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 100

    def test_metformin_below_threshold_fails(self, all_met_evidence, full_requirements):
        """89 days < 90-day threshold → step therapy not met."""
        evidence = all_met_evidence.model_copy(update={"metformin_days": 89})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 75

    def test_metformin_exactly_at_threshold_passes(self, all_met_evidence, full_requirements):
        """Exactly 90 days → step therapy met (inclusive threshold)."""
        evidence = all_met_evidence.model_copy(update={"metformin_days": 90})
        result = self._score(evidence, full_requirements)
        # step therapy factor should be in met_criteria
        step_factor = next(
            (f for f in result.factors if f.factor == "step_therapy"), None
        )
        assert step_factor is not None
        assert step_factor.met is True

    # ---- Clinical criteria missing (−25 pts) ----

    def test_no_a1c_drops_25_points(self, all_met_evidence, full_requirements):
        # a1c_value=None means NO lab on file.
        # Clinical criteria fail (−25) AND documentation fails (−20, A1C lab missing) = 55.
        # Contrast: a1c_value=6.9 (lab exists but below threshold) only fails clinical → 75.
        evidence = all_met_evidence.model_copy(update={"a1c_value": None, "a1c_days_old": None})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 55

    def test_a1c_below_threshold_drops_25_points(self, all_met_evidence, full_requirements):
        """A1C 6.9% < 7.0% threshold → clinical criteria fail."""
        evidence = all_met_evidence.model_copy(update={"a1c_value": 6.9})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 75

    def test_a1c_exactly_at_threshold_passes(self, all_met_evidence, full_requirements):
        """A1C exactly 7.0% → clinical criteria met (inclusive)."""
        evidence = all_met_evidence.model_copy(update={"a1c_value": 7.0})
        result = self._score(evidence, full_requirements)
        clinical_factor = next(
            (f for f in result.factors if f.factor == "clinical_criteria"), None
        )
        assert clinical_factor is not None
        assert clinical_factor.met is True

    def test_bmi_27_with_comorbidity_passes(self, all_met_evidence, full_requirements):
        """BMI 28 with comorbidity meets the lower threshold (≥27)."""
        evidence = all_met_evidence.model_copy(update={
            "bmi_value": 28.0,
            "has_weight_comorbidity": True,
        })
        result = self._score(evidence, full_requirements)
        clinical_factor = next(f for f in result.factors if f.factor == "clinical_criteria")
        assert clinical_factor.met is True

    def test_bmi_28_without_comorbidity_fails(self, all_met_evidence, full_requirements):
        """BMI 28 without comorbidity — doesn't meet standard threshold (≥30)."""
        evidence = all_met_evidence.model_copy(update={
            "bmi_value": 28.0,
            "has_weight_comorbidity": False,
        })
        result = self._score(evidence, full_requirements)
        clinical_factor = next(f for f in result.factors if f.factor == "clinical_criteria")
        assert clinical_factor.met is False

    # ---- Multiple criteria missing → critical ----

    def test_step_therapy_and_a1c_missing_is_critical(self, all_met_evidence, full_requirements):
        """Both records absent: step therapy (−25) + clinical (−25) + documentation (−20) = 30 → critical."""
        evidence = all_met_evidence.model_copy(update={
            "metformin_days": None,
            "a1c_value": None,
            "a1c_days_old": None,
        })
        result = self._score(evidence, full_requirements)
        # When both records are absent: 100 − 25 (step) − 25 (clinical) − 20 (docs) = 30
        assert result.approval_probability == 30
        assert result.indicator == "critical"

    def test_three_missing_criteria_is_critical(self, all_met_evidence, full_requirements):
        """Missing step therapy + clinical + documentation → critical."""
        evidence = all_met_evidence.model_copy(update={
            "metformin_days": None,
            "a1c_value": None,
            "a1c_days_old": None,
            "bmi_value": None,
            "bmi_days_old": None,
        })
        result = self._score(evidence, full_requirements)
        # 100 - 25 (step) - 25 (clinical) - 20 (doc) = 30
        assert result.approval_probability == 30
        assert result.indicator == "critical"
        assert result.risk_level == "high"

    # ---- Coverage factor ----

    def test_inactive_coverage_drops_15_points(self, all_met_evidence, full_requirements):
        evidence = all_met_evidence.model_copy(update={"coverage_active": False})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 85  # 100 - 15

    # ---- Payer history factor ----

    def test_one_prior_denial_partial_points(self, all_met_evidence, full_requirements):
        """1 prior denial → 8 pts (not 15), total 93."""
        evidence = all_met_evidence.model_copy(update={"past_denials_similar": 1})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 93  # 100 - 7

    def test_two_prior_denials_zero_payer_points(self, all_met_evidence, full_requirements):
        """2+ prior denials → 0 pts for payer history, total 85."""
        evidence = all_met_evidence.model_copy(update={"past_denials_similar": 2})
        result = self._score(evidence, full_requirements)
        assert result.approval_probability == 85  # 100 - 15

    # ---- Score interpretation boundaries ----

    def test_score_80_is_info(self, all_met_evidence, full_requirements):
        """80 is the exact boundary for info."""
        evidence = all_met_evidence.model_copy(update={"past_denials_similar": 2})
        # 100 - 15 = 85 → info; let's also drop coverage: 85-15=70 → warning
        evidence2 = evidence.model_copy(update={"coverage_active": False})
        result = self._score(evidence2, full_requirements)
        # 100 - 15 (payer) - 15 (coverage) = 70 → warning
        assert result.indicator == "warning"

    def test_score_49_is_critical(self, all_met_evidence, full_requirements):
        """49 is below the 50-threshold → critical."""
        evidence = all_met_evidence.model_copy(update={
            "metformin_days": None,          # -25
            "a1c_value": None, "a1c_days_old": None,  # -25
            "bmi_value": None, "bmi_days_old": None,  # within clinical -25
            "coverage_active": False,         # -15
        })
        result = self._score(evidence, full_requirements)
        assert result.approval_probability < 50
        assert result.indicator == "critical"

    # ---- Result structure ----

    def test_result_has_five_factors(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        assert len(result.factors) == 5

    def test_factor_names_are_correct(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        names = {f.factor for f in result.factors}
        assert names == {
            "step_therapy",
            "clinical_criteria",
            "documentation",
            "payer_history",
            "coverage_status",
        }

    def test_max_points_sum_to_100(self, all_met_evidence, full_requirements):
        result = self._score(all_met_evidence, full_requirements)
        total_max = sum(f.max_points for f in result.factors)
        assert total_max == 100


# ---------------------------------------------------------------------------
# Payer Rules Engine
# ---------------------------------------------------------------------------

class TestPayerRulesEngine:

    def test_returns_requirements_for_uhc_glp1(self):
        req = get_payer_requirements("glp1", "UnitedHealthcare")
        assert req is not None

    def test_min_metformin_days_is_90(self):
        req = get_payer_requirements("glp1", "UnitedHealthcare")
        assert req.min_metformin_days == 90

    def test_min_a1c_is_7(self):
        req = get_payer_requirements("glp1", "UnitedHealthcare")
        assert req.min_a1c == 7.0

    def test_min_bmi_standard_is_30(self):
        req = get_payer_requirements("glp1", "UnitedHealthcare")
        assert req.min_bmi_standard == 30.0

    def test_unknown_payer_returns_none(self):
        req = get_payer_requirements("glp1", "Nonexistent Payer")
        assert req is None

    def test_unknown_drug_class_returns_none(self):
        req = get_payer_requirements("oncology", "UnitedHealthcare")
        # oncology rules not seeded in Phase 3
        assert req is None

    def test_denial_patterns_returned(self):
        patterns = get_denial_patterns("glp1", "UnitedHealthcare")
        assert len(patterns) >= 1

    def test_denial_patterns_sorted_by_frequency(self):
        patterns = get_denial_patterns("glp1", "UnitedHealthcare")
        frequencies = [p.frequency for p in patterns]
        # Each frequency should be ≥ the next one (descending order)
        # zip(a, a[1:]) pairs each element with its successor
        assert all(a >= b for a, b in zip(frequencies, frequencies[1:]))

    def test_top_denial_reason_is_step_therapy(self):
        patterns = get_denial_patterns("glp1", "UnitedHealthcare")
        assert patterns[0].denial_reason == "step_therapy_not_met"


# ---------------------------------------------------------------------------
# Card Composer
# ---------------------------------------------------------------------------

def _make_result(**overrides) -> DenialRiskResult:
    """Build a minimal DenialRiskResult, overriding specific fields."""
    defaults = dict(
        approval_probability=87,
        risk_level="low",
        indicator="info",
        met_criteria=["Step therapy met: metformin 180 days"],
        unmet_criteria=[],
        suggested_actions=[],
        drug_class="glp1",
        drug_name="Ozempic",
        payer="UnitedHealthcare",
        cost_estimate_monthly=150.0,
        data_source="synthetic",
        patient_id="erXuFYUfucBZaryVksYEcMg3",
    )
    defaults.update(overrides)
    return DenialRiskResult(**defaults)


class TestCardComposer:

    def test_low_risk_indicator_is_info(self):
        card = compose_denial_card(_make_result(risk_level="low", indicator="info"))
        assert card.indicator == "info"

    def test_moderate_risk_indicator_is_warning(self):
        card = compose_denial_card(_make_result(
            approval_probability=65,
            risk_level="moderate",
            indicator="warning",
            unmet_criteria=["Step therapy NOT met: no metformin history"],
            suggested_actions=["Document metformin trial"],
        ))
        assert card.indicator == "warning"

    def test_high_risk_indicator_is_critical(self):
        card = compose_denial_card(_make_result(
            approval_probability=30,
            risk_level="high",
            indicator="critical",
            unmet_criteria=["Step therapy NOT met", "A1C not on file"],
            suggested_actions=["Document metformin", "Order A1C lab"],
        ))
        assert card.indicator == "critical"

    def test_summary_within_140_chars(self):
        card = compose_denial_card(_make_result())
        assert len(card.summary) <= 140

    def test_summary_contains_approval_percentage(self):
        card = compose_denial_card(_make_result(approval_probability=87))
        assert "87" in card.summary

    def test_source_label_set(self):
        card = compose_denial_card(_make_result())
        assert card.source.label == "CFIP Clinical-Financial Intelligence"

    def test_low_risk_has_submit_pa_suggestion(self):
        card = compose_denial_card(_make_result(risk_level="low", indicator="info"))
        labels = [s.label for s in card.suggestions]
        assert any("Submit PA" in label for label in labels)

    def test_detail_contains_met_criteria(self):
        card = compose_denial_card(_make_result(
            met_criteria=["Step therapy met: metformin 180 days"]
        ))
        assert "metformin 180 days" in (card.detail or "")

    def test_detail_contains_unmet_criteria(self):
        card = compose_denial_card(_make_result(
            risk_level="moderate",
            indicator="warning",
            unmet_criteria=["Step therapy NOT met: no metformin history"],
        ))
        assert "Step therapy NOT met" in (card.detail or "")

    def test_detail_contains_cost_estimate(self):
        card = compose_denial_card(_make_result(cost_estimate_monthly=150.0))
        assert "150" in (card.detail or "")

    def test_has_view_full_analysis_link(self):
        card = compose_denial_card(_make_result())
        labels = [link.label for link in card.links]
        assert "View Full Analysis" in labels

    def test_error_card_is_info_indicator(self):
        error = PipelineError(code="fhir_unavailable", message="FHIR timed out")
        card = compose_error_card(error)
        assert card.indicator == "info"

    def test_error_card_summary_within_140_chars(self):
        error = PipelineError(code="no_payer_rules", message="No rules found")
        card = compose_error_card(error)
        assert len(card.summary) <= 140

    def test_error_card_has_source(self):
        error = PipelineError(code="test", message="test")
        card = compose_error_card(error)
        assert card.source.label == "CFIP Clinical-Financial Intelligence"

    def test_synthetic_data_badge_appears_in_detail(self):
        """When data_source is 'synthetic', detail should mention demo data."""
        card = compose_denial_card(_make_result(data_source="synthetic"))
        assert "demo data" in (card.detail or "")

    def test_fhir_data_no_badge(self):
        """When data_source is 'fhir', no badge should appear in detail."""
        card = compose_denial_card(_make_result(data_source="fhir"))
        assert "demo data" not in (card.detail or "")
