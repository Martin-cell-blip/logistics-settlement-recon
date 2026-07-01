-- ============================================================================
-- 03_ar_aging.sql — 应收账款账龄分析 + DSO + 坏账(ECL)计提
-- 依赖 run_pipeline.py 预先载入的基表：ar_invoices, ecl_matrix
-- 账龄五桶：Current(未到期) / 1-30 / 31-60 / 61-90 / 90+（逾期天数）
-- 坏账计提：按账龄桶套用 ECL 率矩阵（率值参照京东物流 2024 年报附注，见行研报告 [44]）
-- ============================================================================

-- 快照日 = 台账中最新发票月（使最后一期为 Current，早期未回款自然老化）
CREATE OR REPLACE VIEW ar_params AS
SELECT MAX(invoice_date) AS as_of_date FROM ar_invoices;

-- 快照日的未结应收（open items）：未回款，或回款日晚于快照
CREATE OR REPLACE TABLE ar_aging AS
WITH p AS (SELECT as_of_date FROM ar_params)
SELECT
    i.invoice_id,
    i.client_id,
    i.client_state,
    i.invoice_date,
    i.due_date,
    i.credit_terms_days,
    i.invoice_amount,
    i.paid_date,
    (SELECT as_of_date FROM p)                                   AS as_of_date,
    DATE_DIFF('day', i.due_date, (SELECT as_of_date FROM p))     AS days_past_due,
    CASE
        WHEN (SELECT as_of_date FROM p) < i.due_date               THEN 'Current'
        WHEN DATE_DIFF('day', i.due_date, (SELECT as_of_date FROM p)) <= 30 THEN '1-30'
        WHEN DATE_DIFF('day', i.due_date, (SELECT as_of_date FROM p)) <= 60 THEN '31-60'
        WHEN DATE_DIFF('day', i.due_date, (SELECT as_of_date FROM p)) <= 90 THEN '61-90'
        ELSE '90+'
    END                                                          AS aging_bucket
FROM ar_invoices i, p
WHERE (i.paid_date IS NULL OR i.paid_date > (SELECT as_of_date FROM p))       -- 快照时点仍未回款
  AND (i.writeoff_date IS NULL OR i.writeoff_date > (SELECT as_of_date FROM p)); -- 且尚未核销出账 = 未结应收

-- 账龄汇总（含 ECL 坏账计提）：按桶给出未结金额、占比、计提率、计提额
CREATE OR REPLACE TABLE ar_aging_summary AS
WITH bucketed AS (
    SELECT aging_bucket,
           COUNT(*)                    AS n_invoices,
           ROUND(SUM(invoice_amount),2) AS open_amount
    FROM ar_aging
    GROUP BY aging_bucket
)
SELECT
    b.aging_bucket,
    b.n_invoices,
    b.open_amount,
    ROUND(100.0 * b.open_amount / SUM(b.open_amount) OVER (), 2)   AS pct_of_ar,
    m.ecl_rate,
    ROUND(b.open_amount * m.ecl_rate, 2)                           AS ecl_provision
FROM bucketed b
LEFT JOIN ecl_matrix m USING (aging_bucket)
ORDER BY CASE b.aging_bucket
            WHEN 'Current' THEN 0 WHEN '1-30' THEN 1 WHEN '31-60' THEN 2
            WHEN '61-90' THEN 3 ELSE 4 END;

-- 关键指标：总应收、加权坏账率、DSO
-- DSO = 期末应收 / 近 365 天开票额 × 365
CREATE OR REPLACE TABLE ar_kpi AS
WITH ar AS (SELECT SUM(open_amount) AS total_ar, SUM(ecl_provision) AS total_ecl
            FROM ar_aging_summary),
sales AS (
    SELECT SUM(invoice_amount) AS credit_sales_365
    FROM ar_invoices, ar_params
    WHERE invoice_date > (SELECT as_of_date FROM ar_params) - INTERVAL 365 DAY
),
wo AS (   -- 近 365 天已核销(实现)坏账
    SELECT COALESCE(SUM(invoice_amount), 0) AS written_off_365
    FROM ar_invoices, ar_params
    WHERE writeoff_date IS NOT NULL
      AND writeoff_date <= (SELECT as_of_date FROM ar_params)
      AND writeoff_date > (SELECT as_of_date FROM ar_params) - INTERVAL 365 DAY
)
SELECT
    ROUND(ar.total_ar, 2)                                             AS total_open_ar,
    ROUND(ar.total_ecl, 2)                                            AS total_ecl_provision,
    ROUND(100.0 * ar.total_ecl / NULLIF(ar.total_ar, 0), 2)          AS weighted_ecl_rate_pct,
    ROUND(sales.credit_sales_365, 2)                                 AS credit_sales_365d,
    ROUND(ar.total_ar / NULLIF(sales.credit_sales_365, 0) * 365, 1)  AS dso_days,
    ROUND(wo.written_off_365, 2)                                     AS written_off_365d
FROM ar, sales, wo;

-- 坏账候选清单（90+ 未结），按金额倒序 —— 结算运营催收/计提重点
CREATE OR REPLACE TABLE bad_debt_candidates AS
SELECT invoice_id, client_id, client_state, invoice_date, due_date,
       invoice_amount, days_past_due
FROM ar_aging
WHERE aging_bucket = '90+'
ORDER BY invoice_amount DESC;

-- 按客户的应收与账龄暴露（Top 客户，供授信/账期治理）
CREATE OR REPLACE TABLE ar_by_client AS
SELECT
    client_id, client_state,
    ROUND(SUM(invoice_amount), 2)                                          AS open_ar,
    COUNT(*)                                                               AS open_invoices,
    ROUND(SUM(CASE WHEN aging_bucket = '90+' THEN invoice_amount ELSE 0 END), 2) AS ar_90plus,
    ROUND(100.0 * SUM(CASE WHEN aging_bucket = '90+' THEN invoice_amount ELSE 0 END)
          / NULLIF(SUM(invoice_amount), 0), 1)                             AS pct_90plus
FROM ar_aging
GROUP BY client_id, client_state
ORDER BY open_ar DESC;
