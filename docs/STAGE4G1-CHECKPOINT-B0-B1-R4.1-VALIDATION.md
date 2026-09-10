# Stage 4G.1 R4.1：R4-01 scoped candidate

状态：`LOCAL_CANDIDATE / CHATGPT_REVIEW_PENDING`。仅 synthetic
`STRUCTURAL_FIXTURE`；不代表真实 Coverage、Shadow Acceptance 或 Trusted Admission。

## 范围与基线

- 分支：`agent/codex-stage4g1`。
- 基线：`1a7286d45942b6392f01ee050fdb7870f5cf3f1c`；开始时目标工作树干净。
- 审计输入：`D:\Projects\stock-tracker-review\stage4g1-b0-b1-r4-1a7286d\CHATGPT-R4-AUDIT.md`。
- 只闭环 R4-01；源码、测试和本文共三个文件。主工作树的既有修改未触碰。
- 未进入 Concrete B1、接线物理 Store、修改 Authority 或其他架构。

## 修复合同与源码复核

`TransportCoverageCertificate._epoch_state()` 对每个 LIVE manifest 的 epoch，
先按原始 `transport_append_order` 扫描连接生命周期，再计算 heartbeat/watermark
周期覆盖。合法前缀要求 CONNECTED 在前，匹配本 manifest subscription scope 的
ACK 在后，之后才允许 HEARTBEAT、CALLBACK_WATERMARK 和 QUEUE_WATERMARK。
ACK scope 为 null 或不匹配时也失败，后补正确 ACK 不会洗掉早先无效证据。

DISCONNECTED 后同 epoch 仅可追加 SESSION_CLOSED；不得继续 ACK/心跳/水位或
重新 CONNECTED。SESSION_CLOSED 后活动及重复 start 继续由原 Snapshot 合同拒绝。
查询区间内提前 DISCONNECTED/SESSION_CLOSED 不证明后续覆盖；终点的合法水位、
断开、关闭次序可以证明截止终点的结构合同。新 epoch 必须独立满足连接和 ACK。

原 Snapshot 的 append/hash 链、UTC observed_at 单调性、durable-known 单调性、
水位与损失计数检查保留。可构造但生命周期违规的 raw transport snapshot 仍可诊断，
certificate 固定返回 `INCOMPLETE_LIFECYCLE`，空事件 Selection 为 `EMPTY_NOT_PROVEN`。
输入未重排、未丢弃、未就地改写；既有 `_periodic()` 时间戳归并发生于顺序验证之后，
不能把逆序证据修成有效输入。此规则保守作用于冻结的整个 epoch，而非静默裁掉坏行。

仅递增实际变化的证书语义身份：

- `stage4g1-fixture-transport-liveness-v2`；旧 v1 policy 不接受。
- `stage4g1-transport-coverage-certificate-v2`。

Record/Snapshot 原始格式及其验证语义未变，继续保留原版本；Selection 通过
certificate ID 自动绑定新语义。Replay/Backfill 的现有 start/completed 合同未扩展。
生产修复无新依赖、无 I/O、无状态升级，最终判定仍绑定 exact frozen Event/Transport。

## 新增回归与失败留存

新增 `TestR41TransportLifecycle` 共 11 个测试方法，包括 subTest 场景：同时间戳
正序、ACK 在连接前、三种活动各自在连接/ACK 前、断开后活动/重连、关闭后活动、
终态在连接前、合法断开后关闭、新 epoch 自己的连接/ACK，以及空/错误 ACK scope
不能被后补 ACK 修复。负例只在 fixture 中按给定顺序重算连续 append/hash，使用公开
factory/`dataclasses.replace`；不以 `object.__setattr__` 伪造运行对象。

首轮 `red-r41.log`：8 个测试方法，13 个失败断言。12 个是基线错误证明 Coverage
的负例（含 subTest）；另 1 个是新增正例的查询窗口错误：辅助选择仅查 10 分钟，却
断言其证明窗口外的 session close。测试 helper 改为覆盖整个 manifest 区间后解决；
不把这一失败写成产品缺陷。所有失败和后续日志均保留，未覆盖原日志。

R4 审计记载的 WorkBuddy 早期 adjacent failure 仍是未归因历史证据；本轮绿色结果
不追溯证明其原因，也不把耗时归因于未经 profile 的 sleep/heartbeat 等待。
两个初始 Windows wildcard 搜索失败另存 `inspection-failures.log` 的 stderr 摘录。

## 当前验证

本轮运行于目标工作树，全部使用 `py -3.14`，unittest 均保留 `-v` 完整输出。

| 检查 | 结果 |
| --- | --- |
| Runtime Evidence / Path / Source 聚焦 | 218 tests，OK |
| `basedpyright --level error stock_tracker/runtime_evidence` 全包 | 0 errors / warnings / notes |
| 完整 Runtime 首轮 | 740 tests，1 error、1 skipped，exit 1；完整日志保留 |
| 上述 ERROR 定点复跑 | 1 test，OK，exit 0 |
| 完整 Runtime 第二轮 | 740 tests，OK (skipped=1)，即 739 passed + 1 skipped |
| 完整 Quant | 724 tests，OK |
| Stage 4G/4F 相邻合同 | 114 tests，OK |
| source distribution / no tracked bytecode | 2 tests，OK |
| compileall、两份变更 Python 文件的 Ruff、pip check、diff check | exit 0 |
| Quant contract smoke / synthetic fixture benchmark | exit 0，仅合成工程证据 |

Runtime 首轮错误精确为
`test_hybrid_h3.TestHybridH3AuditAndTarget.test_remote_write_fails_closed_when_audit_is_unavailable`，
在 `_request()` 的 `connection.getresponse()` 读取 HTTP status 时抛出
`ConnectionAbortedError: [WinError 10053]`。保持相同源码和 runner 环境定点复跑通过，
继而完整 Runtime 第二轮通过；未改该测试、HTTP 服务代码或安全控制。失败原因仍未
归因，不能据复跑绿色声称它已被修复或已证实仅为环境问题。原始 `runtime-full.log`
与 `runtime-full-rerun-02.log` 分开保存。既有 ResourceWarning 与生产 :8080 不可达
导致的 live integration skip 保留，不声称该线上集成用例通过。

关键命令：

```text
py -3.14 -m unittest tests.test_runtime_evidence tests.test_runtime_path_contracts tests.test_market_source_snapshot_contracts -v
py -3.14 -m basedpyright --level error stock_tracker/runtime_evidence
py -3.14 -m unittest discover -s tests -p "test_*.py" -v
py -3.14 -m unittest discover -s tests_quant -p "test_*.py" -v
py -3.14 -m unittest tests_quant.test_outcome_collection tests_quant.test_outcome_ledger_codec tests_quant.test_outcome_ledger_store tests_quant.test_outcome_ledger_cli tests_quant.test_outcome_ledger_scoreboard tests_quant.test_outcomes -v
py -3.14 -m unittest tests_quant.test_source_distribution tests_quant.test_no_tracked_bytecode -v
py -3.14 -m unittest tests.test_hybrid_h3.TestHybridH3AuditAndTarget.test_remote_write_fails_closed_when_audit_is_unavailable -v
py -3.14 -m compileall -q stock_tracker tests tests_quant scripts
py -3.14 -m ruff check stock_tracker/runtime_evidence/source_snapshot_contracts.py tests/test_market_source_snapshot_contracts.py
py -3.14 -m pip check
py -3.14 scripts/run_quant_contract_smoke.py
py -3.14 scripts/run_quant_fixture_benchmark.py
```

两份源码/测试文件在最终聚焦、全包类型检查、完整回归及 Runtime 复跑期间 SHA-256
保持不变。`gate-summary.json` 保留首轮失败；`validation-summary.json` 汇总所有轮次。

## 数据与交付边界

生产 DB/WAL/SHM 开始、门禁前后与 Runtime 复跑后 SHA-256 一致；仅作文件 SHA-256
对比，未连接或查询交易数据。测试/Smoke 使用
临时数据；未执行生产 migration 或 `--apply`，未调用真实 Provider。
外部 `commands.jsonl` 保存 argv、cwd、耗时、退出码和日志 SHA-256；pycache 在外部。
完整日志位于 `D:\Projects\stock-tracker-review\stage4g1-r4.1-run-20260910\`。

| 生产文件 | 起始及验证后 SHA-256 |
| --- | --- |
| stock_tracker.db | `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7` |
| stock_tracker.db-wal | `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e` |
| stock_tracker.db-shm | `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3` |

隔离工作树下三个对应运行文件开始及验证后均不存在。提交不包含运行数据或日志。

交付时使用本地 scoped commit；无 merge/push/历史重写。候选包按最终 SHA 命名，
包含 exact committed-tree ZIP/导出、逐文件 blob/SHA-256、原审计与本轮证据。
独立 ChatGPT 复审之前，停在 `CODEX_CHECKPOINT_READY:B0_B1_R4_1`。
