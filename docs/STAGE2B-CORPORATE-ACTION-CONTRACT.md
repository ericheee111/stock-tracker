# Stage 2B：公司行为、复权因子与调整视图身份合同

> 日期：2026-08-17
> 工程范围：A 股优先的 provider-neutral Corporate Action / Adjustment Identity Foundation
> 工程状态：`IMPLEMENTED / SYNTHETIC_VALIDATED`
> 数据证据状态：`CONTRACT_ONLY`
> 许可状态：`LICENSE_PENDING`
> 研究级状态：`T3_NOT_REACHED`

## 1. 目标与非目标

Stage 2B 解决一个基础金融正确性问题：公司行为不能通过覆盖原始 Bar、按当前公告倒灌历史、或用一个不带来源身份的浮点 `adjustment_factor` 处理。

本阶段提供：

- 稳定 `instrument_id` 下的公司行为事实；
- `known_at / usable_from / ex_date / record_date / payment_date / share_listing_date` 分离；
- 显式 revision/supersedes 图；
- 精确 `Decimal` 条款；
- 公司行为 Snapshot、复权因子链和 adjusted-view 身份；
- append-only SQLite migration；
- synthetic adversarial tests。

本阶段不提供：

- 真实 SSE/SZSE/CNINFO 公司行为完整历史；
- 真实来源许可结论；
- 自动 PDF/XLS/XLSX 解析；
- 对真实证券的前复权/后复权正确性声明；
- 已调整 Bar 数据集；
- 回测收益、策略表现或模型晋级。

## 2. 稳定证券身份

公司行为的永久关联键是：

```text
instrument_id
```

`symbol` 只表示事件发生时有效的交易代码。每个行为事实同时绑定：

```text
instrument_id
identity_fact_id
symbol-at-event
market
```

`identity_fact_id` 必须对应在 `ex_date` 有效、且 instrument/symbol/market 一致的 `InstrumentIdentityFact`。因此：

- 代码变更不会拆分同一证券；
- 同代码复用不会合并不同证券；
- 退市证券的历史行为仍可保留；
- 合并/转换等尚未支持的语义不会通过 symbol 猜测身份。

## 3. 时间合同

### 3.1 信息可得时间

```text
known_at      = 系统有证据证明首次知道该事实的时间
usable_from   = 策略/研究允许使用该事实的最早时间
```

必须满足：

```text
usable_from >= known_at
```

后续修订、取消或更正只能影响其 `known_at / usable_from` 之后的 Snapshot，不能改写此前的历史 Snapshot。

### 3.2 经济与实施日期

合同分别保留：

```text
record_date
ex_date
payment_date
share_listing_date
```

其中：

- `ex_date` 控制价格连续性调整；
- `share_listing_date` 控制自动获得股份的数量连续性；
- 二者不得默认相同；
- `payment_date` 不得早于 `ex_date`；
- 自动股份比例不为 1 时必须给出 `share_listing_date`。

Stage 2C candidate 还保留 `source_published_at`、其时间粒度和更丰富的实施状态；进入 Stage 2B core 时仍不会把发布日期伪造成 `known_at`。

## 4. 生命周期

Stage 2B core 使用最小、失败关闭的三态：

```text
ANNOUNCED
EFFECTIVE
CANCELLED
```

只有 `EFFECTIVE` 可以生成复权因子。

Stage 2C 的 `PROPOSED / APPROVED / IMPLEMENTATION_ANNOUNCED` 在进入 core 时最多映射为 `ANNOUNCED`；`EFFECTIVE / COMPLETED` 才映射为 `EFFECTIVE`。未实施方案不得产生因子。

`CANCELLED` revision 不得携带任何经济条款。

## 5. 条款与精确数值

所有经济数值必须是有限 `Decimal`，禁止 float、int、bool、NaN 和 Infinity。

定义：

```text
A  = automatic_share_ratio
     每 1 股旧股自动变成的股份数
     包括拆股、合股、送股/转增；不包括需要主动认购的配股

C  = cash_dividend_per_share
R  = rights_entitlement_ratio
Pr = rights_subscription_price
P0 = reference_price
```

约束：

- `A > 0`；
- `C >= 0`；
- `R >= 0`；
- `R > 0` 时 `Pr > 0`；
- `P0 > 0`；
- `P0` 必须绑定 `reference_price_snapshot_id`；
- `A = 1, C = 0, R = 0` 是 no-op，禁止作为有效行为；
- monetary terms 必须有明确 currency；
- rights entitlement 绝不自动增加持仓股数。

## 6. 价格因子

### 6.1 `SHARE_CHANGE_ONLY`

仅处理自动股份变化：

```text
backward_price_multiplier = 1 / A
forward_price_multiplier  = A
```

纯拆股/送转不需要参考价格。

### 6.2 `TOTAL_RETURN`

当现金或配股条款存在时，必须给出精确参考价格及其 Snapshot 身份：

```text
backward_price_multiplier
= (P0 - C + R * Pr) / (P0 * (A + R))

forward_price_multiplier
= 1 / backward_price_multiplier
```

该公式表达一个明确的理论除权口径，不代表 Stage 2B 已证明任何真实数据源使用相同口径。缺少 `P0`、`Pr` 或绑定身份时失败关闭，不生成猜测因子。

## 7. 股数因子

股数连续性只包含自动股份变化：

```text
rights entitlement 不进入 automatic share multiplier
```

行为的价格生效日期为 `ex_date`，自动股份数量生效日期为 `share_listing_date`。两者分别参与因子查询。

对于 `BACKWARD` convention，早于自动股份生效日的旧股数乘以 `A`，转换到末端股份口径；对于 `FORWARD` convention，生效后的股数乘以 `1/A`，转换到起点股份口径。

## 8. Snapshot 与 revision graph

### 8.1 Coverage

`CorporateActionCoverage` 表达某个 instrument/date range 是否被明确调查。只有一条可见、可用且满足请求范围的 source/version terminal coverage 才可生成正式 Snapshot。

```text
缺失 coverage != 没有公司行为
```

只有完整 coverage 下的空 action 集合，才能证明“该范围内没有已选行为”。

### 8.2 Revision

Coverage 和 action 均通过显式 `supersedes_revision` 图解析，禁止 revision 字典序或数字外观推断。

解析顺序是：

1. 收集同一 source/version/action stream 的全部 as-of 可见节点；
2. 验证 cycle、missing predecessor、多个 disconnected terminal；
3. 选择唯一 terminal；
4. 最后再判断 terminal 是否覆盖请求范围或其 ex-date 是否位于请求范围。

这避免“旧 revision 在范围内、新 terminal 已移出范围”时错误保留旧事实。

## 9. 派生身份不可注入

以下身份全部由内容重新计算，字段为 `init=False`：

```text
CorporateActionSnapshot.snapshot_id
AdjustmentSeries.factors
AdjustmentSeries.series_id
AdjustmentSeries.corporate_action_snapshot_id
AdjustedMarketDataView.view_id
```

`dataclasses.replace()` 不能注入或删除这些派生字段。

`AdjustmentSeries` 只能由真实 `CorporateActionSnapshot + basis + convention` 构造；调用方不能传入任意 factor list 或伪造 Snapshot ID。

## 10. Adjusted Market Data View 身份

复权因子链不等于已调整行情。一个未来 adjusted-bar view 必须同时绑定：

```text
raw_bar_snapshot_id
calendar_snapshot_id
corporate_action_snapshot_id
adjustment_series_id
basis
convention
policy_version
as_of
instrument/date range
```

`AdjustedMarketDataView` 当前只提供 tamper-evident 身份，不包含 bars，也不产生性能结论。任何一项绑定变化都会改变 `view_id`。

## 11. SQLite migration 0004

新增 append-only 表：

```text
quant_corporate_action_coverage
quant_corporate_action_fact
```

数据库层检查：

- lowercase SHA-256；
- canonical revision encoding 与 predecessor pair；
- self-supersede 拒绝；
- canonical Decimal text；
- date 和 known/usable 时间顺序；
- no-op 拒绝；
- rights price、currency、reference snapshot 配对；
- automatic share change 的 listing date；
- verified/complete source note；
- identity fact 在 ex-date 有效且 instrument/symbol/market 一致；
- UPDATE/DELETE append-only trigger。

migration 不自动 apply，`quant_migrate.py` 默认只做 dry-run。

## 12. 证据边界

即使所有 synthetic tests 通过，也只能得出：

```text
ENGINEERING CONTRACT IMPLEMENTED
SYNTHETIC ADVERSARIAL CASES PASS
```

不能得出：

```text
真实公司行为覆盖完整
真实复权价格正确
真实历史收益有效
RESEARCH_GRADE
T2/T3 已达成
```

必须继续保持：

```text
CONTRACT_ONLY / SYNTHETIC_VALIDATED
LICENSE_PENDING
T3_NOT_REACHED
```
