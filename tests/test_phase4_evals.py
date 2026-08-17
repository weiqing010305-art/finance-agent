from evals.run_phase4_evals import evaluate


def test_phase4_offline_smoke_metrics_are_explicit_and_safe():
    result = evaluate()
    assert result["profile"] == "in_memory_test_smoke"
    assert result["real_milvus_executed"] is False


def test_phase4_verifier_quality_properties_hold_on_mixed_inputs():
    """The verifier must catch fabricated/evidence-free claims and disclose
    conflicts — these quality properties are asserted to be perfect."""
    result = evaluate()
    assert result["unsupported_caught_rate"] == 1.0
    assert result["false_support_rate"] == 0.0
    assert result["conflict_disclosure_rate"] == 1.0


def test_phase4_verifier_metrics_are_honest_not_self_graded():
    """On mixed-quality inputs the honest rates must NOT trivially be 1.0:
    a supported_rate of 1.0 would mean the verifier accepted everything,
    which is exactly the self-grading behaviour this eval exists to prevent.
    The citation coverage stays an integrity property: every claim that
    passed the verifier must be cited by the report."""
    result = evaluate()
    assert result["verification_case_count"] >= 4
    assert 0.0 < result["supported_rate"] < 1.0
    assert result["verification_citation_coverage"] == 1.0
