import pandas as pd
import pytest
from fastapi.testclient import TestClient

import src.copilot_api as api
from src.operational_store import OperationalStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    review_file = tmp_path / "exception_review.csv"
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
    monkeypatch.setattr(
        api, "STORE", OperationalStore(tmp_path / "operational.sqlite3")
    )
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


def test_evidence_bundle_contains_sources_decisions_events_and_lineage(
    client, monkeypatch
):
    evidence = {
        "sor_freight": 100.0,
        "n_items": 1,
        "items": pd.DataFrame([{"order_id": "order-1", "freight_value": 100.0}]),
        "order": {
            "order_status": "delivered",
            "order_delivered_customer_date": "2018-08-20 11:00:00",
        },
        "bills": pd.DataFrame([{"bill_id": "bill-1", "billed_freight": 130.0}]),
        "billed_total": 130.0,
        "billed_unit": 130.0,
        "n_bill_lines": 1,
    }
    monkeypatch.setattr(
        api,
        "_case_with_evidence",
        lambda case_id: (
            {
                "case_id": case_id,
                "recon_status": "OVERBILLED",
                "policy_version": "controlled-review-v2",
            },
            evidence,
            ("CONFIRMED", "追回应付差额", "中", "规则证据完整"),
        ),
    )
    decision = client.post(
        "/cases/REC-000001/human-decision",
        json={
            "decision": "APPROVED",
            "reviewer": "测试审核人",
            "notes": "证据完整",
            "idempotency_key": "bundle-decision-0001",
            "expected_state": "PENDING",
        },
    )
    response = client.get("/cases/REC-000001/evidence-bundle")
    assert decision.status_code == 200
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["bundle_version"] == "audit-evidence-v1"
    assert body["source_evidence"]["items"][0]["freight_value"] == 100.0
    assert body["human_decision"]["reviewer"] == "demo-reviewer"
    assert body["event_timeline"][0]["event_type"] == "DECISION_SUBMITTED"
    assert body["financial_execution"] == "disabled"


def test_api_key_roles_are_enforced(client, monkeypatch):
    monkeypatch.setenv(
        "REVIEW_API_KEYS",
        '{"viewer-key":{"actor_id":"viewer-1","role":"viewer"},'
        '"reviewer-key":{"actor_id":"reviewer-1","role":"reviewer"}}',
    )
    missing = client.get("/cases")
    allowed = client.get("/cases", headers={"X-API-Key": "viewer-key"})
    forbidden = client.post(
        "/cases/REC-000001/human-decision",
        headers={"X-API-Key": "viewer-key"},
        json={
            "decision": "APPROVED",
            "notes": "只读角色不得提交",
            "idempotency_key": "role-check-0001",
            "expected_state": "PENDING",
        },
    )
    reviewer = client.post(
        "/cases/REC-000001/human-decision",
        headers={"X-API-Key": "reviewer-key"},
        json={
            "decision": "APPROVED",
            "notes": "证据已复核",
            "idempotency_key": "role-check-0002",
            "expected_state": "PENDING",
        },
    )
    assert missing.status_code == 401
    assert allowed.status_code == 200
    assert forbidden.status_code == 403
    assert reviewer.status_code == 200
    assert reviewer.json()["decision"]["reviewer"] == "reviewer-1"
