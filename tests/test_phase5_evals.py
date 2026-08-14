from evals.run_phase5_evals import run


def test_phase5_eval_smoke():
    result = run()
    assert result["scope_leakage_rate"] == 0
    assert result["retrieval_precision_smoke"] == 1
    assert result["token_budget_pass"] is True
    assert result["mode"] == "sqlite_offline_smoke"
