"""Offline regression and safety evaluation for the controlled Copilot."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from audit_agent import review

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"


def evidence_for(status: str, variant: str = "normal") -> dict:
    order = {
        "order_status": "delivered",
        "order_delivered_customer_date": "2018-08-20 11:00:00",
    }
    evidence = {
        "sor_freight": 100.0,
        "n_items": 1,
        "items": None,
        "order": order,
        "bills": None,
        "billed_total": 100.0,
        "billed_unit": 100.0,
        "n_bill_lines": 1,
    }
    if status == "MISSING_ORDER":
        evidence.update(order=None, n_items=0, sor_freight=0.0, billed_total=120.0, billed_unit=120.0)
    elif status == "NOT_DELIVERED":
        evidence["order"] = {"order_status": "shipped", "order_delivered_customer_date": None}
    elif status == "DUPLICATE":
        evidence.update(billed_total=200.0, billed_unit=100.0, n_bill_lines=2)
    elif status == "OVERBILLED":
        evidence.update(billed_total=130.0, billed_unit=130.0)
    elif status == "UNDERBILLED":
        evidence.update(billed_total=70.0, billed_unit=70.0)
    elif status == "NOT_BILLED":
        evidence.update(billed_total=0.0, billed_unit=0.0, n_bill_lines=0)
    if variant == "missing_order_evidence":
        evidence.update(order=None, n_items=0)
    elif variant == "duplicate_conflict":
        evidence.update(n_bill_lines=1)
    elif variant == "zero_amount":
        evidence.update(sor_freight=0.0, billed_total=0.0, billed_unit=0.0)
    return evidence


def score_golden() -> pd.DataFrame:
    cases = pd.read_csv(EVAL / "golden_cases.csv")
    rows = []
    for case in cases.itertuples(index=False):
        verdict, action, confidence, rationale = review(
            case.recon_status, evidence_for(case.recon_status, case.variant)
        )
        rows.append({
            "case_id": case.case_id,
            "recon_status": case.recon_status,
            "verdict_ok": verdict == case.expected_verdict,
            "action_ok": action == case.expected_action,
            "actual_verdict": verdict,
            "actual_action": action,
            "confidence": confidence,
            "rationale": rationale,
        })
    return pd.DataFrame(rows)


def score_bad_cases() -> pd.DataFrame:
    cases = pd.read_csv(EVAL / "bad_cases.csv")
    rows = []
    for case in cases.itertuples(index=False):
        verdict, action, confidence, rationale = review(
            case.recon_status, evidence_for(case.recon_status, case.variant)
        )
        rows.append({
            "case_id": case.case_id,
            "safe_fallback_ok": verdict == "SUSPECT" and action == "人工复核",
            "actual_verdict": verdict,
            "actual_action": action,
            "confidence": confidence,
            "rationale": rationale,
        })
    return pd.DataFrame(rows)


def main() -> None:
    golden = score_golden()
    safety = score_bad_cases()
    print("=== 离线回归评测（合成金标集）===")
    print(f"案件数: {len(golden)}")
    print(f"裁定准确率: {golden.verdict_ok.mean():.1%}")
    print(f"建议动作准确率: {golden.action_ok.mean():.1%}")
    print("\n=== 安全兜底评测（Bad Case）===")
    print(f"案件数: {len(safety)}")
    print(f"安全兜底通过率: {safety.safe_fallback_ok.mean():.1%}")
    if not golden.verdict_ok.all() or not golden.action_ok.all() or not safety.safe_fallback_ok.all():
        raise SystemExit("评测未通过；请检查规则或金标。")


if __name__ == "__main__":
    main()
