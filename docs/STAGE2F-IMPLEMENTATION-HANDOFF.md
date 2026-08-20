# Stage 2F Implementation Handoff

> 状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 许可：`LICENSE_PENDING`
> 证据：`T3_NOT_REACHED`

## 交付

```text
stock_tracker/quant/data/adjusted_market_data.py
scripts/materialize_adjusted_market_data.py
tests_quant/test_adjusted_market_data.py
tests_quant/fixtures/adjusted_market_data/
docs/STAGE2F-ADJUSTED-MARKET-DATA-CONTRACT.md
```

## 已实现

- immutable raw-bar snapshot；
- verified/complete Calendar materialization contract；
- stable identity and corporate-action series binding；
- ex-date price adjustment；
- share-listing-date position normalization；
- rights entitlement non-automatic behavior；
- strict open-session validation and explicit gaps；
- separate Decimal adjusted rows；
- raw volume/amount/turnover preservation；
- dataset/row/descriptor identities；
- immutable JSONL write/load/tamper checks；
- synthetic-only offline CLI。

## 关键审查修复

1. Formal dataset 改为只接受 raw/calendar/identity/series/policy 对象，rows、gaps 和全部 IDs 均 `init=False` 派生。
2. 输出 descriptor 增加 instrument/market/range/as_of/row IDs，并由 loader 重新计算 dataset ID。
3. Loader 从 JSONL 内容重新计算 row ID，即使攻击者同时重算数据文件 SHA，也不能篡改 row content。
4. Row timestamp 统一 UTC，session date 按市场时区重新验证。
5. 禁止闭市日 Bar、缺失交易日静默通过，以及 future raw/calendar/identity。
6. 明确不调整 volume/amount/turnover，避免猜测供应商口径。
7. 所有派生 ID 使用显式 payload，避免 `init=False` 自引用。
8. CLI 使用 exact nested schema，未知 Trust/模型字段失败关闭。

## 输出边界

输出只是 synthetic contract dataset，不产生：

```text
backtest performance
model training readiness
real adjusted-price correctness
research-grade promotion
```

最终测试数量和全量门禁结果见：

```text
docs/STAGE2D-STAGE2F-INDEPENDENT-REVIEW.md
```
