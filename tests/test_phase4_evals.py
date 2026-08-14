from evals.run_phase4_evals import evaluate


def test_phase4_offline_smoke_metrics_are_explicit_and_safe():
    result = evaluate()
    assert result["profile"] == "in_memory_test_smoke"
    assert result["recall_at_3"] == 1.0
    assert result["citation_coverage"] == 1.0
    assert result["citation_integrity"] == 1.0
    assert result["numeric_provenance_rate"] == 1.0
    assert result["real_milvus_executed"] is False
