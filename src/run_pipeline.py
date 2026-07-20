"""Run the AP settlement reconciliation and AR risk-monitoring pipelines.

The two modules share source loading and reporting infrastructure, but keep
their business conclusions separate:
1. AP settlement reconciliation compares order freight, delivery evidence,
   and carrier bills.
2. AR risk monitoring calculates aging, DSO, and ECL indicators.

The injected labels are used only after rule execution for evaluation. They
never enter the reconciliation SQL or the case recommendation path.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GEN = ROOT / "data" / "generated"
SQL = ROOT / "sql"
OUT = ROOT / "output"
OUT.mkdir(parents=True, exist_ok=True)

POLICY_VERSION = "controlled-review-v3"
RULE_FILES = ("02_three_way_match.sql", "03_ar_aging.sql")

TRUTH_LABELS = {
    "MATCH": "MATCH",
    "AMOUNT_MISMATCH": "AMOUNT_MISMATCH",
    "DUPLICATE": "DUPLICATE",
    "MISSING_ORDER": "MISSING_ORDER",
    "NOT_DELIVERED": "NOT_DELIVERED",
    "DROP_NOT_BILLED": "NOT_BILLED",
}
PREDICTED_LABELS = {
    "MATCH": "MATCH",
    "OVERBILLED": "AMOUNT_MISMATCH",
    "UNDERBILLED": "AMOUNT_MISMATCH",
    "DUPLICATE": "DUPLICATE",
    "MISSING_ORDER": "MISSING_ORDER",
    "NOT_DELIVERED": "NOT_DELIVERED",
    "NOT_BILLED": "NOT_BILLED",
    "NO_ACTIVITY": "MATCH",
}


def p(path: Path) -> str:
    """Return a DuckDB-safe path."""
    return path.resolve().as_posix()


def load_base_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE raw_order_items AS
            SELECT * FROM read_csv_auto('{p(RAW / "olist_order_items_dataset.csv")}');
        CREATE OR REPLACE TABLE raw_orders AS
            SELECT * FROM read_csv_auto('{p(RAW / "olist_orders_dataset.csv")}');
        CREATE OR REPLACE TABLE carrier_bill AS
            SELECT * FROM read_csv_auto('{p(GEN / "carrier_bill.csv")}');
        CREATE OR REPLACE TABLE contract_expectations AS
            SELECT * FROM read_csv_auto('{p(GEN / "contract_expectations.csv")}');
        CREATE OR REPLACE TABLE contract_rate_card AS
            SELECT * FROM read_csv_auto('{p(GEN / "contract_rate_card.csv")}');
        CREATE OR REPLACE TABLE ar_invoices AS
            SELECT * FROM read_csv_auto('{p(GEN / "ar_invoices.csv")}');
        CREATE OR REPLACE TABLE rate_card AS
            SELECT * FROM read_csv_auto('{p(GEN / "rate_card.csv")}');
        CREATE OR REPLACE TABLE ecl_matrix AS
            SELECT * FROM read_csv_auto('{p(GEN / "ecl_matrix.csv")}');
        """
    )


def run_sql_file(con: duckdb.DuckDBPyConnection, name: str) -> None:
    con.execute((SQL / name).read_text(encoding="utf-8"))


def export(con: duckdb.DuckDBPyConnection, table: str) -> pd.DataFrame:
    frame = con.execute(f"SELECT * FROM {table}").fetchdf()
    frame.to_csv(OUT / f"{table}.csv", index=False)
    return frame


def evaluation_partition(order_id: str, seller_id: str) -> str:
    """Create a deterministic 20% holdout without leaking labels."""
    key = f"{order_id}|{seller_id}".encode("utf-8")
    return "holdout" if int(hashlib.sha256(key).hexdigest()[:8], 16) % 5 == 0 else "development"


def build_evaluation_frame(truth: pd.DataFrame, recon: pd.DataFrame) -> pd.DataFrame:
    frame = truth.merge(recon, on=["order_id", "seller_id"], how="left")
    frame["truth_label"] = frame["injected"].map(TRUTH_LABELS)
    frame["predicted_label"] = frame["recon_status"].map(PREDICTED_LABELS).fillna("UNKNOWN")
    frame["is_correct"] = frame["truth_label"] == frame["predicted_label"]
    frame["evaluation_partition"] = [
        evaluation_partition(str(order_id), str(seller_id))
        for order_id, seller_id in zip(frame["order_id"], frame["seller_id"])
    ]
    return frame


def classification_metrics(frame: pd.DataFrame) -> dict:
    truth_anomaly = frame["truth_label"] != "MATCH"
    predicted_anomaly = ~frame["predicted_label"].isin(["MATCH", "UNKNOWN"])
    tp = int((truth_anomaly & predicted_anomaly).sum())
    fp = int((~truth_anomaly & predicted_anomaly).sum())
    fn = int((truth_anomaly & ~predicted_anomaly).sum())
    tn = int((~truth_anomaly & ~predicted_anomaly).sum())

    def safe_div(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 6) if denominator else None

    precision = safe_div(tp, tp + fp)
    recall = safe_div(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 6)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "rows": int(len(frame)),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_positive_rate": safe_div(fp, fp + tn),
        "exact_label_accuracy": round(float(frame["is_correct"].mean()), 6)
        if len(frame)
        else None,
    }


def per_label_metrics(frame: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    labels = sorted(set(frame["truth_label"].dropna()) | set(frame["predicted_label"]))
    for label in labels:
        if label == "UNKNOWN":
            continue
        truth = frame["truth_label"] == label
        predicted = frame["predicted_label"] == label
        tp = int((truth & predicted).sum())
        fp = int((~truth & predicted).sum())
        fn = int((truth & ~predicted).sum())
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else None
        )
        rows.append(
            {
                "label": label,
                "support": int(truth.sum()),
                "precision": round(precision, 6) if precision is not None else None,
                "recall": round(recall, 6) if recall is not None else None,
                "f1": round(f1, 6) if f1 is not None else None,
            }
        )
    return rows


def validate_recon(con: duckdb.DuckDBPyConnection) -> dict:
    recon = con.execute(
        "SELECT order_id, seller_id, recon_status FROM recon"
    ).fetchdf()
    truth_bill = con.execute(
        """
        SELECT order_id, seller_id, ANY_VALUE("_injected_type") AS injected
        FROM carrier_bill GROUP BY order_id, seller_id
        """
    ).fetchdf()
    truth_drop = con.execute(
        """
        SELECT s.order_id, s.seller_id, 'DROP_NOT_BILLED' AS injected
        FROM contract_service s
        LEFT JOIN carrier_bill b USING(order_id, seller_id)
        WHERE b.order_id IS NULL AND s.is_delivered
        """
    ).fetchdf()
    truth = pd.concat([truth_bill, truth_drop], ignore_index=True)
    evaluation = build_evaluation_frame(truth, recon)
    evaluation.to_csv(OUT / "recon_evaluation.csv", index=False)
    summary = {
        "all": classification_metrics(evaluation),
        "holdout": classification_metrics(
            evaluation[evaluation["evaluation_partition"] == "holdout"]
        ),
        "per_label": per_label_metrics(evaluation),
        "label_source": "injected-ground-truth-used-after-rule-execution-only",
    }
    (OUT / "recon_evaluation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== Reconciliation evaluation ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def file_fingerprint(path: Path) -> dict | None:
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


def write_run_manifest(
    con: duckdb.DuckDBPyConnection, evaluation: dict, exported_tables: list[str]
) -> dict:
    rule_fingerprints = [
        fingerprint
        for name in RULE_FILES
        if (fingerprint := file_fingerprint(SQL / name)) is not None
    ]
    input_paths = [
        RAW / "olist_order_items_dataset.csv",
        RAW / "olist_orders_dataset.csv",
        GEN / "carrier_bill.csv",
        GEN / "contract_expectations.csv",
        GEN / "contract_rate_card.csv",
        GEN / "ar_invoices.csv",
        GEN / "rate_card.csv",
        GEN / "ecl_matrix.csv",
    ]
    row_counts = {
        table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in [
            "raw_order_items",
            "raw_orders",
            "carrier_bill",
            "contract_expectations",
            "contract_rate_card",
            "ar_invoices",
            *exported_tables,
        ]
    }
    created_at = datetime.now(timezone.utc)
    rules_digest = hashlib.sha256(
        "".join(item["sha256"] for item in rule_fingerprints).encode("ascii")
    ).hexdigest()
    manifest = {
        "manifest_version": "audit-run-v1",
        "run_id": f"RECON-{created_at:%Y%m%dT%H%M%SZ}-{rules_digest[:8]}",
        "generated_at_utc": created_at.isoformat(),
        "policy_version": POLICY_VERSION,
        "modules": {
            "ap_settlement_reconciliation": {
                "purpose": "carrier invoice review and exception evidence",
                "financial_execution": "disabled",
            },
            "ar_risk_monitoring": {
                "purpose": "aging, DSO and ECL monitoring",
                "financial_execution": "disabled",
            },
        },
        "input_files": [
            fingerprint
            for path in input_paths
            if (fingerprint := file_fingerprint(path)) is not None
        ],
        "rule_files": rule_fingerprints,
        "row_counts": row_counts,
        "evaluation": evaluation,
    }
    (OUT / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    con = duckdb.connect()
    try:
        print("[1/3] Loading source tables...")
        load_base_tables(con)
        print("[2/3] Running AP reconciliation and AR risk SQL...")
        for rule_file in RULE_FILES:
            run_sql_file(con, rule_file)
        print("[3/3] Exporting reviewed outputs...")
        tables = [
            "recon_summary",
            "recon_exceptions",
            "ar_aging_summary",
            "ar_kpi",
            "bad_debt_candidates",
            "ar_by_client",
        ]
        frames = {table: export(con, table) for table in tables}
        try:
            with pd.ExcelWriter(
                OUT / "settlement_report.xlsx", engine="openpyxl"
            ) as writer:
                for table in tables:
                    frames[table].head(1000).to_excel(
                        writer, sheet_name=table[:31], index=False
                    )
        except Exception as exc:  # CSV outputs remain the source of truth.
            print(f"Excel export skipped: {exc}")
        evaluation = validate_recon(con)
        manifest = write_run_manifest(con, evaluation, tables)
        print(f"Completed run {manifest['run_id']}; outputs: {OUT}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
