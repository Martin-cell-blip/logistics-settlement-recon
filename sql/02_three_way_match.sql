-- 02_three_way_match.sql — 合同预期 × 履约证据 × 承运商账单
-- 判定只使用合同费率计算结果、订单履约状态和账单，不读取 _injected_type。

CREATE OR REPLACE VIEW contract_service AS
SELECT
    ce.order_id,
    ce.seller_id,
    ce.carrier_id,
    ce.service_zone,
    ce.chargeable_weight_kg,
    ce.rate_card_version,
    ce.contract_clause_id,
    ce.contract_expected_freight,
    ce.sor_freight AS source_freight,
    o.order_status,
    (o.order_status = 'delivered'
        AND o.order_delivered_customer_date IS NOT NULL) AS is_delivered
FROM contract_expectations ce
LEFT JOIN raw_orders o USING (order_id);

CREATE OR REPLACE VIEW bill_grouped AS
SELECT
    order_id,
    seller_id,
    COUNT(*) AS bill_line_count,
    ROUND(SUM(billed_freight), 2) AS billed_total,
    ROUND(MAX(billed_freight), 2) AS billed_unit,
    ANY_VALUE(carrier_id) AS carrier_id
FROM carrier_bill
GROUP BY order_id, seller_id;

CREATE OR REPLACE TABLE recon AS
WITH tol AS (
    SELECT MAX(value) AS tol_pct
    FROM rate_card
    WHERE rule = 'freight_tolerance_pct'
)
SELECT
    COALESCE(b.order_id, c.order_id) AS order_id,
    COALESCE(b.seller_id, c.seller_id) AS seller_id,
    COALESCE(b.carrier_id, c.carrier_id) AS carrier_id,
    c.service_zone,
    c.chargeable_weight_kg,
    c.rate_card_version,
    c.contract_clause_id,
    c.contract_expected_freight,
    c.source_freight,
    b.billed_unit,
    b.billed_total,
    b.bill_line_count,
    c.order_status,
    c.is_delivered,
    ROUND(
        COALESCE(b.billed_unit, 0) - COALESCE(c.contract_expected_freight, 0), 2
    ) AS variance_amount,
    CASE
        WHEN c.order_id IS NULL THEN 'MISSING_ORDER'
        WHEN b.order_id IS NULL AND c.is_delivered THEN 'NOT_BILLED'
        WHEN b.order_id IS NULL THEN 'NO_ACTIVITY'
        WHEN b.bill_line_count > 1 THEN 'DUPLICATE'
        WHEN NOT COALESCE(c.is_delivered, FALSE) THEN 'NOT_DELIVERED'
        WHEN ABS(b.billed_unit - c.contract_expected_freight)
             > c.contract_expected_freight * (SELECT tol_pct FROM tol)
            THEN CASE
                WHEN b.billed_unit > c.contract_expected_freight
                THEN 'OVERBILLED' ELSE 'UNDERBILLED'
            END
        ELSE 'MATCH'
    END AS recon_status,
    CASE
        WHEN c.order_id IS NULL THEN 'P1-幽灵单(拒付)'
        WHEN b.order_id IS NULL AND c.is_delivered THEN 'P3-漏计(确认成本)'
        WHEN b.order_id IS NULL THEN 'P0-正常'
        WHEN b.bill_line_count > 1 THEN 'P1-重复计费(拒付重复部分)'
        WHEN NOT COALESCE(c.is_delivered, FALSE) THEN 'P1-未送达计费(拒付)'
        WHEN ABS(b.billed_unit - c.contract_expected_freight)
             > c.contract_expected_freight * (SELECT tol_pct FROM tol)
            THEN 'P2-合同金额差异(复核)'
        ELSE 'P0-正常'
    END AS priority
FROM bill_grouped b
FULL OUTER JOIN contract_service c
    ON b.order_id = c.order_id AND b.seller_id = c.seller_id;

CREATE OR REPLACE TABLE recon_exceptions AS
SELECT
    priority, recon_status, order_id, seller_id, carrier_id,
    service_zone, chargeable_weight_kg, rate_card_version, contract_clause_id,
    contract_expected_freight, source_freight,
    billed_unit, billed_total, bill_line_count, order_status,
    CASE recon_status
        WHEN 'DUPLICATE' THEN ROUND(billed_total - billed_unit, 2)
        WHEN 'MISSING_ORDER' THEN billed_total
        WHEN 'NOT_DELIVERED' THEN billed_total
        WHEN 'OVERBILLED' THEN variance_amount
        WHEN 'UNDERBILLED' THEN variance_amount
        WHEN 'NOT_BILLED' THEN -contract_expected_freight
        ELSE 0
    END AS impact_amount
FROM recon
WHERE recon_status NOT IN ('MATCH', 'NO_ACTIVITY')
ORDER BY priority, ABS(
    CASE recon_status
        WHEN 'DUPLICATE' THEN billed_total - billed_unit
        WHEN 'MISSING_ORDER' THEN billed_total
        WHEN 'NOT_DELIVERED' THEN billed_total
        WHEN 'OVERBILLED' THEN variance_amount
        WHEN 'UNDERBILLED' THEN variance_amount
        WHEN 'NOT_BILLED' THEN contract_expected_freight
        ELSE 0
    END
) DESC;

CREATE OR REPLACE TABLE recon_summary AS
SELECT
    recon_status,
    COUNT(*) AS n_records,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_records,
    ROUND(SUM(COALESCE(contract_expected_freight, 0)), 2) AS total_contract_expected,
    ROUND(SUM(COALESCE(billed_total, 0)), 2) AS total_billed,
    ROUND(SUM(variance_amount), 2) AS total_variance
FROM recon
GROUP BY recon_status
ORDER BY n_records DESC;
