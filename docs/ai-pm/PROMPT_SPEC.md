# 结构化模型摘要 Prompt 规格

> Prompt 版本：`settlement-evidence-summary-v2`  
> 实现：`src/model_review.py`  
> 回滚：将服务环境的版本切回上一稳定提交；规则裁定和人工审批不受模型版本影响。

## 1. 任务边界

模型只负责把已经由 SQL 和规则验证的证据压缩成审核摘要。它不能：

- 计算或修改金额；
- 改变规则裁定、建议动作或提高置信度；
- 引用证据账本外的信息；
- 输出支付、退款、追回或自动执行指令；
- 输出隐藏推理过程。

## 2. 输入契约

输入包含三块：

1. `recon_status`：上游对账异常类型；
2. `evidence_ledger`：限定为 `SOR_AMOUNT`、`ITEM_COUNT`、`BILL_AMOUNT`、`BILL_COUNT`、`ORDER_STATUS`、`DELIVERY_TIMESTAMP`、`RECON_RULE`；
3. `rule_decision`：确定性规则的 `verdict`、`recommended_action`、`confidence` 和人读理由。

不向模型发送客户联系方式、支付账户、API Key 或与案件无关的个人信息。

## 3. 输出 Schema

```json
{
  "verdict": "CONFIRMED | SUSPECT | PASS",
  "recommended_action": "受限动作枚举",
  "explanation": "12–500 字符的证据摘要",
  "evidence_ids": ["SOR_AMOUNT", "BILL_AMOUNT", "RECON_RULE"],
  "confidence": "高 | 中 | 低",
  "fallback_reason": null
}
```

Pydantic 配置为 `extra="forbid"`；未知字段、未知证据 ID、空证据列表或超长文本会直接解析失败。

## 4. 运行时护栏

模型输出只有同时满足以下条件才标记为 `generated`：

1. 通过严格 Schema；
2. 引用的证据 ID 存在；
3. 裁定与规则完全一致；
4. 建议动作与规则完全一致；
5. 模型置信度不高于规则置信度。

否则状态为 `fallback`，界面只展示规则理由并显示回退原因。没有 API Key 或 SDK 时状态为 `disabled`，同样明确展示规则理由。

## 5. 版本、观测与成本

每次模型结果记录 provider、模型 ID、Prompt 版本、时延、输入/输出 Token 和护栏原因。价格不硬编码；只有配置当前的 `ANTHROPIC_INPUT_USD_PER_MTOK` 与 `ANTHROPIC_OUTPUT_USD_PER_MTOK` 时才估算单案成本，避免把过期价格写进产品逻辑。

模型升级步骤：

1. 在开发集运行候选模型；
2. 检查 Schema、证据引用、冲突和 Bad Case；
3. 在冻结集复测；
4. 达到门槛后更新模型 ID 和 Prompt 版本；
5. 异常时回滚提交或关闭模型摘要，规则流程继续工作。
