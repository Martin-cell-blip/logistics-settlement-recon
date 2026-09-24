-- ============================================================================
-- 01_setup.sql — 基表载入（参考）
-- 说明：run_pipeline.py 会用绝对路径自动载入这些表；此文件用于「独立用 duckdb CLI 跑」时参考。
-- 用法（在 repo 根目录）：  duckdb :memory: ".read sql/01_setup.sql"  后再 .read 02/03
-- ============================================================================

CREATE OR REPLACE TABLE raw_order_items AS
    SELECT * FROM read_csv_auto('data/raw/olist_order_items_dataset.csv');

CREATE OR REPLACE TABLE raw_orders AS
    SELECT * FROM read_csv_auto('data/raw/olist_orders_dataset.csv');

CREATE OR REPLACE TABLE carrier_bill AS
    SELECT * FROM read_csv_auto('data/generated/carrier_bill.csv');

CREATE OR REPLACE TABLE contract_expectations AS
    SELECT * FROM read_csv_auto('data/generated/contract_expectations.csv');

CREATE OR REPLACE TABLE contract_rate_card AS
    SELECT * FROM read_csv_auto('data/generated/contract_rate_card.csv');

CREATE OR REPLACE TABLE ar_invoices AS
    SELECT * FROM read_csv_auto('data/generated/ar_invoices.csv');

CREATE OR REPLACE TABLE rate_card AS
    SELECT * FROM read_csv_auto('data/generated/rate_card.csv');

CREATE OR REPLACE TABLE ecl_matrix AS
    SELECT * FROM read_csv_auto('data/generated/ecl_matrix.csv');
