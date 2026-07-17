from src.evaluate_copilot import (
    score_bad_cases,
    score_golden,
    score_model_guardrails,
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

