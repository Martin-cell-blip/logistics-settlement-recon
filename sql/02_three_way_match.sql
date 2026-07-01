-- ============================================================================
-- 02_three_way_match.sql — 三方对账引擎（Three-way Match）
-- 三方 = ①系统应计运费 SOR（order_items）②送达确认（orders 状态/送达时间）③承运商账单（carrier_bill）
-- 逐笔把承运商账单与系统应计核对，自动分类为：匹配 / 超额 / 少计 / 重复 / 幽灵单 / 计费未送达 / 漏计
-- 依赖 run_pipeline.py 预先载入的基表：raw_order_items, raw_orders, carrier_bill, rate_card
-- 说明：本脚本【不使用】carrier_bill._injected_type（该列是注入真值，仅供事后验证准确率）
-- ============================================================================

-- 第①方 + 第②方：系统应计运费 SOR，并带出送达确认标记
CREATE OR REPLACE VIEW sor AS
SELECT
    oi.order_id,
    oi.seller_id,
    ROUND(SUM(oi.freight_value), 2)                                   AS sor_freight,
    ANY_VALUE(o.order_status)                                         AS order_status,
    (ANY_VALUE(o.order_status) = 'delivered'
        AND ANY_VALUE(o.order_delivered_customer_date) IS NOT NULL)   AS is_delivered
FROM raw_order_items oi
LEFT JOIN raw_orders o USING (order_id)
GROUP BY oi.order_id, oi.seller_id;

-- 第③方：承运商账单按 (order_id, seller_id) 汇总，识别重复计费
CREATE OR REPLACE VIEW bill_grouped AS
SELECT
    order_id,
    seller_id,
    COUNT(*)                          AS bill_line_count,     -- >1 即重复计费
    ROUND(SUM(billed_freight), 2)     AS billed_total,        -- 账单总额（含重复）
    ROUND(MAX(billed_freight), 2)     AS billed_unit,         -- 单笔应有金额
    ANY_VALUE(carrier_id)             AS carrier_id
FROM carrier_bill
GROUP BY order_id, seller_id;

-- 对账主表：账单 ↔ 系统 全外连接，逐条判定
CREATE OR REPLACE TABLE recon AS
WITH tol AS (SELECT MAX(value) AS tol_pct FROM rate_card WHERE rule = 'freight_tolerance_pct')
SELECT
    COALESCE(b.order_id,  s.order_id)   AS order_id,
    COALESCE(b.seller_id, s.seller_id)  AS seller_id,
    b.carrier_id,
    s.sor_freight,
    b.billed_unit,
    b.billed_total,
    b.bill_line_count,
    s.order_status,
    s.is_delivered,
    -- 差异金额（正=多付给承运商，负=少付/漏付）
    ROUND(COALESCE(b.billed_unit, 0) - COALESCE(s.sor_freight, 0), 2)  AS variance_amount,
    CASE
        WHEN s.order_id IS NULL                     THEN 'MISSING_ORDER'    -- 幽灵单：账单有、系统无对应订单
        WHEN b.order_id IS NULL AND s.is_delivered  THEN 'NOT_BILLED'       -- 漏计：系统已送达、账单未收到
        WHEN b.order_id IS NULL                     THEN 'NO_ACTIVITY'      -- 未送达且未计费：无需对账(良性)
        WHEN b.bill_line_count > 1                  THEN 'DUPLICATE'        -- 重复计费
        WHEN NOT COALESCE(s.is_delivered, FALSE)    THEN 'NOT_DELIVERED'    -- 计费但服务未发生（未送达）
        WHEN ABS(b.billed_unit - s.sor_freight) > s.sor_freight * (SELECT tol_pct FROM tol)
             THEN CASE WHEN b.billed_unit > s.sor_freight THEN 'OVERBILLED' ELSE 'UNDERBILLED' END
        ELSE 'MATCH'
    END                                                                     AS recon_status,
    -- 异常处理优先级：金额影响大 + 直接现金损失的排前
    CASE
        WHEN s.order_id IS NULL                        THEN 'P1-幽灵单(拒付)'
        WHEN b.order_id IS NULL AND s.is_delivered     THEN 'P3-漏计(催承运商开票/确认成本)'
        WHEN b.order_id IS NULL                        THEN 'P0-正常'   -- 未送达且未计费(良性)
        WHEN b.bill_line_count > 1                     THEN 'P1-重复计费(拒付重复部分)'
        WHEN NOT COALESCE(s.is_delivered, FALSE)       THEN 'P1-未送达计费(拒付)'
        WHEN ABS(b.billed_unit - s.sor_freight) > s.sor_freight * (SELECT tol_pct FROM tol)
             THEN 'P2-金额差异(复核)'
        ELSE 'P0-正常'
    END                                                                     AS priority
FROM bill_grouped b
FULL OUTER JOIN sor s
    ON b.order_id = s.order_id AND b.seller_id = s.seller_id;

-- 异常处理清单（供结算运营逐条跟进）：只出非正常项，按优先级+金额影响排序
CREATE OR REPLACE TABLE recon_exceptions AS
SELECT
    priority, recon_status, order_id, seller_id, carrier_id,
    sor_freight, billed_unit, billed_total, bill_line_count,
    order_status,
    -- 每条异常的"金额影响"（应追回/应拒付/应确认的金额）
    CASE recon_status
        WHEN 'DUPLICATE'     THEN ROUND(billed_total - billed_unit, 2)   -- 多计的重复部分
        WHEN 'MISSING_ORDER' THEN billed_total                          -- 整笔幽灵计费
        WHEN 'NOT_DELIVERED' THEN billed_total                          -- 整笔未送达计费
        WHEN 'OVERBILLED'    THEN variance_amount                       -- 超额部分
        WHEN 'UNDERBILLED'   THEN variance_amount                       -- 少计部分(负)
        WHEN 'NOT_BILLED'    THEN -sor_freight                          -- 未入账的应付成本
        ELSE 0
    END                                                                 AS impact_amount
FROM recon
WHERE recon_status NOT IN ('MATCH', 'NO_ACTIVITY')
ORDER BY priority, ABS(
    CASE recon_status
        WHEN 'DUPLICATE'     THEN billed_total - billed_unit
        WHEN 'MISSING_ORDER' THEN billed_total
        WHEN 'NOT_DELIVERED' THEN billed_total
        WHEN 'OVERBILLED'    THEN variance_amount
        WHEN 'UNDERBILLED'   THEN variance_amount
        WHEN 'NOT_BILLED'    THEN sor_freight
        ELSE 0 END) DESC;

-- 对账汇总：各状态的笔数、金额与影响
CREATE OR REPLACE TABLE recon_summary AS
SELECT
    recon_status,
    COUNT(*)                                        AS n_records,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_records,
    ROUND(SUM(COALESCE(sor_freight, 0)), 2)         AS total_sor,
    ROUND(SUM(COALESCE(billed_total, 0)), 2)        AS total_billed,
    ROUND(SUM(variance_amount), 2)                  AS total_variance
FROM recon
GROUP BY recon_status
ORDER BY n_records DESC;
