# 电商物流结算自动对账 + 应收账款账龄/坏账预警工具
**Logistics Settlement Reconciliation & AR-Aging / Bad-Debt Monitoring**

一个端到端的**财务结算运营**工具：在真实电商物流数据上，用 **SQL(DuckDB) + Python** 实现
①承运商账单**三方自动对账**（识别金额错配/重复/幽灵单/未送达计费/漏计）
②**应收账款账龄分析**（五桶 + DSO）
③**坏账(ECL)计提**（账龄率矩阵）
并用注入的 ground-truth 验证对账引擎**异常识别查全率 ≈ 99.9%**。

![一条命令跑出对账异常与验证](docs/demo.svg)

> 📊 **可视化报告**：[docs/settlement_dashboard.html](docs/settlement_dashboard.html)（KPI 卡片 + 对账分布 + 账龄柱状 + Top 异常，可打印 PDF）

> 面向岗位：京东物流「财务结算运营（Bill Reconciliation & Settlement）」实习。项目职责一一对应对账、异常处理、应收账龄、坏账监控、结算自动化、数据分析报表。

---

## 为什么这么设计
真实数据里没有"带错误的承运商账单"。本项目用 **Olist 巴西电商公开数据集**（10 万+ 订单，含
`freight_value` 运费、`seller_id`、`order_status`、送达时间戳）作为**系统应计运费(SOR)**这一"真值"，
再据此**合成一份承运商账单并注入 5 类已知差异**——因此对账引擎抓到的每一条异常都能与注入真值比对，
量化"异常识别覆盖率"。应收账龄则把每个卖家视为 **B2B 客户**按月开票并模拟回款。

数据集：[Olist (Kaggle)](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)　对账分类逻辑参考 [Enterprise Finance Reconciliation Tool](https://github.com/MuhannadRVD/-Enterprise-Finance-Reconciliation-Tool-Python-Automation)。

## 目录结构
```
logistics-settlement-recon/
├── src/
│   ├── fetch_olist.py       # ① 拉取 Olist 原始 CSV → data/raw/
│   ├── generate_data.py     # ② 由 SOR 合成承运商账单(注入差异) + AR 台账 → data/generated/
│   ├── run_pipeline.py      # ③ 载入 DuckDB → 跑 SQL → 导出报表 → 验证查全率
│   └── audit_agent.py       # ④ 异常"金额-单据溯源 + 自动复核"Agent(规则引擎，可选 LLM)
├── sql/
│   ├── 01_setup.sql             # 基表载入(独立跑参考)
│   ├── 02_three_way_match.sql   # ③ 三方对账引擎
│   └── 03_ar_aging.sql          # ③ 账龄 + DSO + 坏账 ECL 计提
├── data/{raw,generated}/    # 数据(不入库，脚本可重建)
├── output/                  # 报表产物(CSV + settlement_report.xlsx)
├── requirements.txt
└── README.md
```

## 一键运行
```bash
pip install -r requirements.txt
python src/fetch_olist.py        # 拉 Olist 数据(~46MB)
python src/generate_data.py      # 生成承运商账单 + AR 台账(固定随机种子，可复现)
python src/run_pipeline.py       # 跑对账 + 账龄，导出 output/ 并打印验证
python src/audit_agent.py --top 25   # 异常金额-单据溯源 + 自动复核(加 --llm 且配 ANTHROPIC_API_KEY 启用 Claude 研判)
```

## 三方对账引擎（`sql/02_three_way_match.sql`）
三方 = ①系统应计运费 SOR　②送达确认（订单状态/送达时间）　③承运商账单。逐笔全外连接后判定：

| 判定 | 含义 | 处理优先级 |
|---|---|---|
| `MATCH` | 账单与系统一致（±2% 容差内） | P0 |
| `OVERBILLED` / `UNDERBILLED` | 金额超出容差 | P2 复核 |
| `DUPLICATE` | 同单重复计费 | P1 拒付重复部分 |
| `MISSING_ORDER` | 幽灵单：账单有、系统无对应订单 | P1 拒付 |
| `NOT_DELIVERED` | 计费但服务未发生（未送达） | P1 拒付 |
| `NOT_BILLED` | 漏计：系统已送达、账单未收到 | P3 催承运商开票/确认成本 |

**实测结果（Olist 全量，99,485 条账单）：**
```
recon_status   n_records  pct   total_variance
MATCH             86,050  85%          −2.76
OVERBILLED         3,493  3.4%    +21,536   ← 多付给承运商，应追回
DUPLICATE          2,989  3.0%    (重复计费 ~68,872 应拒付)
NOT_BILLED         2,922  2.9%    −65,281   ← 已送达未入账的应付成本
UNDERBILLED        2,357  2.3%    −14,730
MISSING_ORDER      1,200  1.2%    +38,659   ← 幽灵计费，应拒付
NOT_DELIVERED        407  0.4%    (计费未送达，应拒付)
```
**对账引擎验证查全率（对比注入 ground-truth）：DUPLICATE / MISSING_ORDER / NOT_DELIVERED / NOT_BILLED = 100%，金额错配 99.7%，异常总体 99.9%。**

## 应收账款账龄 + DSO + 坏账（`sql/03_ar_aging.sql`）
以最新发票月为快照，未结应收按逾期天数分五桶，套用 **ECL 账龄率矩阵**计提坏账。
> ECL 率（0.9% / 15.6% / 44.2% / 100%）**参照京东物流 2024 年报附注披露**——与本人所做行研报告互相印证。

产出：`ar_aging_summary`（五桶金额/占比/计提）、`ar_kpi`（DSO、加权坏账率）、
`bad_debt_candidates`（90+ 逾期催收清单）、`ar_by_client`（按客户应收暴露）。

**实测结果（校准为健康账簿）：**
```
账龄桶     占比      ECL率     计提
Current  89.4%    0.9%
1-30      5.9%    0.9%
31-60     2.8%   15.6%
61-90     0.9%   44.2%
90+       1.0%    100%
——————————————————————————————
DSO = 53.7 天 | 加权坏账率 = 2.66% | 90+ 占比 0.97% | 近 365 天已核销坏账 ≈ 3.6 万
```
> **对标京东物流**（DSO≈31 天、加权 ECL 1.8%、90+ 占 1.1%）：本合成账簿处于健康区间，DSO 略高、
> 指向"催收/账期治理仍有改善空间"——正是结算运营岗的价值切口。

## 异常自动复核 Agent（`src/audit_agent.py`）
对账引擎只"发现"异常，这一层负责"处置"——模拟结算运营人工复核的动作，但自动化：
1. **金额-单据溯源**：对每条异常，从承运商账单金额回溯到系统 `order_items` 逐笔运费、订单状态/承运/签收时间戳、
   容差规则，拼出完整**证据链（audit trail）**——即"这笔钱能否对上单据"。
2. **自动复核**：基于证据链给出 **裁定**（CONFIRMED/SUSPECT/PASS）+ **建议动作**（拒付整笔/拒付重复部分/追回差额/催开票/人工复核）+ 依据 + 置信度。
3. **可选 LLM 研判**：加 `--llm` 且配置 `ANTHROPIC_API_KEY` 时，对 Top-N 条把证据链交给 Claude 做自然语言研判（无密钥自动跳过）。

**实测（13,368 条异常全部复核）：**
```
裁定         建议动作            笔数     金额影响
CONFIRMED   拒付整笔(幽灵单+未送达)  1,607    49,103   ← 直接拒付
CONFIRMED   拒付重复部分           2,989    68,873
CONFIRMED   追回差额(超额计费)       3,493    21,536   ← 向承运商追回
SUSPECT     人工复核/确认成本(少计)   2,357   −14,730
SUSPECT     催承运商开票/确认应付(漏计) 2,922   −65,281
—————————————————————————————————————————————————————
可直接拒付/追回金额合计 ≈ 139,511；另有 6.5 万漏计应付需催票入账
```
产出：`exception_review.csv`（全部异常的结构化裁定）+ `exception_review.md`（Top-N 人读审计备忘，逐条含溯源证据链）。

## 技术栈与 JD 对应
| JD 要求 | 本项目对应 |
|---|---|
| Reconciliation / Exception handling | 三方对账引擎 + 异常清单(带优先级/金额影响) |
| AR aging / Bad-debt monitoring | 五桶账龄 + DSO + ECL 计提 + 坏账候选清单 |
| SQL / Python / Excel / Power BI | DuckDB SQL(CTE/窗口函数/全外连接) + Python 流水线 + Excel 报表(可接 Power BI) |
| Process optimization / automation | 全流程脚本化、一键复现，替代人工逐笔核对 |
| Data analysis & reporting | 汇总表 + 多 sheet Excel 报表 |

## 简历 bullet（示意）
> **电商物流结算自动对账工具（个人项目）** — 基于 Olist 10 万+ 电商订单，用 **DuckDB SQL** 构建
> 订单×承运商×合同容差的**三方对账引擎**，自动分类匹配/超额/重复/幽灵单/未送达/漏计并输出带优先级的
> 异常清单，注入真值验证**异常识别查全率 99.9%**；用 **Python** 搭建**应收账款五桶账龄 + DSO + 坏账 ECL 计提**（ECL 率对标京东物流年报），
> 生成坏账候选与按客户应收暴露报表；并构建**异常金额-单据溯源 + 自动复核 Agent**（可挂 Claude LLM 研判），
> 自动输出拒付/追回/催票处置建议、识别可追回金额约 14 万，对账+复核+报表工时从小时级压缩到分钟级。

## 已知 TODO（下一版）
- ✅ **回款模型校准（已完成）**：引入"核销出账"逻辑，DSO/90+/加权坏账率已落到健康区间（见上）。
- 增加合同费率卡（按重量×距离）的第三方基准校验；接 Power BI 看板；加单元测试。
