"""Local API for the controlled Settlement Review Copilot MVP.

The API exposes evidence, deterministic recommendations, an optional guarded
model summary, and human decision records. It contains no payment, recovery,
refund, invoicing, or other financial-execution endpoint.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:  # Supports both `uvicorn src.copilot_api:app` and direct execution.
    from .audit_agent import connect, review, trace
    from .model_review import PROMPT_VERSION, generate_model_review
    from .operational_store import DecisionConflict, OperationalStore
except ImportError:  # pragma: no cover
    from audit_agent import connect, review, trace
    from model_review import PROMPT_VERSION, generate_model_review
    from operational_store import DecisionConflict, OperationalStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
REVIEW_FILE = OUT / "exception_review.csv"
STORE = OperationalStore(OUT / "operational.sqlite3")

DEFAULT_ORIGINS = ",".join(
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "null",
    ]
)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("COPILOT_ALLOWED_ORIGINS", DEFAULT_ORIGINS).split(",")
    if origin.strip()
]

MODEL_CACHE_LOCK = threading.Lock()
MODEL_CACHE: dict[str, dict] = {}
REQUEST_LATENCIES_MS: deque[float] = deque(maxlen=1_000)

app = FastAPI(title="Settlement Review Copilot API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class HumanDecision(BaseModel):
    decision: Literal["APPROVED", "REJECTED", "ESCALATED"]
    reviewer: str = Field(min_length=2, max_length=80)
    notes: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=100)
    expected_state: Literal["PENDING"] = "PENDING"


class ModelReviewRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=100)


class ProductEvent(BaseModel):
    event_type: Literal[
        "CASE_OPENED",
        "MODEL_REVIEW_REQUESTED",
        "MODEL_REVIEW_COMPLETED",
        "DECISION_SUBMITTED",
    ]
    case_id: str
    session_id: str = Field(min_length=8, max_length=100)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


@app.middleware("http")
async def observe_latency(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    REQUEST_LATENCIES_MS.append(elapsed_ms)
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    return response


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


def _latest_decision(case_id: str) -> dict | None:
    return STORE.latest_decision(case_id)


def _case_row(case_id: str) -> dict:
    reviews = _load_reviews()
    match = reviews[reviews["case_id"] == case_id]
    if match.empty:
        raise HTTPException(status_code=404, detail="未找到案件")
    return _record(match.iloc[0])


def _case_with_evidence(case_id: str) -> tuple[dict, dict, tuple[str, str, str, str]]:
    item = _case_row(case_id)
    con = connect()
    try:
        evidence = trace(con, item["order_id"], item["seller_id"])
        rule_result = review(item["recon_status"], evidence)
    finally:
        con.close()
    return item, evidence, rule_result


def _event_record(payload: ProductEvent) -> dict:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "event_type": payload.event_type,
        "case_id": payload.case_id,
        "session_id": payload.session_id,
        "metadata_json": json.dumps(payload.metadata, ensure_ascii=False, sort_keys=True),
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": "recommendation-only",
        "financial_execution": "disabled",
        "model_summary": (
            "available" if os.environ.get("ANTHROPIC_API_KEY") else "disabled"
        ),
        "prompt_version": PROMPT_VERSION,
    }


@app.get("/cases")
def cases(priority: str | None = None, limit: int = 50) -> list[dict]:
    reviews = _load_reviews()
    if priority:
        reviews = reviews[reviews["priority"] == priority]
    records = []
    for _, row in reviews.head(min(limit, 200)).iterrows():
        item = _record(row)
        latest = _latest_decision(item["case_id"])
        item["case_state"] = (
            latest.get("human_decision", "PENDING") if latest else "PENDING"
        )
        records.append(item)
    return records


@app.get("/cases/{case_id}")
def case_detail(case_id: str) -> dict:
    item, evidence, (_, _, _, current_rationale) = _case_with_evidence(case_id)
    latest = _latest_decision(case_id)
    item["case_state"] = (
        latest.get("human_decision", "PENDING") if latest else "PENDING"
    )
    item["latest_decision"] = latest
    item["evidence"] = {
        "sor_freight": evidence["sor_freight"],
        "item_count": evidence["n_items"],
        "billed_total": evidence["billed_total"],
        "bill_lines": evidence["n_bill_lines"],
        "order_status": (evidence["order"] or {}).get("order_status"),
        "customer_delivered_at": (evidence["order"] or {}).get(
            "order_delivered_customer_date"
        ),
        "rationale": current_rationale,
    }
    return item


def _file_fingerprint(path: Path) -> dict | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
    }


def _dataframe_records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _json_record(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in record.items()
    }


@app.get("/cases/{case_id}/evidence-bundle")
def evidence_bundle(case_id: str):
    item, evidence, rule_result = _case_with_evidence(case_id)
    verdict, action, confidence, rationale = rule_result
    bundle = {
        "bundle_version": "audit-evidence-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case": item,
        "source_evidence": {
            "sor_freight": evidence["sor_freight"],
            "item_count": evidence["n_items"],
            "items": _dataframe_records(evidence["items"]),
            "order": _json_record(evidence["order"]),
            "bills": _dataframe_records(evidence["bills"]),
            "billed_total": evidence["billed_total"],
            "bill_lines": evidence["n_bill_lines"],
        },
        "rule_decision": {
            "verdict": verdict,
            "recommended_action": action,
            "confidence": confidence,
            "rationale": rationale,
            "policy_version": item.get("policy_version", "controlled-review-v2"),
        },
        "human_decision": _latest_decision(case_id),
        "event_timeline": STORE.case_events(case_id),
        "lineage": {
            "run_manifest": (
                json.loads((OUT / "run_manifest.json").read_text(encoding="utf-8"))
                if (OUT / "run_manifest.json").exists()
                else None
            ),
            "source_files": [
                fingerprint
                for fingerprint in (
                    _file_fingerprint(ROOT / "data" / "generated" / "carrier_bill.csv"),
                    _file_fingerprint(ROOT / "sql" / "02_three_way_match.sql"),
                )
                if fingerprint is not None
            ],
        },
        "financial_execution": "disabled",
    }
    return JSONResponse(
        jsonable_encoder(bundle),
        headers={"Content-Disposition": f'attachment; filename="{case_id}_evidence.json"'},
    )


@app.post("/cases/{case_id}/model-review")
def model_review(case_id: str, payload: ModelReviewRequest) -> dict:
    with MODEL_CACHE_LOCK:
        cached = MODEL_CACHE.get(payload.request_id)
    if cached is not None:
        if cached["case_id"] != case_id:
            raise HTTPException(
                status_code=409,
                detail="request_id 已用于其他案件，请为本案件生成新的请求 ID",
            )
        return {**cached, "idempotent_replay": True}

    item, evidence, rule_result = _case_with_evidence(case_id)
    verdict, action, confidence, rationale = rule_result
    result = generate_model_review(
        item["recon_status"],
        evidence,
        verdict,
        action,
        confidence,
        rationale,
    )
    response = {
        "case_id": case_id,
        "request_id": payload.request_id,
        "recommendation_source": "deterministic-rule",
        "financial_execution": "disabled",
        "model_review": result.model_dump(),
        "idempotent_replay": False,
    }
    with MODEL_CACHE_LOCK:
        MODEL_CACHE[payload.request_id] = response
    STORE.append_event(
        _event_record(
            ProductEvent(
                event_type="MODEL_REVIEW_COMPLETED",
                case_id=case_id,
                session_id=payload.request_id,
                metadata={
                    "status": result.status,
                    "provider": result.provider,
                    "model": result.model,
                    "prompt_version": result.prompt_version,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "estimated_cost_usd": result.usage.estimated_cost_usd,
                },
            )
        )
    )
    return response


@app.post("/cases/{case_id}/human-decision")
def human_decision(case_id: str, payload: HumanDecision) -> dict:
    row = _case_row(case_id)
    if payload.expected_state != "PENDING":
        raise HTTPException(status_code=409, detail="案件状态已变化，请刷新后重试")
    decision = {
        "case_id": case_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "reviewer": payload.reviewer,
        "human_decision": payload.decision,
        "notes": payload.notes,
        "recommended_action": row["recommended_action"],
        "impact_amount": float(row["impact_amount"]),
        "policy_version": row.get("policy_version", "controlled-review-v2"),
        "idempotency_key": payload.idempotency_key,
        "previous_state": "PENDING",
        "new_state": payload.decision,
    }
    try:
        saved, replay = STORE.submit_decision(decision)
    except DecisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "案件已完成决定；不允许覆盖首条审计记录",
                "current_state": exc.existing.get("human_decision"),
            },
        ) from exc
    return {
        "saved": True,
        "idempotent_replay": replay,
        "execution": "disabled",
        "decision": saved,
    }


@app.post("/events")
def record_event(payload: ProductEvent) -> dict:
    if payload.case_id:
        _case_row(payload.case_id)
    STORE.append_event(_event_record(payload))
    return {"saved": True}


@app.get("/metrics/summary")
def metrics_summary() -> dict:
    store_metrics = STORE.metrics()
    decision_counts = store_metrics["decision_counts"]
    total_decisions = store_metrics["total_decisions"]
    latencies = sorted(REQUEST_LATENCIES_MS)
    p95_ms = None
    if latencies:
        p95_ms = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    return {
        "decision_counts": decision_counts,
        "acceptance_rate": (
            round(decision_counts["APPROVED"] / total_decisions, 4)
            if total_decisions
            else None
        ),
        "p95_api_latency_ms": p95_ms,
        "observed_request_count": len(latencies),
        "financial_execution": "disabled",
    }
