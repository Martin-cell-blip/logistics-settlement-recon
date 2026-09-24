import pandas as pd

from src.generate_data import (
    N_GHOST_ORDERS,
    _ghost_order_id,
    _stable_carrier,
    build_contract_rate_card,
)
from src.operational_store import OperationalStore


def test_contract_rate_card_has_unique_versioned_clauses():
    rate_card = build_contract_rate_card()
    assert len(rate_card) == 8 * 3 * 3
    assert rate_card["contract_clause_id"].is_unique
    assert set(rate_card["service_zone"]) == {"LOCAL", "REGIONAL", "NATIONAL"}
    assert set(rate_card["rate_card_version"]) == {"contract-2018-v1"}
    assert (rate_card["base_fee"] > 0).all()
    assert (rate_card["per_kg_fee"] > 0).all()


def test_carrier_assignment_is_deterministic():
    assert _stable_carrier("order-1", "seller-1") == _stable_carrier(
        "order-1", "seller-1"
    )


def test_ghost_order_ids_are_seeded_and_unique():
    # uuid4() made these IDs differ on every run, so the ID-based hold-out split drifted.
    ids = [_ghost_order_id(i) for i in range(N_GHOST_ORDERS)]
    assert ids == [_ghost_order_id(i) for i in range(N_GHOST_ORDERS)]
    assert len(set(ids)) == N_GHOST_ORDERS
    assert all(len(x) == 32 and set(x) <= set("0123456789abcdef") for x in ids)


def test_operational_store_applies_schema_migrations(tmp_path):
    store = OperationalStore(tmp_path / "ops.sqlite3")
    with store._connect() as con:
        versions = {
            row["version"] for row in con.execute("SELECT version FROM schema_migrations")
        }
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(human_decisions)")
        }
    assert versions == {1, 2}
    assert "actor_role" in columns
