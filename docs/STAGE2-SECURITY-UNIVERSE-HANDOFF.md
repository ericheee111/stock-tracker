# Stage 2A Agent C：Security Identity / Status / Historical Universe Handoff

> 日期：2026-08-14
> 状态：`SYNTHETIC_VALIDATED / T2_CANDIDATE_EVIDENCE / T3_NOT_REACHED`
> Agent C 结论：**C 侧 schema 与输出已可交给 Agent D 做 reconciliation；不得据此生成 `complete=true`、`verified=true` 或 T3 声明。**

## 1. 实际交付

新增文件：

```text
stock_tracker/quant/data/security_universe_adapter.py
scripts/import_a_share_identity.py
tests_quant/test_security_universe_adapter.py
tests_quant/fixtures/security_universe/golden_sse.json
tests_quant/fixtures/security_universe/golden_sse.descriptor.json
docs/STAGE2-SECURITY-UNIVERSE-HANDOFF.md
```

Adapter 只读取已捕获的 strict UTF-8 JSON bytes 和 checksum-bound descriptor，不联网、不访问 Provider、不写 SQLite。输入拒绝重复 JSON key、NaN/Infinity、bool-as-int、未知字段、source/version 不匹配、bytes/hash 不匹配、重复事实和相同 `known_at + revision` 冲突。

稳定身份映射冻结为：

```text
instrument_id = "CN:" + exchange + ":" + source_security_id
```

`symbol` 不参与永久身份。同一 `source_security_id + exchange` 的代码变化保持同一个 `instrument_id`；同一 symbol 被不同 `source_security_id` 复用时形成新 `instrument_id`；无法证明连续性且有效期重叠时失败关闭。

SSE/SZSE Universe 独立：

```text
SSE  -> A_SHARE_SSE_ALL
SZSE -> A_SHARE_SZSE_ALL
```

Adapter 不输出单源 `A_SHARE_ALL`。

## 2. Candidate 输出合同

CLI 生成：

```text
candidate_bundle.json
instrument_identities.jsonl
security_statuses.jsonl
universe_memberships.jsonl
coverage.json
coverage_report.json
```

Identity candidate 保留：

```text
source_security_id
exchange
instrument_id
symbol
name
security_type
board
effective_from / effective_to
source_published_at / source_published_granularity
observed_at / retrieved_at / known_at / usable_from
revision / supersedes
source_uri / evidence_ids
```

Status candidate 独立保留 `listing_state / trading_state / risk_designation`，并区分 `DAILY` 与 `INTRADAY`。盘中临停/复牌保存精确 aware datetime 区间，不压成全天停牌。只有明确的 DAILY 且 listing 非 `UNKNOWN` 的记录才产生 core `SecurityStatusFact`；原始 `RESUMED` 在 candidate 中保留，core 日级兼容值为 `TRADABLE`；未知 listing 不伪造 core 状态。

Membership 只接受显式 `INCLUDED / EXCLUDED` 事件，reason 支持：

```text
LISTED
RELISTED
DELISTED
TYPE_CHANGE
OUT_OF_SCOPE
UNKNOWN
```

absence 不生成 `EXCLUDED`。当前列表只能声明 `coverage_kind=CURRENT_ANCHOR`，输出仍固定为 `complete=false / verified=false`。

Bundle 的以下属性可直接交给 `HistoricalUniverse`：

```text
bundle.coverage
bundle.core_identities
bundle.core_statuses
bundle.core_memberships
bundle.historical_universe()
```

本轮 golden fixture 已实际通过：

```python
bundle.historical_universe().snapshot(
    "A_SHARE_SSE_ALL",
    Market.A,
    date(2024, 1, 12),
    as_of,
    require_verified=False,
    require_complete=False,
)
```

结果保留 `600200.SH` 退市身份，`delisted_symbols == ("600200.SH",)`。该调用只证明 candidate/core 兼容，不是 Research Identity 晋级。

## 3. Coverage Report

`coverage_report.json` 固定包含：

```text
unclosed_delistings
missing_listing_event
missing_identity
missing_daily_session_status
missing_exclusion_reason
quantity_continuity_gaps
unparsed_attachments
cross_source_conflicts
current_anchor_only
```

退市闭环 candidate evidence 分别接收：交易所上市公告、终止上市公告、交易所退市列表和 ChinaClear 终止登记 evidence IDs。数量连续性使用：

```text
end_count = begin_count + listings + relistings - delistings + scope_changes
```

Adapter 只列出缺口，不判断历史已经完整。`complete`、`verified`、`trust_tier` 一旦出现在 artifact 任意层级即拒绝；CLI 不提供 trust tier、数据库或 Provider 参数。

## 4. Synthetic 回归覆盖

35 项离线测试覆盖（含主车道 Review 新增的代码复用跨期回归、PIT 时间链负例、UNKNOWN 风险不降级回归）：

- 普通上市、代码变化但身份稳定、代码复用产生新身份、连续性无法证明时失败；
- SSE/SZSE 独立 Universe；
- ST -> NORMAL、NORMAL -> ST、长期停牌、复牌、盘中临停/复牌、未知状态；
- 退市整理、正式退市、INCLUDED -> EXCLUDED、重新上市；
- 当前列表不推断历史退出、退市证券保留在历史 Snapshot；
- 未来更正不改变过去 Snapshot；
- 相同 `known_at + revision` 冲突、缺 identity、缺 daily status、重复行；
- 输入顺序随机化不改变 fact/snapshot identity；
- artifact/descriptor source/version、SHA-256、strict UTF-8；
- `complete=true` / `verified` 自我升级尝试；
- 退市闭环、数量连续性、未解析附件和跨源冲突报告；
- CLI 只生成 candidate JSON/JSONL 与 coverage report。

fixture 是 synthetic official-like 工程样例，不是 SSE 真实数据，不证明覆盖、许可、真实策略表现或 T2 已晋级。

## 5. 本轮真实验证

```text
python -m unittest discover -s tests_quant -p "test_security_universe_adapter.py" -v
  PASS: 35 tests

python -m compileall -q stock_tracker tests tests_quant scripts
  PASS

python scripts/import_a_share_identity.py \
  --artifact tests_quant/fixtures/security_universe/golden_sse.json \
  --descriptor tests_quant/fixtures/security_universe/golden_sse.descriptor.json \
  --output-dir <temporary-smoke-directory>
  PASS: 6 files; 4 identities / 15 statuses / 8 memberships
  complete=false; verified=false; database_modified=false; T3_NOT_REACHED
```

Python LSP diagnostics 对 Adapter、CLI 和测试文件均为 clean。JSON LSP 因本机 `biome` 未安装且此前已拒绝安装而不可用；strict JSON parser、descriptor hash 和定向测试已覆盖 fixture 语法与内容身份。

最终 `git diff --check` 与未跟踪 Agent C 文件的补充 whitespace 检查结果见本轮最终汇报。

## 6. 未修改与安全边界

- 未修改 `stock_tracker/quant/core/**`、`stock_tracker/quant/storage/**`、`stock_tracker/quant/data/__init__.py`；
- 未修改 Agent B/D 文件和 Stage 1.1 并行文件；
- 未读取或修改 `data/stock_tracker.db`；
- 未联网、未访问真实 SSE/SZSE/ChinaClear 数据；
- 未 git add、commit、merge 或 push。

## 7. Agent D 接入说明与已知限制

**可以交给 Agent D：YES（Agent C 侧）。** Agent D 应优先读取 candidate JSON/JSONL envelope，而不是只读取 core dataclass，因为以下字段当前不在共享 core 类型中：

```text
source_security_id
name / board
source_published_at / source_published_granularity
observed_at / retrieved_at
supersedes
INTRADAY effective_start / effective_end
```

Agent D 必须：

1. 将 `required_session_dates` 与 Agent B Calendar/会话集合对账，不能信任 artifact 自报日期范围来证明连续性；
2. 将 `coverage_report` 的任何缺口作为 `HARD_BLOCK` 或 `TRUST_BLOCK`，不能反向升级；
3. 对盘中状态读取完整 status candidate；不能只看 `core_statuses`；
4. 保持 SSE/SZSE 分开对账，合并由 validation/Manifest 层完成；
5. 对真实 artifact 继续要求许可、退市逐证券闭环、总量连续性和跨源 reconciliation。

主车道 Review 已修复此前的跨期 core 限制：`HistoricalUniverse.snapshot()` 现在只要求 INCLUDED 成员拥有目标日 active identity + target-session status；EXCLUDED 历史身份绑定退出日有效 identity 与不晚于退出日的最后可见 status。因此“旧证券退市后同一代码被新证券复用”可以同时保留旧 `instrument_id` 的退出证据和新 `instrument_id` 的当前成员身份，不再要求为旧证券虚构退市后的每日 status。`delisted_instrument_ids` 用于在代码复用时避免仅看 symbol 产生歧义。

Coverage Report 也区分 `has_snapshot_blockers` 与 `has_trust_blockers`。后者始终至少包含 `ADAPTER_UNVERIFIED_INCOMPLETE`、`SOURCE_SECURITY_ID_STABILITY_UNPROVEN` 和 `UPSTREAM_RAW_PROVENANCE_INCOMPLETE`，并显式列出 `trust_blocker_codes`；Agent D 必须消费这些原因码，不能把“candidate snapshot 可构造”误判为“Trust 已闭环”。

这两个默认 provenance blocker 是有意设计：字段名叫 `source_security_id` 不代表来源合同已证明它是跨代码变更/重新上市稳定的永久 ID；checksum-bound JSON 也只是规范化 candidate artifact，不等同于 SSE/SZSE/CNINFO/ChinaClear 的 exact upstream raw。只有后续 validation/reconciliation 持有独立证据时才能在外层报告中标记这些 blocker 已关闭，Agent C 本身不能关闭它们。

因此当前可进入 Agent D reconciliation，但不能进入 T3 Manifest、正式回测、训练、校准或 Replay 发布。
