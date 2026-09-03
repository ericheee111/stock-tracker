# Stage 4G.1 Checkpoint A — ChatGPT Adversarial Review

状态：`A4_COMMITTED_AND_PASSED / B0_B1_R1_CANDIDATE_COMMITTED / WORKBUDDY_R1_REVIEW_PENDING`

日期：2026-09-03

## 1. 审查对象

| 项 | 值 |
|---|---|
| A3 Commit | `3e6ad3f73c78f5d4ddd8f0537250ba5b275bd0f2` |
| A3 Tree | `2388299c5d9cab60d74eee1b78c99e630afdc5fe` |
| Parent | `f8b278a7f9464e012b263066d51080a2857aa58f` |
| Base | `082a6dac310388ec10c8a432427a40e275bcd7ae` |
| Branch | `agent/codex-stage4g1` |
| Remote state | 未 push |

A3 的目标是关闭 A2 中的 Delivery Target Binding、全局 Integrity Block、Signal History 引用完整性、PIT 字段、Legacy migration 与 upstream occurrence gate。

## 2. A3 通过的设计项

经源码级复审，以下方向成立：

- Runtime DB 和 Artifact Store 使用显式 `runtime_store_id → artifact_store_id` binding；
- 全局 Integrity Event 以 delivery binding 为作用域，不再依赖 `worker_id` 作为唯一权威；
- Delivery 记录绑定 Artifact Store ID、append order、record hash、audit ID 与时间；
- Store Integrity/长期可用性故障不再把合法 occurrence 当作 payload poison quarantine；
- SQLite Runtime 连接启用 foreign keys，并审计 `foreign_key_check`；
- occurrence 绑定 `signal_history_id` 与 `signal_history_sha256`；
- 被 Runtime Evidence 引用的 Signal History 不能 UPDATE/DELETE；
- occurrence 有本地 append-order/hash chain；
- Artifact v4 明示 `RUNTIME_MEMORY_ONLY`、`BAR_KNOWN_AT_AUTHORITY_PENDING`、单活和本地 fork 边界；
- `ENTRY_EXPIRED/USER_CANCELLED` 尚未被错误映射到 `DATA_INVALID`；
- v1/v2 非空 legacy Runtime Evidence 普通迁移失败关闭；
- migration 报告将 target state、latest readiness 与 rehearsal 分开；
- 不存在 XTP/Broker 写面，`auto_trade=false`；
- 不存在 Trusted Admission 或真实 Scoreboard 声明。

## 3. WorkBuddy 机械复跑失败的裁定

WorkBuddy 在 `git archive` 导出目录中运行完整套件，报告：

```text
Runtime: 577 tests, errors=2
Quant:   724 tests, errors=1
```

三个错误均出现在 Windows 临时目录或 symlink 测试的清理阶段，并包含由外部 safe-delete/trash overlay 产生的 COM 错误：

```text
HRESULT(0x80040154) / class not registered
```

在原 A3 Git 工作树、同一 Python 3.14 解释器下复跑：

```text
py -3.14 -m unittest discover -s tests -p "test_*.py" -q
→ Ran 577 tests
→ OK (skipped=1)

py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -q
→ Ran 724 tests
→ OK
```

因此这三个错误裁定为：

```text
RUNNER_ENVIRONMENT_OVERLAY_FAILURE
NOT_A_STABLE_PRODUCT_REGRESSION
```

但验证流程必须修正：

1. Git tracking/source-distribution 检查在真实 Git checkout 中运行；
2. archive/import 检查在无 safe-delete overlay 的中性目录运行；
3. `compileall` 明确会生成 bytecode，不能与“生成后立刻要求目录无 pyc”混为同一阶段；
4. bytecode scan 在独立 export 或清理后的目录执行；
5. 固定解释器为 Python 3.14，并记录版本与 tzdata 环境；
6. WorkBuddy 不得把 runner cleanup failure 直接判为工程代码 failure。

WorkBuddy 报告的 source-distribution `skipped=1` 也符合预期：该测试显式要求 `.git`，而 `git archive` 不包含 `.git`。

## 4. A3 后发现的阻断问题

### A3-R1 · CRITICAL · Artifact Store 的 source Runtime identity 未在领取前验证

A3 Worker 原流程只向 Repository 提供：

```text
artifact_store_id
```

但没有在建立 delivery binding 和领取 lease 前证明：

```text
ArtifactStore.source_runtime_store_id
== Runtime DB runtime_store_id
```

错误装配序列：

```text
Runtime DB R1 contains valid occurrence
Artifact Store S2 declares source_runtime_store_id=R2
Worker starts with R1 + S2
Repository binds R1 → artifact_store_id(S2)
Worker claims legal row
Store rejects artifact.runtime_store_id=R1 against S2.source_runtime_store_id=R2
old classification treats mismatch as ROW_PERMANENT
legal occurrence can be quarantined and cursor can advance
```

这违反：

- 合法 occurrence 不能因 lane/configuration 错误被当作 poison；
- delivery binding 必须同时证明源 Runtime 与目标 Artifact Store；
- Store Identity mismatch 必须是全局 Integrity Block。

## 5. 已提交的 A4 修复

A4 修复已由 scoped commit `728835c493642488044df68700f8dfbf35fde5df` 固化：

```text
RuntimeOutboxLease.runtime_store_id
claim_runtime_outbox(source_runtime_store_id=...)
Runtime DB store ID 与 Artifact Store source ID 的领取前比较
lease binding 同时验证 runtime_store_id + artifact_store_id
Artifact Store runtime binding mismatch → STORE_INTEGRITY_BLOCK
```

稳定错误码：

```text
ARTIFACT_STORE_SOURCE_RUNTIME_MISMATCH
ARTIFACT_STORE_RUNTIME_BINDING_MISMATCH
ARTIFACT_STORE_BINDING_MISMATCH
```

错误装配现在必须：

```text
HARD_BLOCKED
outbox delivery remains PENDING
no delivery binding inserted
no quarantine inserted
cursor unchanged
```

新增专项测试：

```text
test_worker_rejects_source_runtime_store_mismatch
```

当前本地专项结果：

```text
py -3.14 -m unittest tests.test_runtime_evidence -q
Ran 56 tests
OK
```

B0/B1 R1 进一步关闭 entry-boundary causality、multi-authority PIT、projection lineage、market-local session date、finding completeness 和 scalable snapshot commitment。当前 A4+B0+B1 合并专项为：

```text
py -3.14 -m unittest \
  tests.test_runtime_evidence \
  tests.test_runtime_path_contracts \
  tests.test_market_source_snapshot_contracts -q
Ran 125 tests
OK
```

A4+B0+B1 R1 后完整 Runtime 稳定复跑：

```text
py -3.14 -m unittest discover -s tests -p "test_*.py" -q
Ran 647 tests
OK (skipped=1)
```

完整 Runtime 运行仍可能在 stderr 记录本地测试客户端主动断开的 `ConnectionAbortedError`，但进程退出码为 0、647 项断言全部通过；该日志不代表业务失败。

完整 Quant 当前为 724 项通过；`tests_quant.test_source_distribution + tests_quant.test_no_tracked_bytecode` 为 2 项通过。B0 R1 commit 为 `7a49222c73ca0a9800b2aec8d2c07450e195cbca`，B1 R1 commit 为 `ffb0441a21b9ff60b28ae2adeb393517c8301cab`。

## 6. A3/A4/B0/B1 当前门禁

A3/A4 不再需要架构性推倒重来。当前剩余工作属于：

- Codex 完成 B0/B1 R1 文档与精确 final-tree 证据；
- WorkBuddy 对同一 commit/tree 做独立机械复跑；
- 只有 WorkBuddy 明确通过后才能进入 B2 storage/worker wiring。

在这些完成前：

```text
A4_SOURCE_RUNTIME_BINDING_PASS = true
CHECKPOINT_B_STORAGE_WIRING_ALLOWED = false
PUSH_ALLOWED = false
```

B0 的纯合同设计与实现可以并行完成，因为它不写 Store、不打开 Case，也不改变 A3 数据库。

## 7. 接受的阶段残余

以下不阻断 A4/B0 checkpoint，但必须在后续阶段明确关闭：

| 残余 | 关闭阶段 |
|---|---|
| Artifact Store append/full-audit 的 O(N²) 扩展性 | D scale harness 前 |
| 全局 integrity block 无自动解除仪式 | D/Operations ADR |
| 外部 fork checkpoint/adjudication 不存在 | Stage 4H |
| Bar known-at authority 不存在 | B1/B2 只能保持 candidate/blocked 语义 |
| Production Runtime migration 未 apply | 独立运维授权后 |
| 真实多交易日 Shadow Acceptance 未完成 | D |
| Trusted Outcome Admission 未实现 | Stage 4H |
| 真实 Strategy Scoreboard 未实现 | Stage 4I |

## 8. 最终结论

```text
A3_ARCHITECTURE = SUBSTANTIALLY_CORRECT
A3_STABLE_FULL_SUITE_REGRESSION = NOT_FOUND
A3_RUNNER_PROTOCOL = NEEDS_CORRECTION
A3_SOURCE_RUNTIME_BINDING = GAP_CLOSED_BY_A4
A4_SOURCE_RUNTIME_BINDING_FIX = COMMITTED_AND_PASSED
B0_EVIDENCE_AND_PATH_CORE_R1 = COMMITTED_CANDIDATE
B1_AUDITED_SOURCE_SNAPSHOT_CORE_R1 = COMMITTED_CANDIDATE
A4_B0_B1_FOCUSED_TEST = 125/125 PASS
A4_B0_B1_FULL_RUNTIME = 647 PASS / 1 EXPECTED SKIP
A4_B0_B1_FULL_QUANT = 724/724 PASS
B0_B1_R1_WORKBUDDY_REVIEW = PENDING
```
