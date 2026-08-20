# Stage 2E Implementation Handoff

> 状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 许可：`LICENSE_PENDING`
> 证据：`T3_NOT_REACHED`

## 交付

```text
stock_tracker/quant/data/corporate_action_reconciliation.py
scripts/reconcile_a_share_corporate_actions.py
tests_quant/test_corporate_action_reconciliation.py
docs/STAGE2E-CORPORATE-ACTION-RECONCILIATION-CONTRACT.md
```

## 已实现

- explicit logical-action mapping；
- cross-source field reconciliation；
- identity/date/lifecycle/economic-term conflicts；
- as-of future correction isolation；
- candidate coverage claims；
- primary candidate/coverage requirements；
- independent source count；
- attachment/reference-price evidence gates；
- license status gate；
- candidate-only PromotionEligibility；
- deterministic report identity；
- strict offline report CLI。

## 关键审查修复

1. Identity conflict 纳入 logical action conflict IDs，不能只显示却不阻断资格。
2. 主源 candidate 与主源 coverage 分开要求，防止空 coverage claim 代替真实候选。
3. 每个 terminal candidate 必须匹配唯一 coverage interval 和 source event ID。
4. 同一来源多个 action IDs 合并到同一 logical action 时标记映射歧义。
5. Future bundle/candidate 保留在输入证据链，但不进入过去 as-of terminal。
6. 增加 attachment evidence gate。
7. 所有派生 ID 使用显式 payload，避免 `init=False` 自引用。
8. CLI 嵌套对象使用 exact field set，未知 Trust 字段失败关闭。

## 输出含义

```text
ELIGIBLE_FOR_INDEPENDENT_VERIFICATION
```

只代表可送独立验证，不代表 verified、complete、T2/T3、research-grade 或模型可用。

最终精确测试数量和全量回归结果见：

```text
docs/STAGE2D-STAGE2F-INDEPENDENT-REVIEW.md
```
