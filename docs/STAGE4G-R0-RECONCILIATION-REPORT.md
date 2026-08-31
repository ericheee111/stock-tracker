# Stage 4G-R0 — Manual Outcome Collection Core Reconciliation Report

状态：`ENGINEERING_REVIEW_PASSED / GIT_DELIVERED`

日期：2026-08-31

## 1. 目的

Stage 4G 原提交 `a075026` 被错误表述为完整 Runtime Outcome Collection/Finalization Service。实际实现只是受控调用方可驱动的 manual Collection/Finalization Core。本轮 R0 不扩张到自动 Runtime Adapter、Broker、Trusted Admission 或 Scoreboard，而是将代码、测试和文档收敛到同一真实边界，并关闭已确认的金融正确性、PIT、身份、并发和恢复缺口。

允许的阶段结论仅为：

```text
STAGE4G_MANUAL_COLLECTION_CORE = ENGINEERING_REVIEW_PASSED
AUTOMATIC_RUNTIME_COLLECTION_ADAPTER = NOT_IMPLEMENTED
BROKER_EXECUTION_CAPTURE = NOT_IMPLEMENTED
TRUSTED_OUTCOME_ADMISSION = NOT_IMPLEMENTED
REAL_STRATEGY_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
INVESTMENT_PERFORMANCE_CLAIM = FALSE
AUTO_TRADE = FALSE
```

## 2. Schema 与迁移决策

本轮采用显式语义版本升级：

```text
Collection Store: stage4g-outcome-collection-v3
Exit Request: stage4g-outcome-exit-request-v3
Exit Decision Snapshot: stage4g-exit-decision-snapshot-v3
Exit Path Prefix: stage4g-exit-request-path-prefix-v3
Collection Case Projection: stage4g-outcome-collection-case-v3
Collection Audit: stage4g-outcome-collection-audit-v3
```

原因：

- v1 缺少外部 episode occurrence identity，并使用全局 `fact_id` 唯一性；
- v2 的退出前缀只绑定 `point_id + observed_at`，未绑定完整 PATH event `fact_id`；
- v2 terminal reason 仍是 any-touch，不能证明 first-touch 顺序。

v1/v2 evidence 不得静默重算、改写或解释成 v3。打开旧 Collection Catalog 时保持文件不变并失败关闭；未来如需保留本地实验 evidence，必须使用单独、可审计的 migration artifact。

## 3. 已闭环 Findings

| ID | Severity | 问题 | 闭环结果 |
|---|---|---|---|
| R0-01 | CRITICAL | 完整 Runtime Service 过度声明 | 阶段状态修正为 manual core；自动 Adapter、worker、Broker、Authority 和 Scoreboard 明确未实现 |
| R0-02 | CRITICAL | runtime `signal_id` 长期复用，不能唯一标识 episode | 显式外部 `runtime_episode_fact_id`；相同 fact 下 snapshot drift 冲突，新 episode 必须有新 occurrence fact |
| R0-03 | CRITICAL | collector capture time 冒充 entry request time | `entry_requested_at` 与 `captured_at` 分离并进入不可变身份 |
| R0-04 | CRITICAL | naive datetime 进入正式量化身份 | Runtime、request、fill、path、exit 和 audit 时间全部要求 timezone-aware 并规范为 UTC |
| R0-05 | CRITICAL | 不完整 path 可标为 Complete | entry 到实际 exit session 必须连续 observable；缺失 session、exit 后 point、不可实现 fill/reference price 均失败关闭 |
| R0-06 | CRITICAL | partial entry 静默丢弃剩余委托 | 当前单腿合同要求 entry/exit 完整数量；partial/multi-leg 留给显式后续合同 |
| R0-07 | CRITICAL | `minimum_exit_session_offset` 被错误限制不大于 horizon | offset 与策略 horizon 分离；因停牌、未成交或规则导致的延迟 fill 和额外 holding sessions 必须保留 |
| R0-08 | CRITICAL | TARGET/STOP/TIMEOUT 使用 any-touch，可事后选择更有利结果 | 请求前且 horizon 内按严格路径顺序执行 first-touch；terminal reason 必须匹配首个水平障碍 |
| R0-09 | CRITICAL | 同一粗粒度 PATH point 同时触发 target/stop 顺序不可证明 | 失败关闭；只允许未来更细粒度、可审计路径或 Authority 接受的冻结 ambiguity policy 解锁 |
| R0-10 | CRITICAL | 请求后补录较早 market timestamp 可回填解释旧决策 | 退出请求冻结请求时已 durable append 的最大连续 PATH 前缀，并绑定 `point_id + fact_id + observed_at` |
| R0-11 | CRITICAL | Path 来源替换后 `point_id` 不变 | PATH event `fact_id` 覆盖 raw-bar snapshot 和 evidence IDs；前缀 v3 绑定完整 fact identity |
| R0-12 | CRITICAL | Collection DB 首次初始化和文件身份竞态 | 同目录完整临时 DB、fsync、no-overwrite hard-link 发布、打开前后 inode/device 重验 |
| R0-13 | IMPORTANT | 全库 `UNIQUE(fact_id)` 误阻断跨 case 合法事实复用 | 唯一范围改为 `(case_id, fact_id)` |
| R0-14 | CRITICAL | 并发 finalization 可写第二个 FINALIZED marker | 单事务重放；已完成时只核对 Stage 4F record identities 并返回幂等 |
| R0-15 | IMPORTANT | Ledger target 只绑定路径 | 同时绑定 canonical path、record-root/catalog filesystem identity 并重验 Stage 4F guards |
| R0-16 | CRITICAL | `dataclasses.replace()` 可局部伪造 Case 状态/证据 | Case 重新派生并核验状态、路径来源、前缀、Prepared Outcome 和 Finalized identities |
| R0-17 | IMPORTANT | SQLite trigger/view/generated column/WAL 可改变证据语义 | 精确 schema/index/hidden-column 审计，拒绝 trigger/view 和非 DELETE journal mode |
| R0-18 | IMPORTANT | Runtime Signal/ScoreSet 子类可覆写快照行为 | 只接受项目定义的 exact types，并执行稳定双快照检测 |
| R0-19 | IMPORTANT | 全局 Decimal context 可改变 Outcome/Scoreboard | 模块内固定高精度 `ROUND_HALF_EVEN` context；低精度 `ROUND_DOWN` 回归证明指标和 ID 稳定 |

## 4. 验证证据

Checkout 验证结果：

```text
Stage 4G focused:
60 passed + 11 subtests

Outcome / Stage 4F adjacent + source distribution:
116 passed + 114 subtests

Full Quant:
725 passed + 327 subtests

Full Runtime:
521 passed
1 expected live-service probe skipped
350 subtests

Source distribution / no tracked bytecode:
3 passed + 83 subtests

Today Mock UI:
17/17

Real temporary API/Web Today:
17/17

Portfolio CRUD:
13/13
```

同时通过：

- targeted Ruff；
- compileall；
- `pip check`；
- Quant contract smoke；
- synthetic fixture benchmark；
- production migration dry-run；
- Benchmark 未自动晋级 Challenger；
- `synthetic_fixture_only=true`；
- `investment_performance_claim=false`。

生产数据库验证前后 SHA-256：

```text
ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7
```

迁移结果：

```text
mode = DRY_RUN
database_modified = false
pending_count = 4
```

全量测试候选 tree（本段验证证据写回前）：

```text
655b3e421aa3f4f65a33f57d593e028e37974f9f
```

该 tree 导出到不含运行数据库的干净源码目录后通过：

```text
Targeted Ruff: passed
Outcome / Stage 4F focused: 114 passed + 31 subtests
Full Quant: 723 passed + 2 expected Git-checkout-only skips + 244 subtests
Full Runtime: 521 passed + 1 expected live-service probe skip + 350 subtests
Source distribution: 1 passed + 2 expected checkout-context skips
compileall: passed
Quant contract smoke: passed
synthetic fixture benchmark: passed, challenger not promoted
```

## 5. 本轮没有假装解决的事项

以下事项被明确冻结为下一阶段前置合同，而不是以字符串或错误枚举绕过：

- 自动 Runtime Decision/Transition Artifact；
- Runtime transactional outbox 与显式 Runtime migration；
- 系统生成的可信 episode occurrence；
- Market Event Store path worker、cursor、restart recovery 和 quarantine；
- `ENTRY_EXPIRED / USER_CANCELLED` 普通未成交终态；
- `SUSPENDED / NO_TRADE / MARKET_CLOSED / MISSING_DATA` 独立 path/session evidence；
- observation window start/end；
- partial fill aggregation 和原生多腿生命周期；
- Broker-confirmed read-only execution adapter；
- Trusted Outcome Admission Authority、非对称签名、撤销和外部时间锚；
- 真实 Strategy Scoreboard。

在这些门禁完成前，Live Manual 永远保持：

```text
BEST_EFFORT
verified = false
lane = LIVE_CANDIDATE
```

## 6. Git 边界

本轮提交只允许纳入 Stage 4G-R0 的核心实现、测试、设计、独立 Review、项目状态同步与后续 Stage 4G.1/4H 设计。并行 `web/**`、`qa/**`、截图、运行数据、数据库、缓存、临时文件和凭据不得进入 Git Index。

实现与独立 Review 提交：

```text
commit = e5aed357e6127345f3e3de0bab993dc5f895906c
tree = 20fad49d582dbc5401b2cc05aec01a67b22a4339
message = fix: close Stage 4G R0 review gaps
```

该实现 tree 即最终通过 Ruff、专项、完整 Quant/Runtime 与 compileall 的精确 staged tree。GitHub 三方 SHA 验证记录写入 `CHATGPT_HANDOFF.md` 与最终交付回复。
