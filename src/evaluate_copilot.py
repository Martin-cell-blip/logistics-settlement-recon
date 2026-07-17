"""Offline rule, safety, and model-guardrail evaluation.

The rule fixtures contain explicit evidence fields and independently declared
expected labels. They are regression tests, not a claim of production or model
accuracy. Live model evaluation is opt-in because it requires credentials and
incurs provider cost.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

try:
    from .audit_agent import review
    from .model_review import (
        ModelReview,
        ModelUsage,
        generate_model_review,
    )
except ImportError:  # pragma: no cover - direct script execution
    from audit_agent import review
    from model_review import ModelReview, ModelUsage, generate_model_review

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _optional_text(value: object) -> str | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    return str(value)


def evidence_from_row(case) -> dict:
    """Build evidence only from fixture columns, never from expected labels."""

    order = None
    if _bool(case.order_present):
        order = {
            "order_status": _optional_text(case.order_status),
            "order_delivered_customer_date": _optional_text(case.delivered_at),
        }
    return {
        "sor_freight": float(case.sor_freight),
        "n_items": int(case.n_items),
        "items": None,
        "order": order,
        "bills": None,
        "billed_total": float(case.billed_total),
        "billed_unit": float(case.billed_unit),
        "n_bill_lines": int(case.n_bill_lines),
    }


def _score(path: Path, safety: bool = False) -> pd.DataFrame:
    cases = pd.read_csv(path)
    rows = []
    for case in cases.itertuples(index=False):
        verdict, action, confidence, rationale = review(
            case.recon_status, evidence_from_row(case)
        )
        rows.append(
            {
                "case_id": case.case_id,
                "scenario": case.scenario,
                "recon_status": case.recon_status,
                "verdict_ok": verdict == case.expected_verdict,
                "action_ok": action == case.expected_action,
                "safe_fallback_ok": (
                    verdict == "SUSPECT" and action == "人工复核"
                    if safety
                    else True
                ),
                "actual_verdict": verdict,
                "actual_action": action,
                "confidence": confidence,
                "rationale": rationale,
            }
        )
    return pd.DataFrame(rows)


def score_golden() -> pd.DataFrame:
    return _score(EVAL / "golden_cases.csv")


def score_bad_cases() -> pd.DataFrame:
    return _score(EVAL / "bad_cases.csv", safety=True)


class FixtureProvider:
    name = "fixture"
    model = "guardrail-fixture-v1"

    def __init__(self, output: ModelReview):
        self.output = output

    def generate(self, prompt: str) -> tuple[ModelReview, ModelUsage]:
        return self.output, ModelUsage(input_tokens=100, output_tokens=30)


def score_model_guardrails() -> pd.DataFrame:
    """Exercise accept, conflict, and overconfidence paths without an API call."""

    case = pd.read_csv(EVAL / "golden_cases.csv").query(
        "recon_status == 'OVERBILLED'"
    ).iloc[0]
    evidence = evidence_from_row(case)
    verdict, action, confidence, rationale = review(case.recon_status, evidence)
    fixtures = [
        (
            "aligned-output",
            ModelReview(
                verdict=verdict,
                recommended_action=action,
                explanation="账单金额高于系统应计金额，且已超过规则允许的容差范围。",
                evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
                confidence=confidence,
            ),
            "generated",
        ),
        (
            "verdict-conflict",
            ModelReview(
                verdict="PASS",
                recommended_action=action,
                explanation="模型错误地尝试改变规则裁定，护栏应阻止该结果进入界面。",
                evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
                confidence=confidence,
            ),
            "fallback",
        ),
        (
            "action-conflict",
            ModelReview(
                verdict=verdict,
                recommended_action="无需处理",
                explanation="模型错误地尝试改变规则动作，护栏应阻止该结果进入界面。",
                evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
                confidence=confidence,
            ),
            "fallback",
        ),
        (
            "overconfidence",
            ModelReview(
                verdict=verdict,
                recommended_action=action,
                explanation="模型将中置信度规则擅自提高为高置信度，护栏应强制回退。",
                evidence_ids=["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
                confidence="高",
            ),
            "fallback",
        ),
    ]
    rows = []
    for scenario, output, expected_status in fixtures:
        result = generate_model_review(
            case.recon_status,
            evidence,
            verdict,
            action,
            confidence,
            rationale,
            provider=FixtureProvider(output),
        )
        rows.append(
            {
                "scenario": scenario,
                "expected_status": expected_status,
                "actual_status": result.status,
                "guardrail_ok": result.status == expected_status,
                "guardrail_reasons": "；".join(result.guardrail_reasons),
            }
        )
    return pd.DataFrame(rows)


def score_live_model(limit: int) -> pd.DataFrame:
    """Optional cost-bearing model run over independent synthetic fixtures."""

    cases = pd.read_csv(EVAL / "golden_cases.csv").head(limit)
    rows = []
    for case in cases.itertuples(index=False):
        evidence = evidence_from_row(case)
        verdict, action, confidence, rationale = review(
            case.recon_status, evidence
        )
        result = generate_model_review(
            case.recon_status,
            evidence,
            verdict,
            action,
            confidence,
            rationale,
        )
        rows.append(
            {
                "case_id": case.case_id,
                "status": result.status,
                "provider": result.provider,
                "model": result.model,
                "latency_ms": result.latency_ms,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "estimated_cost_usd": result.usage.estimated_cost_usd,
                "guardrail_reasons": "；".join(result.guardrail_reasons),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="调用配置的模型评测结构化摘要；会产生 API 成本",
    )
    parser.add_argument("--model-limit", type=int, default=5)
    args = parser.parse_args()

    golden = score_golden()
    safety = score_bad_cases()
    guardrails = score_model_guardrails()

    print("=== 离线规则回归（独立证据字段的合成夹具）===")
    print(f"案件数: {len(golden)}")
    print(f"裁定准确率: {golden.verdict_ok.mean():.1%}")
    print(f"建议动作准确率: {golden.action_ok.mean():.1%}")
    print("\n=== 安全兜底（证据缺失或冲突）===")
    print(f"案件数: {len(safety)}")
    print(f"安全兜底通过率: {safety.safe_fallback_ok.mean():.1%}")
    print("\n=== 模型输出护栏（无 API 调用）===")
    print(f"场景数: {len(guardrails)}")
    print(f"护栏通过率: {guardrails.guardrail_ok.mean():.1%}")

    all_passed = (
        golden.verdict_ok.all()
        and golden.action_ok.all()
        and safety.safe_fallback_ok.all()
        and guardrails.guardrail_ok.all()
    )
    if not all_passed:
        raise SystemExit("评测未通过，请检查失败案例。")

    if args.with_model:
        live = score_live_model(max(1, min(args.model_limit, len(golden))))
        print("\n=== 可选真实模型结构化摘要评测 ===")
        print(live.to_string(index=False))
        print("\n状态分布:")
        print(live.status.value_counts().to_string())
        print(f"P95 时延: {live.latency_ms.quantile(0.95):.2f} ms")


if __name__ == "__main__":
    main()
