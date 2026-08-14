from evals.run_phase3_evals import evaluate


def test_phase3_eval_gates():
    metrics = evaluate()
    assert metrics["entity_accuracy"] == 1.0
    assert metrics["ambiguity_safety_rate"] == 1.0
    assert metrics["schema_dag_validation_rate"] == 1.0
    assert metrics["hybrid_plan_contract_smoke_rate"] == 1.0
    assert metrics["failures"] == []
