# BOUNDARY-MATRIX — WB1 / R4.1 (788ffdb)

- 会话：WB1（WorkBuddy，W5 批次）
- 来源：WB1-w5-788ffdb 原始 clone；具体路径及字节身份见仓外 SOURCE-MANIFEST。
- 基线：HEAD `788ffdb1cc3e770f99d63bdd10d923f46d9b1ddd` / Tree `18a5b27bf2219bd5536f76736099da9ad5dac262`
- 依据：`docs/STAGE4G1-CHECKPOINT-B0-B1-R4.1-VALIDATION.md`、`CHATGPT-R4.1-REVIEW-INPUT.md`、`AUDIT-DECISIONS.md`（R4_01 = CLOSED）、AGENTS.md §7/§8
- 状态标记：`COVERED` = R4.1 已有测试覆盖；`GAP` = 本次新增测试覆盖；`N/A` = 本任务范围外/已明确由 Codex 冻结。

> R4-01 已在 R4.1 通过 `_live_lifecycle_is_ordered` + `INCOMPLETE_LIFECYCLE` 关闭（`source_snapshot_contracts.py:3023-3060`、`3069-3072`）。本矩阵不再把旧红测当作新发现。

## 约束 → R4.1 已有测试 → 本次缺口

| # | 冻结约束 | 代码位置（相对 clone） | R4.1 已有测试 | 状态 | 本次新增 |
|---|---|---|---|---|---|
| M1 | market_session_date：aware datetime / exact Market / 已知 policy | `stock_tracker/core/market_time.py:22-27` | `test_market_session_date_covers_a_hk_and_us_dst`（正向换算 + US DST） | 部分 | C01–C06（naive/子类/伪 tz/非 Market/未知 policy/非 str policy） |
| M2 | TransportRecord int 字段 exact int（bool/float/str 拒绝）+ 范围 | `source_snapshot_contracts.py:158-169, 1953-1965` | 无系统表驱动 | GAP | T01–T05 |
| M3 | sha256 字段小写 64 hex；text 安全（trim/控制字符/空） | `:135-155, 1946-1969` | 无系统表驱动 | GAP | T06–T09 |
| M4 | kind 必须 exact `MarketEventTransportKind` | `:1970-1971` | 无 | GAP | T10–T11 |
| M5 | observed_at / durable_known_at aware UTC；observed ≤ durable | `:172-177, 1972-1979` | 无 naive/等值用例 | GAP | T12–T14 |
| M6 | canonical_payload 仅允许 `"{}"` | `:1980-1983` | 无 | GAP | T15–T16 |
| M7 | LIVE 不得携带 job 字段；REPLAY/BACKFILL 必须携带 | `:1984-2010` | 无 | GAP | T17–T18 |
| M8 | Snapshot：records exact tuple、append 连续、hash 前缀、store/stream/known-time 一致 | `:2077-2157` | `test_r4_transport_store_mismatch_rejected`、`test_r4_transport_chain_and_counter_rollback_rejected` | 部分 | I01–I03（stream 混用/known-time 回退/非 tuple） |
| M9 | 同 epoch 重复 start / close 后分支拒绝 | `:2132-2151` | `test_r41_activity_after_closed_is_rejected`、`test_r41_same_epoch_restart_after_disconnect_is_rejected` | COVERED | — |
| M10 | watermark/counter/observed 单调 | `:2110-2131` | `test_r4_transport_chain_and_counter_rollback_rejected`（prefix） | 部分 | I04（watermark 回退） |
| M11 | 重复 start（CONNECTED/REPLAY/BACKFILL）拒绝 | `:2136-2145` | 无独立用例 | GAP | I05 |
| M12 | **R4-01 同时间戳生命周期次序** | `:3023-3072` | `TestR41TransportLifecycle` 11 项 | COVERED（已关闭） | —（不再当新发现） |
| M13 | _epoch_state：CONNECTED 唯一≤start；DISCONNECTED<end 阻断；ACK scope 精确 | `:3073-3102` | r41 ack scope / disconnect / new epoch 等 | COVERED | — |
| M14 | clock：ahead≤2s、received→durable≤5min、regression 0；等值边界接受 | `:365-382, 1440-1464` | `test_r4_one_hour_negative_latency`、`test_r4_durability_delay_boundary_is_explicit`（仅 301 超限）、`test_r4_small_explicit_clock_skew`（1s） | 部分（缺等值临界） | C07–C09 |
| M15 | 未知 clock/liveness policy 失败关闭 | `:377-382, 1910-1917` | `test_r4_unknown_clock_and_liveness_policies_rejected`、r41 policy v2 identity | COVERED | — |
| M16 | inventory 重复 event/record/storage/append 身份拒绝 | `:1248-1262` | 无独立用例 | GAP | I06–I08 |
| M17 | artifact catalog schema/journal/表/索引/触发器/唯一约束严格校验 | `store.py:322-465` | `test_store_rejects_inventory_and_exact_schema_tampering`、`test_store_schema_tamper_is_a_stable_global_block`、`test_schema_audit_rejects_trigger_body_index_view_and_generated_column` | COVERED | — |
| M18 | record/query Store 错配（source_runtime_store_id 绑定） | `store.py:445-457, 707-715` | `test_store_is_bound_to_one_runtime_evidence_store`、`test_worker_rejects_source_runtime_store_mismatch`、`test_repository_rejects_source_runtime_store_mismatch` | COVERED | — |
| M19 | 有界组合：非法值注入必抛合同错误 | AGENTS.md §8 | 无 | GAP | fuzz（≤100 固定种子，独立脚本） |

## 本次新增测试清单（35 场景 + 1 组固定种子探测）

| ID | 文件 | 场景 | 预期（来源） |
|---|---|---|---|
| T01 | test_wb1_type_boundary.py | `transport_append_order` = True / 1.0 / "1" / 0 | 拒绝，`_require_int`（:158-169） |
| T02 | 同上 | `connection_epoch` = "1" / None | 拒绝（:1954） |
| T03 | 同上 | `queue_overflow_count` / `dropped_callback_count` = True | 拒绝（:1964-1965） |
| T04 | 同上 | `reconnect_epoch` = 0（合法下界）→ 接受；= -1 → 拒绝（:1955） |
| T05 | 同上 | `callback_high_water` = False / "5" | 拒绝（:1959-1963） |
| T06 | 同上 | `source_store_id` = "not-a-hash" / 大写 SHA / None | 拒绝（`_require_sha256`） |
| T07 | 同上 | `session_id` = 前后空格 / 空串 / 控制字符 | 拒绝（`_require_text`） |
| T08 | 同上 | `previous_transport_record_hash` = 非 hex | 拒绝（:1946-1951） |
| T09 | 同上 | `transport_stream_id` = None | 拒绝 |
| T10 | 同上 | `kind` = "CONNECTED"（str） | 拒绝（:1970-1971） |
| T11 | 同上 | `kind` = None | 拒绝 |
| T12 | 同上 | `observed_at` naive | 拒绝（`_require_utc`） |
| T13 | 同上 | `durable_known_at` naive | 拒绝 |
| T14 | 同上 | `observed_at` == `durable_known_at`（等值）→ 接受；observed > durable → 拒绝（:1974-1977） |
| T15 | 同上 | `canonical_payload` = `'{"x":1}'` | 拒绝（:1980-1983） |
| T16 | 同上 | `canonical_payload` = 123（非 str） | 拒绝 |
| T17 | 同上 | LIVE record 携带 job 字段 | 拒绝（:1999-2010） |
| T18 | 同上 | REPLAY record 缺 job 字段 | 拒绝（:1984-1989） |
| C01 | test_wb1_clock_boundary.py | market_session_date naive | ValueError（market_time.py:22-23） |
| C02 | 同上 | market_session_date datetime 子类 | ValueError（`type is not datetime`） |
| C03 | 同上 | market_session_date 伪 tz（utcoffset None） | ValueError |
| C04 | 同上 | market_session_date 非 Market（str "A"） | ValueError（:24-25） |
| C05 | 同上 | market_session_date 未知 policy | ValueError（:26-27） |
| C06 | 同上 | market_session_date policy 非 str（bytes） | ValueError |
| C07 | 同上 | clock ahead = 2s（等值）→ 无 finding（正向） | `:1443-1444`（`>` 严格） |
| C08 | 同上 | clock ahead = 2s+1µs → finding | 同上 |
| C09 | 同上 | durability 300s（等值）→ 接受；300s+1s → finding | `:1448-1449`（`>` 严格） |
| I01 | test_wb1_identity_lifecycle.py | snapshot records 为 list（非 tuple） | 拒绝（:2081-2084） |
| I02 | 同上 | 两条 record 不同 `transport_stream_id` | 拒绝（:2104） |
| I03 | 同上 | durable_known_at 回退 | 拒绝（:2102-2103） |
| I04 | 同上 | callback_high_water 回退 | 拒绝（:2110-2118） |
| I05 | 同上 | 同 epoch 重复 CONNECTED（append 不同位置） | 拒绝（:2141-2144） |
| I06 | 同上 | inventory 重复 event_id | 拒绝（:1248-1262） |
| I07 | 同上 | inventory 重复 record_storage_key | 拒绝 |
| I08 | 同上 | inventory append_order 重复 | 拒绝（:1243-1247） |

## 未覆盖 / 明确缺口（保留说明，不凑数）

- TransportRecord `job_input_selection` / `job_input_verification` 内部字段级合法性（REPLAY/BACKFILL job contract）由 `validate_selection_verification` 与既有 selection 测试覆盖，本批不重复。
- Store 层 artifact schema/binding/identity（M17/M18）已有大量 `test_runtime_evidence.py` 覆盖，不再新增。
- 物理 B1 Store 的 format/codec/stream 属 B1a–B1d，不在本批范围（AUDIT-DECISIONS.md §6）。
