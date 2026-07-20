"""
generate_data.py — 从 Olist 真实电商数据派生"结算对账"场景所需的两份合成数据：
  1) 承运商账单 carrier_bill.csv      —— 在系统应计运费(SOR)基础上【注入已知差异】，用于三方对账
  2) 应收账款台账 ar_invoices.csv     —— 把每个卖家当作 B2B 客户按月开票并模拟回款，用于账龄/DSO/坏账

设计要点：
- SOR（System of Record，系统应计运费）来自 Olist order_items 的 freight_value，视为"平台系统认为应付承运商的运费"，是对账真值。
- 承运商账单在 SOR 上注入 5 类差异并保留隐藏列 _injected_type 作为 ground-truth（对账 SQL 不得使用该列），
  从而可事后计算对账引擎的 precision/recall，证明"异常识别覆盖率"。
- 全流程固定随机种子 (RNG_SEED)，可复现。金额单位沿用 Olist 原始币种（示意，可视作"元"）。
"""
from __future__ import annotations
import hashlib
import uuid
from pathlib import Path
import numpy as np
import pandas as pd

RNG_SEED = 42
rng = np.random.default_rng(RNG_SEED)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
GEN = ROOT / "data" / "generated"
GEN.mkdir(parents=True, exist_ok=True)

COMMISSION_RATE = 0.12          # 平台向卖家收取的佣金率（用于 AR 发票金额）
TOLERANCE_PCT = 0.02            # 对账容差：账单与 SOR 相差 ±2% 以内视为匹配（写入 rate_card）


STATE_REGION = {
    "AC": "N", "AP": "N", "AM": "N", "PA": "N", "RO": "N", "RR": "N", "TO": "N",
    "AL": "NE", "BA": "NE", "CE": "NE", "MA": "NE", "PB": "NE", "PE": "NE",
    "PI": "NE", "RN": "NE", "SE": "NE",
    "DF": "CO", "GO": "CO", "MT": "CO", "MS": "CO",
    "ES": "SE", "MG": "SE", "RJ": "SE", "SP": "SE",
    "PR": "S", "RS": "S", "SC": "S",
}


def build_contract_rate_card() -> pd.DataFrame:
    """Versioned synthetic carrier contract used as deterministic pricing truth."""
    rows = []
    zone_factor = {"LOCAL": 1.0, "REGIONAL": 1.25, "NATIONAL": 1.65}
    bands = [
        ("W1", 0.0, 1.0, 5.2, 1.4),
        ("W2", 1.0, 5.0, 7.5, 1.15),
        ("W3", 5.0, 9999.0, 11.0, 0.95),
    ]
    for carrier_no in range(1, 9):
        carrier_id = f"CR{carrier_no:02d}"
        carrier_factor = 0.94 + carrier_no * 0.015
        for zone, factor in zone_factor.items():
            for band_id, lower, upper, base_fee, per_kg in bands:
                rows.append(
                    {
                        "rate_card_version": "contract-2018-v1",
                        "contract_clause_id": f"{carrier_id}-{zone}-{band_id}",
                        "carrier_id": carrier_id,
                        "service_zone": zone,
                        "weight_band": band_id,
                        "min_weight_kg": lower,
                        "max_weight_kg": upper,
                        "base_fee": round(base_fee * factor * carrier_factor, 4),
                        "per_kg_fee": round(per_kg * factor * carrier_factor, 4),
                        "fuel_surcharge_pct": 0.08,
                        "valid_from": "2017-01-01",
                        "valid_to": "2018-12-31",
                    }
                )
    return pd.DataFrame(rows)


def _stable_carrier(order_id: str, seller_id: str) -> str:
    digest = hashlib.sha256(f"{order_id}|{seller_id}".encode()).digest()
    return f"CR{digest[0] % 8 + 1:02d}"


def load_olist() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build source ledger plus contract expectations at order/seller grain."""
    items = pd.read_csv(RAW / "olist_order_items_dataset.csv")
    orders = pd.read_csv(
        RAW / "olist_orders_dataset.csv",
        parse_dates=["order_purchase_timestamp", "order_delivered_customer_date",
                     "order_delivered_carrier_date"],
    )
    sellers = pd.read_csv(RAW / "olist_sellers_dataset.csv")
    customers = pd.read_csv(RAW / "olist_customers_dataset.csv")
    products = pd.read_csv(RAW / "olist_products_dataset.csv")

    sor = (items.groupby(["order_id", "seller_id"], as_index=False)
                .agg(sor_freight=("freight_value", "sum"),
                     gmv=("price", "sum"),
                     n_items=("order_item_id", "count")))
    weighted_items = items.merge(
        products[["product_id", "product_weight_g"]], on="product_id", how="left"
    )
    weights = (
        weighted_items.groupby(["order_id", "seller_id"], as_index=False)
        .agg(product_weight_g=("product_weight_g", "sum"))
    )
    sor = sor.merge(weights, on=["order_id", "seller_id"], how="left")
    sor = sor.merge(
        orders[["order_id", "customer_id", "order_status", "order_purchase_timestamp",
                "order_delivered_customer_date"]],
        on="order_id", how="left")
    sor = sor.merge(sellers[["seller_id", "seller_state"]], on="seller_id", how="left")
    sor = sor.merge(
        customers[["customer_id", "customer_state"]], on="customer_id", how="left"
    )
    sor["sor_freight"] = sor["sor_freight"].round(2)
    sor["chargeable_weight_kg"] = (
        sor["product_weight_g"].fillna(1000).clip(lower=100) / 1000
    ).clip(lower=0.1).round(3)
    sor["carrier_id"] = [
        _stable_carrier(order_id, seller_id)
        for order_id, seller_id in zip(sor["order_id"], sor["seller_id"])
    ]
    same_state = sor["seller_state"] == sor["customer_state"]
    same_region = sor["seller_state"].map(STATE_REGION) == sor["customer_state"].map(STATE_REGION)
    sor["service_zone"] = np.select(
        [same_state, same_region], ["LOCAL", "REGIONAL"], default="NATIONAL"
    )
    sor["weight_band"] = pd.cut(
        sor["chargeable_weight_kg"],
        bins=[-np.inf, 1.0, 5.0, np.inf],
        labels=["W1", "W2", "W3"],
        right=False,
    ).astype(str)
    rate_card = build_contract_rate_card()
    expectations = sor.merge(
        rate_card,
        on=["carrier_id", "service_zone", "weight_band"],
        how="left",
        validate="many_to_one",
    )
    if expectations["contract_clause_id"].isna().any():
        raise ValueError("存在未匹配到合同费率卡的订单")
    expectations["contract_expected_freight"] = (
        (
            expectations["base_fee"]
            + expectations["per_kg_fee"] * expectations["chargeable_weight_kg"]
        )
        * (1 + expectations["fuel_surcharge_pct"])
    ).round(2)
    contract_cols = [
        "order_id", "seller_id", "carrier_id", "service_zone",
        "chargeable_weight_kg", "weight_band", "rate_card_version",
        "contract_clause_id", "contract_expected_freight",
        "sor_freight", "order_status", "order_delivered_customer_date",
    ]
    return sor, orders, expectations[contract_cols], rate_card


def build_carrier_bill(expectations: pd.DataFrame) -> pd.DataFrame:
    """Inject six auditable exception types around contract-expected freight."""
    delivered = expectations[expectations["order_status"] == "delivered"].copy()
    nondelivered = expectations[expectations["order_status"] != "delivered"].copy()

    n = len(delivered)
    # 从 delivered 中划分类别：drop=漏计(不进账单), 其余进账单
    # 概率：正常匹配 0.86 / 金额错配 0.06 / 重复 0.03 / 漏计(NOT_BILLED) 0.03 / (剩余给未送达+幽灵单在后面补)
    cat = rng.choice(
        ["MATCH", "AMOUNT_MISMATCH", "DUPLICATE", "DROP"],
        size=n, p=[0.88, 0.06, 0.03, 0.03])
    delivered = delivered.assign(disc_cat=cat)

    rows = []

    def new_line(order_id, seller_id, carrier_id, billed, injected):
        rows.append({
            "bill_line_id": f"BL{len(rows):07d}",
            "order_id": order_id,
            "seller_id": seller_id,
            "billed_freight": round(float(billed), 2),
            "carrier_id": carrier_id,
            "bill_date": pd.Timestamp("2018-10-31"),
            "_injected_type": injected,       # 隐藏真值列，对账 SQL 不得使用
        })

    for r in delivered.itertuples(index=False):
        c = r.disc_cat
        if c == "DROP":
            continue  # 漏计：SOR 有、账单无 → 期望被识别为 NOT_BILLED
        if c == "MATCH":
            # 允许极小舍入噪声（仍在容差内）
            noise = rng.choice([0.0, 0.01, -0.01], p=[0.8, 0.1, 0.1])
            new_line(
                r.order_id, r.seller_id, r.carrier_id,
                r.contract_expected_freight + noise, "MATCH"
            )
        elif c == "AMOUNT_MISMATCH":
            if rng.random() < 0.6:
                factor = rng.uniform(1.10, 1.45)   # 超额计费（承运商多收）
            else:
                factor = rng.uniform(0.55, 0.90)   # 少计费
            new_line(
                r.order_id, r.seller_id, r.carrier_id,
                r.contract_expected_freight * factor, "AMOUNT_MISMATCH"
            )
        elif c == "DUPLICATE":
            new_line(
                r.order_id, r.seller_id, r.carrier_id,
                r.contract_expected_freight, "DUPLICATE"
            )
            new_line(
                r.order_id, r.seller_id, r.carrier_id,
                r.contract_expected_freight, "DUPLICATE"
            )

    # 未送达却计费：从非送达订单抽样若干，账单金额正常但服务未发生
    nd_sample = nondelivered.sample(n=min(400, len(nondelivered)), random_state=RNG_SEED)
    for r in nd_sample.itertuples(index=False):
        new_line(
            r.order_id, r.seller_id, r.carrier_id,
            r.contract_expected_freight, "NOT_DELIVERED"
        )

    # 幽灵单：伪造 SOR 中不存在的 order_id
    real_sellers = expectations["seller_id"].drop_duplicates().sample(
        n=min(1200, expectations["seller_id"].nunique()),
        random_state=RNG_SEED,
    ).tolist()
    for _ in range(1200):
        fake_order = uuid.uuid4().hex
        seller = real_sellers[int(rng.integers(0, len(real_sellers)))]
        new_line(
            fake_order, seller, f"CR{int(rng.integers(1, 9)):02d}",
            rng.uniform(5, 60), "MISSING_ORDER"
        )

    bill = pd.DataFrame(rows)
    bill = bill.sample(frac=1.0, random_state=RNG_SEED).reset_index(drop=True)  # 打散顺序
    return bill


def build_ar_ledger(sor: pd.DataFrame) -> pd.DataFrame:
    """把每个卖家视为 B2B 客户，按【月】聚合开票（运费+佣金），并模拟回款行为。"""
    d = sor[sor["order_status"] == "delivered"].dropna(subset=["order_purchase_timestamp"]).copy()
    d["ym"] = d["order_purchase_timestamp"].dt.to_period("M").dt.to_timestamp()

    inv = (d.groupby(["seller_id", "seller_state", "ym"], as_index=False)
             .agg(freight=("sor_freight", "sum"), gmv=("gmv", "sum"), n_orders=("order_id", "count")))
    inv["invoice_amount"] = (inv["freight"] + inv["gmv"] * COMMISSION_RATE).round(2)
    # 发票日 = 次月首日（该月账单月结）
    inv["invoice_date"] = (inv["ym"] + pd.offsets.MonthBegin(1))

    # 客户分层 → 授信账期：按卖家总开票额划分（大客户账期更长，模拟议价力）
    tot = inv.groupby("seller_id")["invoice_amount"].transform("sum")
    q1, q2 = tot.quantile(0.5), tot.quantile(0.9)
    terms = np.where(tot >= q2, 50, np.where(tot >= q1, 35, 20))
    inv["credit_terms_days"] = terms
    inv["due_date"] = inv["invoice_date"] + pd.to_timedelta(inv["credit_terms_days"], unit="D")

    # 模拟回款（健康账簿）：绝大多数按期回款；少量逾期后仍回款；极少数违约→逾期 WRITEOFF_DAYS 天后核销出账。
    # 三个可调旋钮（校准到 DSO≈40-50 / 90+ 占个位数 / 加权ECL 个位数，对标京东物流 DSO≈31、ECL 1.8%）：
    ONTIME_P, LATE_P, DEFAULT_P = 0.90, 0.085, 0.015   # 行为占比
    WRITEOFF_DAYS = 150                                # 逾期满 150 天核销出账（此后不再计入未结应收）
    m = len(inv)
    beh = rng.choice(["ONTIME", "LATE", "DEFAULT"], size=m, p=[ONTIME_P, LATE_P, DEFAULT_P])
    delay = np.zeros(m)
    ontime = beh == "ONTIME"
    late = beh == "LATE"
    delay[ontime] = rng.normal(-5, 5, ontime.sum())          # 按期：多在到期前后小幅波动
    delay[late] = rng.uniform(5, 55, late.sum())             # 逾期：5~55 天后回款（落入 1-60 桶）
    paid = inv["due_date"] + pd.to_timedelta(delay.round(), unit="D")
    paid = paid.where(beh != "DEFAULT", pd.NaT)              # 违约 → 无回款日
    inv["paid_date"] = paid
    # 违约发票在逾期 WRITEOFF_DAYS 天后核销（写出账簿）；非违约无核销日
    writeoff = pd.Series(pd.NaT, index=inv.index, dtype="datetime64[ns]")
    writeoff[beh == "DEFAULT"] = inv.loc[beh == "DEFAULT", "due_date"] + pd.Timedelta(days=WRITEOFF_DAYS)
    inv["writeoff_date"] = writeoff
    inv["invoice_id"] = ["INV" + str(i).zfill(6) for i in range(1, m + 1)]

    out = inv[["invoice_id", "seller_id", "seller_state", "invoice_date",
               "credit_terms_days", "due_date", "invoice_amount", "paid_date", "writeoff_date"]]
    out = out.rename(columns={"seller_id": "client_id", "seller_state": "client_state"})
    return out.sort_values("invoice_date").reset_index(drop=True)


def write_policy_files(contract_rate_card: pd.DataFrame):
    """写出对账规则与坏账 ECL 账龄率矩阵（ECL 率参照京东物流 2024 年报附注披露，见行研报告 [44]）。"""
    pd.DataFrame([{"rule": "freight_tolerance_pct", "value": TOLERANCE_PCT,
                   "note": "账单与合同预期金额相差在此比例内视为匹配"},
                  {"rule": "commission_rate", "value": COMMISSION_RATE,
                   "note": "平台佣金率(AR发票口径)"}]).to_csv(GEN / "rate_card.csv", index=False)
    # ECL 账龄率矩阵：参照京东物流 2024 年报（0.9%/15.6%/44.2%/100%），当期沿用最短档
    pd.DataFrame([
        {"aging_bucket": "Current", "ecl_rate": 0.009},
        {"aging_bucket": "1-30",    "ecl_rate": 0.009},
        {"aging_bucket": "31-60",   "ecl_rate": 0.156},
        {"aging_bucket": "61-90",   "ecl_rate": 0.442},
        {"aging_bucket": "90+",     "ecl_rate": 1.000},
    ]).to_csv(GEN / "ecl_matrix.csv", index=False)
    contract_rate_card.to_csv(GEN / "contract_rate_card.csv", index=False)


def main():
    sor, _, expectations, contract_rate_card = load_olist()
    print(f"[SOR] (order_id,seller_id) 组数 = {len(sor):,}；其中已送达 = {(sor.order_status=='delivered').sum():,}")

    expectations.to_csv(GEN / "contract_expectations.csv", index=False)
    bill = build_carrier_bill(expectations)
    bill_out = bill.drop(columns=[])  # 保留 _injected_type（隐藏真值）
    bill_out.to_csv(GEN / "carrier_bill.csv", index=False)
    print(f"[承运商账单] 行数 = {len(bill):,}")
    print(bill["_injected_type"].value_counts().to_string())

    ar = build_ar_ledger(sor)
    ar.to_csv(GEN / "ar_invoices.csv", index=False)
    unpaid = ar["paid_date"].isna().sum()
    print(f"[AR台账] 发票数 = {len(ar):,}；未回款(NaT) = {unpaid:,}；开票总额 = {ar['invoice_amount'].sum():,.0f}")

    write_policy_files(contract_rate_card)
    print("[完成] 已写出合同费率卡、逐单合同预期、承运商账单、AR台账与规则文件。")


if __name__ == "__main__":
    main()
