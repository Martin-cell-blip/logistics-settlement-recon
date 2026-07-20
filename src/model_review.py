"""Structured, guarded model summaries for settlement-review cases.

The model is deliberately downstream of deterministic reconciliation rules.
It may compress verified evidence into an audit-friendly explanation, but it
cannot change amounts, the rule verdict, the recommended action, or execution
state.
"""
from __future__ import annotations

import json
import os
import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROMPT_VERSION = "settlement-evidence-summary-v3"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

Verdict = Literal["CONFIRMED", "SUSPECT", "PASS"]
Action = Literal[
    "拒付整笔",
    "拒付重复部分",
    "追回差额",
    "人工复核",
    "人工复核/确认成本",
    "催承运商开票/确认应付",
    "无需处理",
]
Confidence = Literal["高", "中", "低"]
EvidenceId = Literal[
    "SOR_AMOUNT",
    "CONTRACT_EXPECTED_AMOUNT",
    "CONTRACT_CLAUSE",
    "RATE_CARD_VERSION",
    "SERVICE_ZONE",
    "ITEM_COUNT",
    "BILL_AMOUNT",
    "BILL_COUNT",
    "ORDER_STATUS",
    "DELIVERY_TIMESTAMP",
    "RECON_RULE",
]

SYSTEM_PROMPT = """你是物流结算案件的证据摘要助手，不是付款决策者。
只能使用输入中的 evidence_ledger，不得补充外部事实或修改任何金额。
rule_decision 是确定性规则给出的唯一业务裁定；你的 verdict、
recommended_action 和 confidence 必须与其完全一致。
explanation 必须简洁说明证据如何支持该裁定，并在 evidence_ids 中列出
实际引用的证据。证据不足时必须说明 fallback_reason。不得输出思维链、
支付指令、退款指令或任何自动执行建议。"""


class ModelReview(BaseModel):
    """Business-safe structured output produced by a model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Verdict
    recommended_action: Action
    explanation: str = Field(min_length=12, max_length=500)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=11)
    confidence: Confidence
    fallback_reason: str | None = Field(default=None, max_length=240)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_must_be_unique(cls, value: list[EvidenceId]) -> list[EvidenceId]:
        if len(value) != len(set(value)):
            raise ValueError("evidence_ids must be unique")
        return value


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


class ModelReviewResult(BaseModel):
    """Runtime envelope exposed to the API, UI, logs, and evaluation."""

    status: Literal["generated", "disabled", "fallback"]
    review: ModelReview
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION
    latency_ms: float
    usage: ModelUsage = Field(default_factory=ModelUsage)
    guardrail_reasons: list[str] = Field(default_factory=list)


class StructuredReviewProvider(Protocol):
    name: str
    model: str

    def generate(self, prompt: str) -> tuple[ModelReview, ModelUsage]:
        """Return a parsed model review and usage metadata."""


class AnthropicReviewProvider:
    """Anthropic implementation loaded only when explicitly requested."""

    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, timeout_seconds: float = 15.0):
        import anthropic

        self.model = model
        self._client = anthropic.Anthropic(timeout=timeout_seconds)

    def generate(self, prompt: str) -> tuple[ModelReview, ModelUsage]:
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=ModelReview,
        )
        if message.parsed_output is None:
            raise ValueError("model returned no parsed output")
        usage = ModelUsage(
            input_tokens=int(getattr(message.usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(message.usage, "output_tokens", 0) or 0),
        )
        usage.estimated_cost_usd = estimate_cost(usage)
        return message.parsed_output, usage


def estimate_cost(usage: ModelUsage) -> float | None:
    """Estimate cost only when explicit current prices are configured.

    Prices intentionally remain environment configuration because provider
    pricing changes over time. Values are USD per one million tokens.
    """

    input_price = os.environ.get("ANTHROPIC_INPUT_USD_PER_MTOK")
    output_price = os.environ.get("ANTHROPIC_OUTPUT_USD_PER_MTOK")
    if not input_price or not output_price:
        return None
    return round(
        usage.input_tokens * float(input_price) / 1_000_000
        + usage.output_tokens * float(output_price) / 1_000_000,
        8,
    )


def evidence_ledger(evidence: dict) -> dict[str, object]:
    """Return only the bounded, named evidence fields available to the model."""

    order = evidence.get("order") or {}
    contract = evidence.get("contract") or {}
    return {
        "SOR_AMOUNT": evidence.get("sor_freight"),
        "CONTRACT_EXPECTED_AMOUNT": evidence.get("contract_expected_freight"),
        "CONTRACT_CLAUSE": contract.get("contract_clause_id"),
        "RATE_CARD_VERSION": contract.get("rate_card_version"),
        "SERVICE_ZONE": contract.get("service_zone"),
        "ITEM_COUNT": evidence.get("n_items"),
        "BILL_AMOUNT": evidence.get("billed_total"),
        "BILL_COUNT": evidence.get("n_bill_lines"),
        "ORDER_STATUS": order.get("order_status") if order else None,
        "DELIVERY_TIMESTAMP": (
            order.get("order_delivered_customer_date") if order else None
        ),
        "RECON_RULE": "金额与状态由 controlled-review-v3 确定性规则裁定",
    }


def available_evidence_ids(evidence: dict) -> set[str]:
    """Evidence IDs may cite explicit absence, so keys remain available."""

    ledger = evidence_ledger(evidence)
    available = {"SOR_AMOUNT", "ITEM_COUNT", "BILL_AMOUNT", "BILL_COUNT", "RECON_RULE"}
    if evidence.get("contract") is not None:
        available.update(
            {
                "CONTRACT_EXPECTED_AMOUNT",
                "CONTRACT_CLAUSE",
                "RATE_CARD_VERSION",
                "SERVICE_ZONE",
            }
        )
    if evidence.get("order") is not None:
        available.update({"ORDER_STATUS", "DELIVERY_TIMESTAMP"})
    return {key for key in available if key in ledger}


def build_prompt(
    recon_status: str,
    evidence: dict,
    rule_verdict: str,
    rule_action: str,
    rule_confidence: str,
    rule_rationale: str,
) -> str:
    payload = {
        "recon_status": recon_status,
        "evidence_ledger": evidence_ledger(evidence),
        "rule_decision": {
            "verdict": rule_verdict,
            "recommended_action": rule_action,
            "confidence": rule_confidence,
            "rationale": rule_rationale,
        },
        "output_contract": {
            "prompt_version": PROMPT_VERSION,
            "recommendation_only": True,
            "financial_execution": "disabled",
        },
    }
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def validate_generated_review(
    review: ModelReview,
    evidence: dict,
    rule_verdict: str,
    rule_action: str,
    rule_confidence: str,
) -> list[str]:
    reasons: list[str] = []
    if review.verdict != rule_verdict:
        reasons.append("模型裁定与确定性规则冲突")
    if review.recommended_action != rule_action:
        reasons.append("模型建议动作与确定性规则冲突")
    confidence_rank = {"低": 0, "中": 1, "高": 2}
    if confidence_rank[review.confidence] > confidence_rank[rule_confidence]:
        reasons.append("模型置信度高于规则允许上限")
    unknown_ids = set(review.evidence_ids) - available_evidence_ids(evidence)
    if unknown_ids:
        reasons.append(f"模型引用不存在的证据：{', '.join(sorted(unknown_ids))}")
    return reasons


def deterministic_review(
    evidence: dict,
    rule_verdict: str,
    rule_action: str,
    rule_confidence: str,
    rule_rationale: str,
    fallback_reason: str | None = None,
) -> ModelReview:
    explanation = (
        rule_rationale
        if len(rule_rationale.strip()) >= 12
        else f"确定性规则理由：{rule_rationale.strip()}"
    )
    return ModelReview(
        verdict=rule_verdict,
        recommended_action=rule_action,
        explanation=explanation,
        evidence_ids=sorted(available_evidence_ids(evidence)),
        confidence=rule_confidence,
        fallback_reason=fallback_reason,
    )


def generate_model_review(
    recon_status: str,
    evidence: dict,
    rule_verdict: str,
    rule_action: str,
    rule_confidence: str,
    rule_rationale: str,
    provider: StructuredReviewProvider | None = None,
) -> ModelReviewResult:
    """Generate a model summary and fail closed to deterministic evidence."""

    started = time.perf_counter()
    if provider is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            reason = "未配置 ANTHROPIC_API_KEY；已显示确定性规则理由"
            return ModelReviewResult(
                status="disabled",
                review=deterministic_review(
                    evidence,
                    rule_verdict,
                    rule_action,
                    rule_confidence,
                    rule_rationale,
                    reason,
                ),
                provider="none",
                model=DEFAULT_MODEL,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                guardrail_reasons=[reason],
            )
        try:
            provider = AnthropicReviewProvider()
        except (ImportError, ModuleNotFoundError):
            reason = "未安装 anthropic SDK；已显示确定性规则理由"
            return ModelReviewResult(
                status="disabled",
                review=deterministic_review(
                    evidence,
                    rule_verdict,
                    rule_action,
                    rule_confidence,
                    rule_rationale,
                    reason,
                ),
                provider="none",
                model=DEFAULT_MODEL,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                guardrail_reasons=[reason],
            )

    prompt = build_prompt(
        recon_status,
        evidence,
        rule_verdict,
        rule_action,
        rule_confidence,
        rule_rationale,
    )
    try:
        review, usage = provider.generate(prompt)
        guardrail_reasons = validate_generated_review(
            review, evidence, rule_verdict, rule_action, rule_confidence
        )
        status: Literal["generated", "fallback"] = (
            "fallback" if guardrail_reasons else "generated"
        )
        if guardrail_reasons:
            review = deterministic_review(
                evidence,
                rule_verdict,
                rule_action,
                rule_confidence,
                rule_rationale,
                "；".join(guardrail_reasons),
            )
        return ModelReviewResult(
            status=status,
            review=review,
            provider=provider.name,
            model=provider.model,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            usage=usage,
            guardrail_reasons=guardrail_reasons,
        )
    except Exception as exc:
        reason = f"模型调用或 Schema 校验失败：{type(exc).__name__}"
        return ModelReviewResult(
            status="fallback",
            review=deterministic_review(
                evidence,
                rule_verdict,
                rule_action,
                rule_confidence,
                rule_rationale,
                reason,
            ),
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", DEFAULT_MODEL),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            guardrail_reasons=[reason],
        )
