import pandas as pd
import pytest
from fastapi.testclient import TestClient

import src.copilot_api as api


@pytest.fixture
def client(tmp_path, monkeypatch):
    review_file = tmp_path / "exception_review.csv"
    decision_file = tmp_path / "human_decisions.csv"
    event_file = tmp_path / "product_events.csv"
    pd.DataFrame(
        [
            {
                "case_id": "REC-000001",
                "priority": "P1",
                "recon_status": "OVERBILLED",
                "order_id": "order-1",
                "seller_id": "seller-1",
                "sor_freight": 100.0,
                "billed_total": 130.0,
                "impact_amount": 30.0,
                "verdict": "CONFIRMED",
                "recommended_action": "追回差额",
                "confidence": "中",
                "rationale": "规则理由",
                "policy_version": "controlled-review-v2",
            }
        ]
    ).to_csv(review_file, index=False)
    monkeypatch.setattr(api, "REVIEW_FILE", review_file)
    monkeypatch.setattr(api, "DECISION_FILE", decision_file)
    monkeypatch.setattr(api, "EVENT_FILE", event_file)
    api.MODEL_CACHE.clear()
    api.REQUEST_LATENCIES_MS.clear()
    return TestClient(api.app)


def test_frontend_origin_is_allowed(client):
    response = client.options(
        "/cases",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:8080"


def test_health_exposes_model_and_execution_status(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["financial_execution"] == "disabled"
    assert response.json()["model_summary"] == "disabled"
    assert response.headers["X-Process-Time-Ms"]


def test_human_decision_is_idempotent_and_not_overwritable(client):
    payload = {
        "decision": "APPROVED",
        "reviewer": "测试审核人",
        "notes": "证据完整",
        "idempotency_key": "decision-key-0001",
        "expected_state": "PENDING",
    }
    first = client.post("/cases/REC-000001/human-decision", json=payload)
    replay = client.post("/cases/REC-000001/human-decision", json=payload)
    conflict = client.post(
        "/cases/REC-000001/human-decision",
        json={**payload, "idempotency_key": "decision-key-0002"},
    )
    assert first.status_code == 200
    assert first.json()["idempotent_replay"] is False
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert conflict.status_code == 409


def test_model_endpoint_returns_explicit_disabled_fallback(
    client, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    evidence = {
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
    monkeypatch.setattr(
        api,
        "_case_with_evidence",
        lambda case_id: (
            {"recon_status": "OVERBILLED"},
            evidence,
            ("CONFIRMED", "追回差额", "中", "规则理由保持可见。"),
        ),
    )
    response = client.post(
        "/cases/REC-000001/model-review",
        json={"request_id": "model-request-0001"},
    )
    replay = client.post(
        "/cases/REC-000001/model-review",
        json={"request_id": "model-request-0001"},
    )
    cross_case = client.post(
        "/cases/REC-OTHER/model-review",
        json={"request_id": "model-request-0001"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model_review"]["status"] == "disabled"
    assert body["recommendation_source"] == "deterministic-rule"
    assert body["financial_execution"] == "disabled"
    assert replay.json()["idempotent_replay"] is True
    assert cross_case.status_code == 409


def test_event_and_metrics_endpoints(client):
    event = client.post(
        "/events",
        json={
            "event_type": "CASE_OPENED",
            "case_id": "REC-000001",
            "session_id": "session-0001",
            "metadata": {"source": "test"},
        },
    )
    metrics = client.get("/metrics/summary")
    assert event.status_code == 200
    assert metrics.status_code == 200
    assert metrics.json()["financial_execution"] == "disabled"
    assert metrics.json()["observed_request_count"] >= 1
