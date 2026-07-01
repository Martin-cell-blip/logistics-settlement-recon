"""
run_pipeline.py — 端到端跑通结算对账 + 应收账龄流水线
  1) 把 Olist 原始表 + 合成表载入 DuckDB
  2) 执行 sql/02_three_way_match.sql（三方对账）与 sql/03_ar_aging.sql（账龄/DSO/坏账）
  3) 导出异常清单 / 汇总 / 账龄 / 坏账候选到 output/（CSV，若有 openpyxl 则另出一个多 sheet 的 xlsx）
  4) 用注入的 ground-truth 计算对账引擎的查全率（recall），证明"异常识别覆盖率"

运行：  PYTHONUTF8=1 python src/run_pipeline.py
"""
from __future__ import annotations
from pathlib import Path
import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GEN = ROOT / "data" / "generated"
SQL = ROOT / "sql"
OUT = ROOT / "output"
OUT.mkdir(parents=True, exist_ok=True)


def p(path: Path) -> str:
    """DuckDB 用正斜杠路径。"""
    return path.resolve().as_posix()


def load_base_tables(con: duckdb.DuckDBPyConnection):
    con.execute(f"""
        CREATE OR REPLACE TABLE raw_order_items AS
            SELECT * FROM read_csv_auto('{p(RAW / "olist_order_items_dataset.csv")}');
        CREATE OR REPLACE TABLE raw_orders AS
            SELECT * FROM read_csv_auto('{p(RAW / "olist_orders_dataset.csv")}');
        CREATE OR REPLACE TABLE carrier_bill AS
            SELECT * FROM read_csv_auto('{p(GEN / "carrier_bill.csv")}');
        CREATE OR REPLACE TABLE ar_invoices AS
            SELECT * FROM read_csv_auto('{p(GEN / "ar_invoices.csv")}');
        CREATE OR REPLACE TABLE rate_card AS
            SELECT * FROM read_csv_auto('{p(GEN / "rate_card.csv")}');
        CREATE OR REPLACE TABLE ecl_matrix AS
            SELECT * FROM read_csv_auto('{p(GEN / "ecl_matrix.csv")}');
    """)


def run_sql_file(con: duckdb.DuckDBPyConnection, name: str):
    con.execute((SQL / name).read_text(encoding="utf-8"))


def export(con: duckdb.DuckDBPyConnection, table: str) -> pd.DataFrame:
    df = con.execute(f"SELECT * FROM {table}").fetchdf()
    df.to_csv(OUT / f"{table}.csv", index=False)
    return df


def validate_recon(con: duckdb.DuckDBPyConnection):
    """用 carrier_bill._injected_type（注入真值）核对对账引擎判定，计算各异常类查全率。"""
    recon = con.execute("SELECT order_id, seller_id, recon_status FROM recon").fetchdf()
    # 真值A：账单侧（每组取注入类型）
    truth_bill = con.execute("""
        SELECT order_id, seller_id, ANY_VALUE("_injected_type") AS injected
        FROM carrier_bill GROUP BY order_id, seller_id
    """).fetchdf()
    # 真值B：系统已送达但被漏计的组 → 期望 NOT_BILLED
    truth_drop = con.execute("""
        SELECT s.order_id, s.seller_id, 'DROP_NOT_BILLED' AS injected
        FROM (SELECT oi.order_id, oi.seller_id,
                     (ANY_VALUE(o.order_status)='delivered'
                      AND ANY_VALUE(o.order_delivered_customer_date) IS NOT NULL) AS deliv
              FROM raw_order_items oi LEFT JOIN raw_orders o USING(order_id)
              GROUP BY oi.order_id, oi.seller_id) s
        LEFT JOIN carrier_bill b USING(order_id, seller_id)
        WHERE b.order_id IS NULL AND s.deliv
    """).fetchdf()
    truth = pd.concat([truth_bill, truth_drop], ignore_index=True)
    m = truth.merge(recon, on=["order_id", "seller_id"], how="left")

    # 注入类型 → 期望的对账判定集合
    expected = {
        "MATCH":           {"MATCH"},
        "AMOUNT_MISMATCH": {"OVERBILLED", "UNDERBILLED"},
        "DUPLICATE":       {"DUPLICATE"},
        "MISSING_ORDER":   {"MISSING_ORDER"},
        "NOT_DELIVERED":   {"NOT_DELIVERED"},
        "DROP_NOT_BILLED": {"NOT_BILLED"},
    }
    print("\n=== 对账引擎准确率验证（vs 注入 ground-truth）===")
    print(f"{'注入类型':<18}{'样本数':>8}{'判定正确':>10}{'查全率recall':>14}")
    rows = []
    for inj, exp in expected.items():
        sub = m[m["injected"] == inj]
        n = len(sub)
        if n == 0:
            continue
        hit = sub["recon_status"].isin(exp).sum()
        rows.append((inj, n, hit, hit / n))
        print(f"{inj:<18}{n:>8,}{hit:>10,}{hit/n:>13.1%}")
    # 异常整体查全（真异常中被判为非 MATCH 的比例）
    anom = m[m["injected"] != "MATCH"]
    caught = (~anom["recon_status"].isin(["MATCH", "NO_ACTIVITY", None])).sum()
    print(f"{'[异常总体]':<18}{len(anom):>8,}{caught:>10,}{caught/len(anom):>13.1%}")
    return rows


def main():
    con = duckdb.connect()
    print("[1/3] 载入基表 …")
    load_base_tables(con)

    print("[2/3] 执行 SQL：三方对账 + 应收账龄 …")
    run_sql_file(con, "02_three_way_match.sql")
    run_sql_file(con, "03_ar_aging.sql")

    print("[3/3] 导出报表到 output/ …")
    tables = ["recon_summary", "recon_exceptions", "ar_aging_summary",
              "ar_kpi", "bad_debt_candidates", "ar_by_client"]
    dfs = {t: export(con, t) for t in tables}

    # 可选：合并成一个多 sheet 的 Excel
    try:
        with pd.ExcelWriter(OUT / "settlement_report.xlsx", engine="openpyxl") as xw:
            for t in tables:
                dfs[t].head(1000).to_excel(xw, sheet_name=t[:31], index=False)
        xlsx_msg = "settlement_report.xlsx（多 sheet）"
    except Exception as e:
        xlsx_msg = f"[跳过 xlsx：{e}]"

    # ---- 打印关键结果 ----
    print("\n=== 三方对账汇总 recon_summary ===")
    print(dfs["recon_summary"].to_string(index=False))
    print("\n=== 应收账龄 + 坏账计提 ar_aging_summary ===")
    print(dfs["ar_aging_summary"].to_string(index=False))
    print("\n=== 关键指标 ar_kpi（DSO / 加权坏账率）===")
    print(dfs["ar_kpi"].to_string(index=False))
    print(f"\n坏账候选(90+) 笔数 = {len(dfs['bad_debt_candidates']):,}；"
          f"异常清单 recon_exceptions 笔数 = {len(dfs['recon_exceptions']):,}")

    validate_recon(con)

    print(f"\n[完成] 报表已导出到 {OUT}（6 个 CSV + {xlsx_msg}）")
    con.close()


if __name__ == "__main__":
    main()
