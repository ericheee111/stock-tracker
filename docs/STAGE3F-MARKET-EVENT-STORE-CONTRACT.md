# Stage 3F — 本地 Market Event Store 合同

> 状态：`ENGINEERING_IMPLEMENTED_AND_REVIEWED`

## 1. 隔离

Market Event Store 使用独立路径：

```text
data/market-events/
data/market_events.db
data/market-events-quarantine/
```

不得复用或修改：

```text
data/stock_tracker.db
```

配置解析还会拒绝 Event Catalog 与 Monitor DB 共用同一 SQLite，以及 Event Root 与 Quarantine Root 相同或互为父子目录。所有路径均由配置中的仓库相对路径解析；绝对路径、`..` 与 Symlink 逃逸失败关闭。`data/` 继续由 `.gitignore` 排除。

## 2. 原始事件与 Hash Chain

每个接受事件写入一份 canonical JSON immutable record：

```text
market=A/trading_day=YYYY-MM-DD/symbol=CODE.SH/
  event-<callback-seq>-<event-id>.json
  manifest.json
```

Record 绑定：

```text
schema
event
previous_record_hash
record_hash
```

`record_hash = SHA256(canonical(record identity))`。分区 Manifest 绑定事件文件 SHA、前序 Hash、首尾 Hash、事件数、时间、回调序列和不可变 `append_order`。

Hash Chain 按真实追加顺序连接，而不是按可能乱序到达的 `callback_seq` 重排。`callback_seq=1,3,2` 的输入会保留该证据顺序，Manifest 的 `append_order=1,2,3` 仍保持链连续；乱序本身同时记录为 Finding。

Catalog 当前 schema 为：

```text
stock-tracker-market-event-store-v3
```

v2 Catalog 首次打开时按既有 SQLite `rowid` 回填 `append_order` 并升级为 v3；不会重写 immutable Event File。若历史文件本身已被篡改或既有链不一致，后续全量 Integrity 仍会失败关闭。

## 3. 协调提交与补偿边界

SQLite `BEGIN IMMEDIATE` 事务覆盖：

1. Session 元数据；
2. Event 元数据；
3. Finding；
4. 分钟聚合；
5. Partition Manifest 元数据。

Event File 和 Manifest 使用同目录临时文件、`fsync` 与原子替换激活。文件系统与 SQLite 不存在真正的跨资源原子事务，因此实现采用协调提交与补偿恢复：分钟聚合、Manifest 或 SQLite Commit 失败时回滚 SQLite、删除本轮新建 Event File，并按已提交 Catalog 重建 Manifest。文档不把该机制夸大为跨文件系统/SQLite 的单一原子事务。

## 4. Finding

```text
CALLBACK_SEQUENCE
PROVIDER_SEQUENCE
OUT_OF_ORDER
SOURCE_TIME_REGRESSION
CONTRACT_VIOLATION
```

规则：

- `callback_seq` 按 Session 全局递增检查，不按 Symbol 错报跨标的 gap；
- Provider Gap 只在相同 Session/Symbol 且双方 Provider Sequence 均存在时计算；
- 合同失败进入 metadata-only Quarantine；
- Quarantine 不保存可能含秘密或账户信息的畸形原始 Body。

## 5. 分钟聚合

OHLC 取事件 `last`；成交量与成交额按相同 Session 内累计值增量计算。累计值回退、跨 Session、乱序、Gap 或样本稀疏会产生：

```text
COMPLETE
INCOMPLETE_GAP
INCOMPLETE_OUT_OF_ORDER
INCOMPLETE_SPARSE
```

持久化分钟 Bar 固定为 `DELAYED`，不得标为 `LIVE`。

## 6. Integrity

```python
store.verify_integrity()                       # 全量手工验收
store.verify_integrity(partition_keys=keys)    # Poll 受影响分区
```

每次采集 Poll 只扫描受影响分区，避免历史数据增长后全库线性拖慢实时采集；发布/手工验收仍执行全量完整性检查。

## 7. Replay

`MarketEventReplay` 支持：

```text
python
auto
duckdb
```

DuckDB 未安装时，显式 `duckdb` 请求失败；`auto` 可诚实回退到 Python，并在结果中分别记录 requested/used backend。Replay Identity 绑定 Event ID 与 Record Hash，不允许任意 SQL。

Monitor 的 `GET /api/monitor/replay` 使用 `record_run=false`，确保读取型 API 不写 Replay Catalog；事件行与分钟 Bar 使用同一 start/end 窗口。显式 CLI Replay 可以记录本地 replay run，但仍不修改生产数据库。

该 Replay 是本地运行事件回放，不等于正式 PIT Replay；正式历史决策重放仍需要 T3 数据、历史 Universe、证券状态、公司行为和事件已知时间。

## 8. 验收结论

```text
APPEND_ONLY_FILES = PASSED
HASH_CHAIN = PASSED
OUT_OF_ORDER_APPEND_CHAIN = PASSED
COORDINATED_COMMIT_AND_COMPENSATION = PASSED
V2_TO_V3_CATALOG_MIGRATION = PASSED
DEDUP_GAP_SESSION = PASSED
MINUTE_AGGREGATION = PASSED
TARGETED_AND_FULL_INTEGRITY = PASSED
PRODUCTION_DATABASE_MODIFIED = FALSE
RESEARCH_GRADE = FALSE
```
