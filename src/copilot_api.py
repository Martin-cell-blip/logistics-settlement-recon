"""Small local API for the Settlement Review Copilot MVP.

It deliberately exposes recommendations and evidence only.  Any financial
action is recorded as a human decision; the API cannot execute payment,
recovery, or invoice operations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:  # Supports both `uvicorn src.copilot_api:app` and direct script execution.
    from .audit_agent import connect, review, trace
except ImportError:  # pragma: no cover
    from audit_agent import connect, review, trace

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
REVIEW_FILE = OUT / "exception_review.csv"
DECISION_FILE = OUT / "human_decisions.csv"

app = FastAPI(title="Settlement Review Copilot API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "null"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class HumanDecision(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "ESCALATED"]
    reviewer: str = Field(min_length=2, max_length=80)
    notes: str = Field(default="", max_length=500)


def _load_reviews() -> pd.DataFrame:
    if not REVIEW_FILE.exists():
        raise HTTPException(
            status_code=409,
            detail="尚无复核数据。请先依次运行 run_pipeline.py 和 audit_agent.py。",
        )
    reviews = pd.read_csv(REVIEW_FILE)
    if "case_id" not in reviews.columns:
        reviews.insert(0, "case_id", [f"REC-{i + 1:06d}" for i in range(len(reviews))])
    return reviews


def _record(row: pd.Series) -> dict:
    return {
        key: (None if pd.isna(value) else value)
        for key, value in row.to_dict().items()
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "recommendation-only", "financial_execution": "disabled"}


@app.get("/cases")
def cases(priority: str | None = None, limit: int = 50) -> list[dict]:
    reviews = _load_reviews()
    if priority:
        reviews = reviews[reviews["priority"] == priority]
    return [_record(row) for _, row in reviews.head(min(limit, 200)).iterrows()]


@app.get("/cases/{case_id}")
def case_detail(case_id: str) -> dict:
    reviews = _load_reviews()
    match = reviews[reviews["case_id"] == case_id]
    if match.empty:
        raise HTTPException(status_code=404, detail="未找到案件")
    item = _record(match.iloc[0])
    con = connect()
    try:
        evidence = trace(con, item["order_id"], item["seller_id"])
        _, _, _, current_rationale = review(item["recon_status"], evidence)
    finally:
        con.close()
    item["evidence"] = {
        "sor_freight": evidence["sor_freight"],
        "billed_total": evidence["billed_total"],
        "bill_lines": evidence["n_bill_lines"],
        "order_status": (evidence["order"] or {}).get("order_status"),
        "customer_delivered_at": (evidence["order"] or {}).get("order_delivered_customer_date"),
        "rationale": current_rationale,
    }
    return item


@app.post("/cases/{case_id}/human-decision")
def human_decision(case_id: str, payload: HumanDecision) -> dict:
    reviews = _load_reviews()
    match = reviews[reviews["case_id"] == case_id]
    if match.empty:
        raise HTTPException(status_code=404, detail="未找到案件")
    row = _record(match.iloc[0])
    decision = {
        "case_id": case_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "reviewer": payload.reviewer,
        "human_decision": payload.decision,
        "notes": payload.notes,
        "recommended_action": row["recommended_action"],
        "impact_amount": row["impact_amount"],
        "policy_version": row.get("policy_version", "controlled-review-v1"),
    }
    pd.DataFrame([decision]).to_csv(
        DECISION_FILE, mode="a", index=False, header=not DECISION_FILE.exists()
    )
    return {"saved": True, "execution": "disabled", "decision": decision}
