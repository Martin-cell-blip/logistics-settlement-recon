import pandas as pd

from src.audit_agent import review


def _evidence(order_status: str, delivered_at) -> dict:
    return {
        "sor_freight": 20.0,
        "contract_expected_freight": 20.0,
        "n_items": 1,
        "order": {
            "order_status": order_status,
            "order_delivered_customer_date": delivered_at,
        },
        "billed_total": 20.0,
        "billed_unit": 20.0,
        "n_bill_lines": 1,
    }


def test_not_delivered_with_missing_date_is_confirmed():
    # DuckDB returns a missing delivery date as NaT; it must count as "no delivery".
    verdict, action, confidence, _ = review("NOT_DELIVERED", _evidence("shipped", pd.NaT))
    assert (verdict, action, confidence) == ("CONFIRMED", "拒付整笔", "高")


def test_not_delivered_with_delivery_date_goes_to_human_review():
    ev = _evidence("shipped", pd.Timestamp("2018-03-01 10:00"))
    verdict, action, _, _ = review("NOT_DELIVERED", ev)
    assert (verdict, action) == ("SUSPECT", "人工复核")


def test_not_billed_without_delivery_date_goes_to_human_review():
    verdict, action, _, _ = review("NOT_BILLED", _evidence("delivered", pd.NaT))
    assert (verdict, action) == ("SUSPECT", "人工复核")
