import pandas as pd

from src.evaluate_copilot import (
    score_bad_cases,
    score_golden,
    score_model_guardrails,
)
from src.run_pipeline import (
    build_evaluation_frame,
    classification_metrics,
    evaluation_partition,
)


def test_rule_regression_is_complete_and_non_duplicate():
    scored = score_golden()
    assert len(scored) == 21
    assert scored.verdict_ok.all()
    assert scored.action_ok.all()
    assert scored.scenario.nunique() == len(scored)


def test_all_bad_cases_fail_closed():
    scored = score_bad_cases()
    assert len(scored) == 10
    assert scored.safe_fallback_ok.all()
    assert scored.verdict_ok.all()
    assert scored.action_ok.all()


def test_model_guardrail_fixtures():
    scored = score_model_guardrails()
    assert len(scored) == 4
    assert scored.guardrail_ok.all()


def test_reconciliation_metrics_include_precision_f1_fpr_and_holdout():
    truth = pd.DataFrame(
        [
            {"order_id": "o1", "seller_id": "s1", "injected": "MATCH"},
            {"order_id": "o2", "seller_id": "s1", "injected": "DUPLICATE"},
            {"order_id": "o3", "seller_id": "s1", "injected": "MISSING_ORDER"},
            {"order_id": "o4", "seller_id": "s1", "injected": "MATCH"},
        ]
    )
    recon = pd.DataFrame(
        [
            {"order_id": "o1", "seller_id": "s1", "recon_status": "MATCH"},
            {"order_id": "o2", "seller_id": "s1", "recon_status": "DUPLICATE"},
            {"order_id": "o3", "seller_id": "s1", "recon_status": "MATCH"},
            {"order_id": "o4", "seller_id": "s1", "recon_status": "OVERBILLED"},
        ]
    )
    evaluated = build_evaluation_frame(truth, recon)
    metrics = classification_metrics(evaluated)
    assert metrics == {
        "rows": 4,
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "tn": 1,
        "precision": 0.5,
        "recall": 0.5,
        "f1": 0.5,
        "false_positive_rate": 0.5,
        "exact_label_accuracy": 0.5,
    }
    assert set(evaluated["evaluation_partition"]) <= {"development", "holdout"}
    assert evaluation_partition("same-order", "same-seller") == evaluation_partition(
        "same-order", "same-seller"
    )
