"""
audit_agent.py — 对账异常「金额-单据溯源 + 自动复核」Agent
对 output/recon_exceptions.csv 中的每条异常：
  1) 溯源 trace：从承运商账单金额，回溯到系统 order_items 逐笔运费、订单状态/送达时间戳、容差规则，
     拼出完整证据链（audit trail）——即"这笔钱对不对得上单据"。
  2) 复核 review：基于证据链给出 裁定(CONFIRMED/SUSPECT/PASS) + 建议动作(拒付/追回/催开票/人工复核)
     + 依据 + 置信度。默认用内置规则引擎（离线、确定性、覆盖全部异常）。
  3) 可选 LLM 复核：加 --llm 且设置了 ANTHROPIC_API_KEY 时，对 top-N 条调用 Claude 做自然语言研判，
     体现"AI Agent 复核"能力；无密钥则自动跳过，不影响主流程。

输出：output/exception_review.csv（结构化，全部异常）+ output/exception_review.md（人读审计备忘，top-N）
运行：PYTHONUTF8=1 python src/audit_agent.py [--top 25] [--llm]
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GEN = ROOT / "data" / "generated"
OUT = ROOT / "output"


def _p(path: Path) -> str:
    return path.resolve().as_posix()


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"""
        CREATE TABLE raw_order_items AS SELECT * FROM read_csv_auto('{_p(RAW/"olist_order_items_dataset.csv")}');
        CREATE TABLE raw_orders      AS SELECT * FROM read_csv_auto('{_p(RAW/"olist_orders_dataset.csv")}');
        CREATE TABLE carrier_bill    AS SELECT * FROM read_csv_auto('{_p(GEN/"carrier_bill.csv")}');
    """)
    return con


def trace(con: duckdb.DuckDBPyConnection, order_id: str, seller_id: str) -> dict:
    """金额溯源：拼出该 (order_id, seller_id) 的完整证据链。"""
    items = con.execute(
        """SELECT order_item_id, product_id, price, freight_value, shipping_limit_date
           FROM raw_order_items WHERE order_id=? AND seller_id=? ORDER BY order_item_id""",
        [order_id, seller_id]).fetchdf()
    order = con.execute(
        """SELECT order_status, order_purchase_timestamp, order_delivered_carrier_date,
                  order_delivered_customer_date
           FROM raw_orders WHERE order_id=?""", [order_id]).fetchdf()
    bills = con.execute(
        """SELECT bill_line_id, billed_freight, carrier_id, bill_date
           FROM carrier_bill WHERE order_id=? AND seller_id=? ORDER BY bill_line_id""",
        [order_id, seller_id]).fetchdf()
    return {
        "sor_freight": round(float(items["freight_value"].sum()), 2) if len(items) else 0.0,
        "n_items": len(items),
        "items": items,
        "order": order.iloc[0].to_dict() if len(order) else None,
        "bills": bills,
        "billed_total": round(float(bills["billed_freight"].sum()), 2) if len(bills) else 0.0,
        "billed_unit": round(float(bills["billed_freight"].max()), 2) if len(bills) else 0.0,
        "n_bill_lines": len(bills),
    }


# 各异常类型的规则化复核逻辑：返回 (裁定, 建议动作, 置信度, 依据)
def review(recon_status: str, ev: dict) -> tuple[str, str, str, str]:
    sor, billed_u, n_bill = ev["sor_freight"], ev["billed_unit"], ev["n_bill_lines"]
    order = ev["order"]
    status = order["order_status"] if order else "无订单记录"
    if recon_status == "MISSING_ORDER":
        return ("CONFIRMED", "拒付整笔", "高",
                f"账单 order_id 在系统 order_items 与 orders 中均无记录（幽灵计费），应拒付 {ev['billed_total']:.2f}。")
    if recon_status == "NOT_DELIVERED":
        return ("CONFIRMED", "拒付整笔", "高",
                f"订单存在但状态='{status}'、无客户签收时间，服务未发生却计费 {ev['billed_total']:.2f}，应拒付。")
    if recon_status == "DUPLICATE":
        dup = round(ev["billed_total"] - billed_u, 2)
        return ("CONFIRMED", "拒付重复部分", "高",
                f"同(order,seller)出现 {n_bill} 条账单、单笔应为 {billed_u:.2f}，重复计费 {dup:.2f} 应拒付。")
    if recon_status == "OVERBILLED":
        return ("CONFIRMED", "追回差额", "中",
                f"账单 {billed_u:.2f} > 系统应计 {sor:.2f} 且超 ±2% 容差，超额 {billed_u - sor:.2f} 应追回。")
    if recon_status == "UNDERBILLED":
        return ("SUSPECT", "人工复核/确认成本", "中",
                f"账单 {billed_u:.2f} < 系统应计 {sor:.2f}，少计 {sor - billed_u:.2f}；需确认是折扣还是漏计。")
    if recon_status == "NOT_BILLED":
        return ("SUSPECT", "催承运商开票/确认应付", "中",
                f"系统已送达(应计 {sor:.2f})但未收到承运商账单，存在未入账应付成本，应催开票并计提。")
    return ("PASS", "无需处理", "高", "账单与系统一致，在容差内。")


LLM_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def review_with_llm(recon_status: str, ev: dict) -> str | None:
    """可选：调用 Claude 对证据链做自然语言研判。无 SDK 或无密钥则返回 None。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        return None
    order = ev["order"] or {}
    prompt = (
        "你是物流结算对账复核员。基于以下证据链判断该承运商账单是否应支付，"
        "给出：裁定(确认异常/疑似/放行)、建议动作、一句话依据。\n\n"
        f"对账系统初判：{recon_status}\n"
        f"系统应计运费(SOR)={ev['sor_freight']}（{ev['n_items']}个明细）\n"
        f"订单状态={order.get('order_status')}，客户签收时间={order.get('order_delivered_customer_date')}\n"
        f"承运商账单：{ev['n_bill_lines']}条，合计={ev['billed_total']}，单笔={ev['billed_unit']}\n"
    )
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(model=LLM_MODEL, max_tokens=300,
                                     messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text.strip()
    except Exception as e:
        return f"[LLM 调用失败：{e}]"


def memo(row, ev: dict, verdict, action, conf, rationale, llm_text=None) -> str:
    order = ev["order"] or {}
    lines = [
        f"### [{row.priority}] {row.recon_status} — order `{row.order_id[:12]}…` / seller `{row.seller_id[:8]}…`",
        f"- **金额影响**：{row.impact_amount:,.2f}　|　系统应计 SOR={ev['sor_freight']:.2f}　账单合计={ev['billed_total']:.2f}（{ev['n_bill_lines']} 条）",
        f"- **单据溯源**：order_items {ev['n_items']} 条明细；订单状态=`{order.get('order_status')}`；"
        f"承运时间=`{order.get('order_delivered_carrier_date')}`；客户签收=`{order.get('order_delivered_customer_date')}`",
        f"- **复核裁定**：**{verdict}** → 建议：**{action}**（置信度 {conf}）",
        f"- **依据**：{rationale}",
    ]
    if llm_text:
        lines.append(f"- **LLM 研判**：{llm_text}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="生成审计备忘的异常条数(按金额影响)")
    ap.add_argument("--llm", action="store_true", help="对 top 条启用 Claude LLM 复核(需 ANTHROPIC_API_KEY)")
    args = ap.parse_args()

    exc = pd.read_csv(OUT / "recon_exceptions.csv")
    if exc.empty:
        print("无异常记录。请先运行 run_pipeline.py。")
        return
    exc = exc.reindex(exc["impact_amount"].abs().sort_values(ascending=False).index).reset_index(drop=True)

    con = connect()
    records, memos = [], []
    llm_used = 0
    for i, row in enumerate(exc.itertuples(index=False)):
        ev = trace(con, row.order_id, row.seller_id)
        verdict, action, conf, rationale = review(row.recon_status, ev)
        llm_text = None
        if args.llm and i < args.top:
            llm_text = review_with_llm(row.recon_status, ev)
            if llm_text:
                llm_used += 1
        records.append({
            "priority": row.priority, "recon_status": row.recon_status,
            "order_id": row.order_id, "seller_id": row.seller_id,
            "sor_freight": ev["sor_freight"], "billed_total": ev["billed_total"],
            "impact_amount": row.impact_amount,
            "verdict": verdict, "recommended_action": action, "confidence": conf,
            "rationale": rationale,
        })
        if i < args.top:
            memos.append(memo(row, ev, verdict, action, conf, rationale, llm_text))
    con.close()

    rev = pd.DataFrame(records)
    rev.to_csv(OUT / "exception_review.csv", index=False)

    # 人读审计备忘
    head = [
        "# 对账异常自动复核报告（金额-单据溯源 + Agent 复核）",
        f"> 共复核异常 {len(rev):,} 条；下列为金额影响 Top {min(args.top, len(rev))} 的审计备忘。"
        + ("　（含 LLM 研判）" if llm_used else "　（规则引擎复核；加 --llm 且配置 ANTHROPIC_API_KEY 可启用 Claude 研判）"),
        "",
        "## 复核汇总",
        rev.groupby(["verdict", "recommended_action"]).agg(
            笔数=("order_id", "count"), 金额影响合计=("impact_amount", "sum")
        ).round(2).to_markdown(),
        "",
        "## Top 异常审计备忘",
        "",
    ]
    (OUT / "exception_review.md").write_text("\n".join(head) + "\n\n".join(memos) + "\n", encoding="utf-8")

    print(f"[完成] 复核 {len(rev):,} 条异常 → output/exception_review.csv / .md（备忘 Top {args.top}）")
    print("\n=== 复核汇总（裁定 × 建议动作）===")
    summary = rev.groupby(["verdict", "recommended_action"]).agg(
        n=("order_id", "count"), impact=("impact_amount", "sum")).round(2)
    print(summary.to_string())
    recover = rev.loc[rev["recommended_action"].isin(["追回差额", "拒付整笔", "拒付重复部分"]), "impact_amount"].abs().sum()
    print(f"\n可拒付/追回金额合计 ≈ {recover:,.2f}")
    if args.llm:
        print(f"LLM 研判条数：{llm_used}" + ("" if llm_used else "（未启用：缺 ANTHROPIC_API_KEY 或 anthropic SDK）"))


if __name__ == "__main__":
    main()
