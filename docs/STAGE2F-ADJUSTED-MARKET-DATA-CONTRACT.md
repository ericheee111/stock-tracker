# Stage 2F：确定性复权行情数据集合同

> 工程状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 数据状态：`SYNTHETIC_CONTRACT_ONLY`
> 许可状态：`LICENSE_PENDING`
> 证据等级：`T3_NOT_REACHED`

## 1. 目标

Stage 2F 将多份不可变输入绑定为独立的 adjusted dataset：

```text
raw-bar artifact/snapshot
+ verified complete Calendar contract
+ verified stable instrument identity
+ verified complete corporate-action snapshot
+ AdjustmentSeries
+ versioned materialization policy
→ separate adjusted rows
→ immutable JSONL artifact and descriptor
```

Raw `Bar` 永远不被原地修改。

## 2. Formal materialization 输入

### 2.1 RawBarSnapshot

绑定：

```text
raw artifact ID
instrument_id
identity_fact_id
symbol/market
range/as_of
chronological unique raw bars
raw row identities
source note
```

要求：

- OHLC 有限且大于 0；
- high/low 一致；
- bars 严格按时间排序且无重复；
- 每个 bar 位于 snapshot range；
- symbol/market 一致。

### 2.2 CalendarMaterializationSnapshot

绑定：

```text
market
range/as_of
open sessions
verified/complete contract flags
source note
```

Formal materialization 要求 `verified=True` 且 `complete=True`。当前 synthetic 测试中的布尔值只验证工程门禁，不证明真实 Calendar 已达到 Trust Tier。

### 2.3 Identity 与 Corporate Action

要求：

- `InstrumentIdentityFact` verified；
- identity 在每个 bar session 有效；
- identity known/usable 不晚于 series as_of；
- `AdjustmentSeries` 来自 `require_verified + require_complete` 的 `CorporateActionSnapshot`；
- instrument/market/date range 与 raw/calendar 完全一致。

## 3. 时间与交易日验证

- raw/calendar as_of 不能晚于 series as_of；
- bar timestamp 不能晚于 series as_of；
- bar 不能落在 Calendar closed session；
- 默认要求所有 open sessions 都有 bar；
- `ALLOW_EXPLICIT_GAPS` 时，显式 gap 列表必须与缺失 open sessions 完全相等；
- gap 列表不能含重复或乱序条目。

## 4. Price 与 Share 日期

价格复权继续使用：

```text
ex_date
```

自动股份数量口径继续使用：

```text
share_listing_date / automatic-share effective date
```

因此同一 action 可以表现为：

```text
除权日后价格已切换
新股上市日前股数口径尚未切换
```

配股权利只作为 metadata，永远不自动增加股份数量。

## 5. Decimal 与原始字段

OHLC 通过 `Decimal(str(raw_value))` 进入固定 50 位 context，调整值与 multiplier 均以 canonical decimal text 写出。

Stage 2F 不猜测供应商对下列字段的复权口径：

```text
volume
amount
turnover
```

它们作为原始值保留，并带有：

```text
PRESERVED_RAW; volume/amount/turnover were not vendor-adjusted
```

不会产生含义模糊的“已复权成交量”。

## 6. Dataset 身份不可注入

`AdjustedMarketDataDataset` 只能由以下对象构造：

```text
RawBarSnapshot
CalendarMaterializationSnapshot
InstrumentIdentityFact
AdjustmentSeries
AdjustedMarketDataPolicy
explicit gap sessions
```

以下字段全部是 `init=False` 派生值：

```text
raw_bar_snapshot_id
calendar_snapshot_id
corporate_action_snapshot_id
adjustment_series_id
identity_fact_id
instrument/range/as_of
policy_id
rows
gaps
dataset_id
```

调用方不能配一个合法 ID 再传入任意 adjusted rows。

Dataset ID 绑定：

- 所有输入 Snapshot/Series/Identity/Policy IDs；
- basis/convention（通过 AdjustmentSeries ID）；
- range/as_of；
- output row IDs；
- explicit gap report。

## 7. Adjusted row

每行同时保存：

```text
raw OHLC
adjusted OHLC
price multiplier
automatic share multiplier
raw volume/amount/turnover
raw source
raw-field status
identity and session data
```

Row timestamp 规范化为 UTC，但 session date 按 market timezone 复核；二者不一致则失败。

## 8. 不可变 Artifact

输出：

```text
adjusted-market-data/<data_sha256>.jsonl
adjusted-market-data/descriptors/<dataset_id>.json
```

Descriptor 包含：

- dataset ID；
- data SHA/key/length/row count；
- raw/calendar/action/series/identity/policy IDs；
- instrument/market/range/as_of；
- row IDs；
- gaps。

Loader 会：

1. 严格读取 descriptor field set；
2. 从 descriptor 元数据重新计算 dataset ID；
3. 检查 JSONL length/SHA/UTF-8/row count；
4. 从每行内容重新计算 row ID；
5. 检查 descriptor row IDs 与 JSONL rows 一致；
6. 检查确定性顺序。

## 9. CLI

入口：

```text
scripts/materialize_adjusted_market_data.py
```

CLI 只接受 strict synthetic request，使用 exact nested field sets。没有：

```text
network
database or migration apply
backtest
model training
verified/trust/promotion switches
```

输入不能放在 output root 内部，输出采用不可变内容寻址。

## 10. 非目标与证据边界

本阶段不声明：

```text
真实 SSE/SZSE 公司行为已完整
真实供应商 Bar 已正确复权
真实回测有效
模型可训练或晋级
投资收益
T2/T3 已达到
```

继续保持：

```text
SYNTHETIC_CONTRACT_ONLY
LICENSE_PENDING
T3_NOT_REACHED
```
