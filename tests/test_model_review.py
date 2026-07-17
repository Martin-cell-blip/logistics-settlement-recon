import pytest
from pydantic import ValidationError

from src.model_review import (
    ModelReview,
    ModelUsage,
    generate_model_review,
)


EVIDENCE = {
    "sor_freight": 100.0,
    "n_items": 1,
    "items": None,
    "order": {
        "order_status": "delivered",
        "order_delivered_customer_date": "2018-08-20 11:00:00",
    },
    "bills": None,
    "billed_total": 130.0,
    "billed_unit": 130.0,
    "n_bill_lines": 1,
}


class FakeProvider:
    name = "fake"
    model = "fake-structured-v1"

    def __init__(self, review):
        self.review = review

    def generate(self, prompt):
        assert "evidence_ledger" in prompt
        return self.review, ModelUsage(input_tokens=120, output_tokens=40)


def test_aligned_structured_output_is_accepted():
    output = ModelReview(
        verdict="CONFIRMED",
        recommended_action="追回差额",
        explanation="账单金额高于系统应计金额，且差异超过已配置的规则容差。",
        evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
        confidence="中",
    )
    result = generate_model_review(
        "OVERBILLED",
        EVIDENCE,
        "CONFIRMED",
        "追回差额",
        "中",
        "规则理由",
        provider=FakeProvider(output),
    )
    assert result.status == "generated"
    assert result.review == output
    assert result.guardrail_reasons == []


def test_conflicting_output_falls_back_to_rule():
    output = ModelReview(
        verdict="PASS",
        recommended_action="无需处理",
        explanation="模型输出与确定性规则发生冲突，因此不应进入审核页面。",
        evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
        confidence="高",
    )
    result = generate_model_review(
        "OVERBILLED",
        EVIDENCE,
        "CONFIRMED",
        "追回差额",
        "中",
        "规则理由保持为唯一有效说明。",
        provider=FakeProvider(output),
    )
    assert result.status == "fallback"
    assert result.review.verdict == "CONFIRMED"
    assert result.review.recommended_action == "追回差额"
    assert result.review.explanation == "规则理由保持为唯一有效说明。"
    assert len(result.guardrail_reasons) == 3


def test_unknown_evidence_id_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        ModelReview(
            verdict="CONFIRMED",
            recommended_action="追回差额",
            explanation="引用了不在证据账本中的字段，因此 Schema 必须拒绝。",
            evidence_ids=["MADE_UP_FIELD"],
            confidence="中",
        )


def test_missing_key_is_an_explicit_disabled_state(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = generate_model_review(
        "OVERBILLED",
        EVIDENCE,
        "CONFIRMED",
        "追回差额",
        "中",
        "规则理由保持可见。",
    )
    assert result.status == "disabled"
    assert result.provider == "none"
    assert result.guardrail_reasons

