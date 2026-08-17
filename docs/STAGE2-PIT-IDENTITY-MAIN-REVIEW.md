# Stage 2A PIT / Identity 主线最终 Review

> 日期：2026-08-17
> Review：ChatGPT 5.6 Sol Pro + CodexPro 主线集成审查
> 工程 Verdict：`ENGINEERING_READY_FOR_MAIN_REVIEW`
> 证据 Verdict：`EVIDENCE_TIER_STATUS = T3_NOT_REACHED`
> 许可：`LICENSE_PENDING`

## 1. 审查范围

本次主线 Review 覆盖：

- Calendar exact-raw capture、raw descriptor、parse descriptor 与 deterministic replay；
- Calendar `known_at / usable_from` 与显式 supersedes revision graph；
- Security Identity、Status、Historical Universe；
- Reconciliation findings、Trust blocker governance、coverage metrics 与 `report_id`；
- migration 0003 的 append-only、predecessor persistence 与 dry-run；
- Agent A/B/C/D Handoff、WorkBuddy synthetic fixtures；
- 两轮独立 Reviewer 的全部 findings；
- Stage 1.1 私有 API、fetch-SSE 与 Portfolio UI 的混合工作树兼容性。

## 2. 独立 Review findings 关闭情况

### 第一轮 Review

| Finding | 状态 | 主线修复 |
|---|---|---|
| 无证据 `known_at` 回填 | `CLOSED` | 真实 Calendar 强制 `observed_at = known_at = retrieved_at`；CLI 删除裸时间覆盖；非 synthetic Security/Universe 同样绑定 descriptor retrieval |
| Calendar core / Reconciliation revision 结论分裂 | `CLOSED` | `CalendarDay` 保留 predecessor；core 与 Reconciliation 共用显式 supersedes graph resolver |
| `dataclasses.replace()` 删除 HARD_BLOCK | `CLOSED` | findings、blockers、coverage、gaps、tier 字段全部 `init=False`，只能从规范输入重新派生 |
| future Security/Universe 污染历史 as-of | `CLOSED` | descriptor 与 candidate 均做严格 as-of visible projection；未来 bundle 不进入历史 counts/findings |
| 断开同 payload cycle 未检测 | `CLOSED` | resolver 在 terminal 选择前遍历全部 visible nodes，cycle/missing predecessor/multiple terminal 全部失败关闭 |

### 第二轮 Review

第二轮新增 `IMPORTANT-01`：不同 `source_family` 恰好使用相同 `source_version` 时，会被错误合成一个 Calendar revision graph。

修复后：

```text
Calendar stream identity
= (exchange, source_family, source_version)
```

- 同 family 内 version/parser mixing 继续 HARD_BLOCK；
- 不同 family 即使版本字符串相同，也先独立解析各自 revision graph；
- terminal 相同，不产生虚假 branch conflict；
- terminal 不同，在独立解析后产生真正的 `CALENDAR_OPEN_CLOSED_CONFLICT` / `CALENDAR_SESSION_CONFLICT`；
- input/finding/report identity 绑定 family；
- Adapter 拒绝跨 family assembly；
- `CalendarCoverage.source` 和 `CalendarDay.source` 保留 `owner/family`，进入 core 后不再降维。

第二轮 `MINOR-01` 同时关闭：migration 回归已覆盖合法 predecessor 插入/readback、predecessor UPDATE/DELETE append-only、NULL pair guard 与非规范 INTEGER `"01"` 拒绝。

## 3. 金融正确性结论

当前工程合同已经对以下高风险错误失败关闭：

- look-ahead / first-known backdating；
- current-list backfill 和 survivorship bias；
- absence 被误解释为 EXCLUDED；
- 退市样本删除或要求伪造退市后每日 status；
- symbol 作为永久身份；
- code reuse / relisting / symbol rename 错误合并；
- SSE/SZSE Calendar 串用；
- revision lexical ordering、cycle、missing predecessor 和 disconnected terminal；
- source family/version stream 降维；
- future candidate 污染历史 coverage；
- synthetic agreement 被当作 corroboration；
- self-asserted closure 关闭 Trust blocker；
- 直接构造/replace 伪造 ReconciliationReport；
- migration predecessor round-trip 丢失。

未发现剩余工程 merge blocker。

## 4. 最终验证

```text
Calendar Adapter targeted       25 / 25 PASS
Security/Universe targeted      36 / 36 PASS
Reconciliation targeted         37 / 37 PASS
Migration targeted              15 / 15 PASS
Full Quant                      298 / 298 PASS
Runtime                         341 PASS
                                1 existing localhost :8080 probe skipped
Stage 1 Today real API/Web       17 / 17 PASS
Stage 1 Portfolio CRUD           13 / 13 PASS
compileall                       PASS
Quant contract smoke             PASS
Synthetic fixture benchmark      PASS
pip check                        PASS
D-lane Ruff                      PASS
git diff --check                 PASS
```

Synthetic benchmark 仍然明确：

```text
synthetic_fixture_only = true
investment_performance_claim = false
promoted = false
reasons = [ECE_REGRESSED, TIME_INSTABILITY]
```

Migration：

```text
mode = DRY_RUN
applied_count = 0
pending_count = 3
database_modified = false
```

生产数据库 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

验证后保持不变。

## 5. 证据等级边界

工程可合入不等于真实数据晋级。以下仍未闭合：

- SSE/SZSE/ChinaClear/CNINFO 的书面许可；
- 完整历史 upstream exact raw；
- 完整 revision history；
- 逐证券退市闭环与数量连续性；
- `source_security_id` 官方稳定身份合同；
- Corporate Action PIT 与 adjustment identity；
- 行业/板块历史成分；
- T3 joint Manifest 与独立晋级流程。

因此必须继续保持：

```text
LICENSE_PENDING
T3_NOT_REACHED
CONTRACT_ONLY / SYNTHETIC_VALIDATED / T2_CANDIDATE_EVIDENCE
```

## 6. 主线结论

```text
ENGINEERING_READY_FOR_MAIN_REVIEW
EVIDENCE_TIER_STATUS = T3_NOT_REACHED
```

允许提交：代码合同、Adapter、Reconciliation、migration、synthetic fixtures、测试和审计文档。

禁止声明：T2/T3 已达成、RESEARCH_GRADE、真实回测有效、真实模型可训练、真实概率可上线或可实盘。

## 7. 下一阶段

下一阶段选择 `Stage 2B：Corporate Action / Adjustment Identity Foundation`。

原因：拆股、送转、配股、分红、复权因子和已知时间错误会同时污染价格序列、标签、特征、回测、仓位和策略战绩；它是进入真实 EOD snapshot、Replay、Big Trend 和模型训练前的下一项共同基础。
