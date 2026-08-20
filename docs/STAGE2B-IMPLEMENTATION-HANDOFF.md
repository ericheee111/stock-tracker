# Stage 2B Corporate Action / Adjustment Identity Implementation Handoff

> 日期：2026-08-17
> 工程状态：`READY_FOR_FULL_GATES`
> 证据状态：`CONTRACT_ONLY / SYNTHETIC_VALIDATED`
> 许可状态：`LICENSE_PENDING`
> 研究级状态：`T3_NOT_REACHED`

## 1. 交付范围

主要实现：

```text
stock_tracker/quant/core/corporate_actions.py
stock_tracker/quant/core/fingerprint.py
stock_tracker/quant/core/__init__.py
stock_tracker/quant/storage/migrations/0004_corporate_action_identity.sql
tests_quant/test_corporate_actions.py
tests_quant/test_core_pit.py
tests_quant/test_storage_migrations.py
docs/STAGE2B-CORPORATE-ACTION-CONTRACT.md
```

未修改或未执行：

```text
data/stock_tracker.db migration --apply
真实公司行为数据导入
生产 Bar 原地复权
回测/训练/模型晋级
```

## 2. 实现摘要

### 2.1 公司行为核心

实现：

- `CorporateActionCoverage`；
- `CorporateActionFact`；
- `CorporateActionBook`；
- `CorporateActionSnapshot`；
- exact `Decimal` 条款；
- explicit revision/supersedes graph；
- stable `instrument_id + identity_fact_id + symbol-at-event`；
- `known_at / usable_from / economic dates` 分离；
- `ANNOUNCED / EFFECTIVE / CANCELLED` fail-closed lifecycle。

### 2.2 因子链

实现：

- `SHARE_CHANGE_ONLY` 与 `TOTAL_RETURN` basis；
- `BACKWARD` 与 `FORWARD` convention；
- price ex-date 与 automatic-share listing date 分离；
- rights entitlement 不进入 automatic share quantity；
- reference price 必须绑定 lowercase SHA-256 snapshot ID；
- raw `Bar` 不被修改。

### 2.3 派生身份防绕过

发现并修复了两个合入阻断：

1. `CorporateActionSnapshot.snapshot_id` 已设为 `init=False`，但 builder 仍传入该字段，导致 17 项测试失败；builder 现在只传规范内容，由 dataclass 自行派生身份。
2. `AdjustmentSeries` 原本仍允许调用方传任意 factor list 和一个 64 位 corporate-action snapshot ID；现在 Series 只能接受真实 `CorporateActionSnapshot + basis + convention`，instrument/date/as-of/factors/snapshot ID/series ID 全部派生。

`AdjustedMarketDataView` 进一步绑定：

```text
raw_bar_snapshot_id
calendar_snapshot_id
corporate_action_snapshot_id
adjustment_series_id
basis/convention/policy/as_of
```

该对象只有身份，不包含 adjusted bars 或性能字段。

### 2.4 SQLite 0004

新增：

```text
quant_corporate_action_coverage
quant_corporate_action_fact
```

约束覆盖：

- append-only；
- canonical revision/predecessor；
- lowercase SHA-256；
- canonical Decimal text；
- no-op、rights、currency、reference price/snapshot；
- date 与 known/usable 顺序；
- identity 在 ex-date 有效且 instrument/symbol/market 一致；
- verified/complete source note。

## 3. 对抗性验证

Stage 2B targeted：

```text
python -m unittest discover -s tests_quant -p "test_corporate_actions.py" -v
34 / 34 PASS
```

Migration targeted：

```text
python -m unittest discover -s tests_quant -p "test_storage_migrations.py" -v
17 / 17 PASS
```

Decimal fingerprint targeted：

```text
python -m unittest discover -s tests_quant -p "test_core_pit.py" -v
22 / 22 PASS
```

覆盖内容包括：

- future correction/cancellation；
- revision cycle/missing predecessor/disconnected terminal；
- terminal revision 移出范围；
- missing coverage vs explicit no-action；
- float/int/bool/nonfinite rejection；
- split/cash/rights formula；
- ex-date vs share-listing date；
- symbol change/stable instrument identity；
- direct constructor/replace bypass；
- raw/calendar/action 三快照 adjusted-view binding；
- SQLite direct-write bypass。

## 4. 待最终记录的全量门禁

提交前必须补录：

```text
full tests_quant
full tests
compileall
quant contract smoke
synthetic fixture benchmark
migration dry-run
pip check
ruff
git diff --check
production DB SHA-256 before/after
clean committed-tree validation
```

## 5. 证据边界

本实现不能关闭：

```text
LICENSE_PENDING
T3_NOT_REACHED
真实公司行为覆盖完整性
真实复权正确性
真实回测有效性
```

当前只能声明：

```text
ENGINEERING CONTRACT IMPLEMENTED
SYNTHETIC ADVERSARIAL TESTS PASS
```
