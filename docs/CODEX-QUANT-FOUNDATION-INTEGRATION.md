# stock-tracker Quant Foundation 本机集成报告

> 日期：2026-08-13
>
> 工作区：`D:\Projects\stock-tracker`
>
> 分支：`main`
>
> Quant Foundation 基线：`93e1c94`
>
> 状态：v0.4 PRD 对齐；G0 source-distribution 修复；Wave 2B.1 第一切片实施中

## 1. 执行结论

Wave 1 / Wave 2A 已在真实本机工作区完成工程集成，并于 commit `93e1c94` 进入 `main`。其能力仍是量化合同与合成 fixture 证据，不代表真实投资表现。

后续 fresh-clone 审计发现：根 `.gitignore` 使用未锚定的 `data/`，误排除了 `stock_tracker/quant/data/`，使本机可以导入 Manifest、远端源码却缺包。v0.4 将此类问题升级为 G0 发布阻断项，并新增关键源码 `git ls-files` 回归门禁。

Wave 2B.1 第一切片继续保持运行缓存与研究存储隔离：Eastmoney K 线适配器拆分为 exact raw bytes 获取与确定性解析；原始响应先内容寻址落盘，再生成 `RawDataArtifact`、Trust Tier、normalized dataset fingerprint 和可重放 descriptor。该路径不会自动升级为 `RESEARCH_GRADE`，也不会修改生产 SQLite。

当前仍然禁止：

- 对 `data/stock_tracker.db` 自动应用量化迁移；
- 把页面 `bars` 缓存直接当作研究训练集；
- 接入自动下单；
- 用合成 fixture 声称任何真实胜率、收益率、Sharpe 或最大回撤；
- 仅通过修改 Trust Tier 或重算 descriptor ID 将单个公开源捕获自我升级为研究级数据。

## 2. 需求基准与交付包限制

本轮以以下本机文档为需求基准：

- `docs/PRD-股票辅助判断与交易参考网站.md`
  - 本轮读取时 SHA-256：`c348d49aeba43df8b068a003852f97a266924479acab771f7be54462d1f112cf`
- `docs/VALIDATED-STRATEGY-ML-LIBRARY.md`
- 用户提供的 Wave 1 / Wave 2A 完整阶段输出、模块清单、安全合同与验证说明。

上传的 ZIP 位于对话侧 Linux `/mnt/data`，而 CodexPro 工作区运行于 Windows 本机。该 ZIP 无法被字节级桥接到 `D:\Projects\stock-tracker`，因此本轮不是对 ZIP 的逐文件原样复制，而是根据 PRD 和阶段输出在真实仓库中重新实现、复核并验证。

这意味着：

- 已实现的合同和模块与阶段说明对齐；
- 不能声称本机文件与上传 ZIP 逐字节相同；
- 本机代码、测试、迁移和证据均已单独真实运行。

## 3. 新增工程结构

```text
stock_tracker/quant/
├── backtest/
│   ├── backtester.py
│   ├── costs.py
│   ├── execution.py
│   └── market_rules.py
├── core/
│   ├── calendar.py
│   ├── fingerprint.py
│   ├── point_in_time.py
│   ├── reproducibility.py
│   └── time.py
├── data/
│   ├── bar_artifact.py
│   └── manifest.py
├── evaluation/
│   ├── calibration.py
│   ├── holdout.py
│   ├── metrics.py
│   └── walk_forward.py
├── features/
│   ├── alpha158.py
│   ├── context.py
│   ├── metadata.py
│   ├── normalization.py
│   └── qlib_audit.py
├── labels/
│   ├── calendar_aware.py
│   └── triple_barrier.py
├── models/
│   ├── baseline.py
│   ├── comparison.py
│   ├── dataset.py
│   ├── diagnostics.py
│   ├── horizons.py
│   ├── lightgbm_meta.py
│   ├── protocol.py
│   └── registry.py
├── research/
│   ├── candidates.py
│   ├── experiments.py
│   └── leakage.py
├── storage/
│   ├── migrations.py
│   └── migrations/
│       ├── 0001_quant_foundation.sql
│       └── 0002_trusted_data_calendar.sql
└── config.py
```

此外新增：

```text
config/quant_wave1.toml
config/quant_wave2.toml
scripts/capture_quant_bars.py
scripts/quant_migrate.py
scripts/run_quant_contract_smoke.py
scripts/run_quant_fixture_benchmark.py
tests_quant/
docs/quant-contract-smoke.json
docs/quant-synthetic-fixture-benchmark.json
docs/quant-coverage.json
```

## 4. 已实现的关键合同

### 4.1 Point-in-Time 与时间语义

- 所有正式时间接口拒绝无时区 `datetime`；
- `known_at <= usable_from <= as_of`；
- revision 明确区分整数和字符串，SQL 使用 `revision_kind + revision_value` 保留原语义；
- 相同 `known_at + revision` 但 payload 不一致时失败关闭；
- Snapshot ID 绑定可见事实、截止时间和核验策略；
- 配置、集合、字典和浮点值使用稳定规范化哈希，NaN/Inf 被拒绝。

### 4.2 完整交易日历与 Session

- 日历覆盖区间必须包含每个自然日，明确标记 `OPEN` 或 `CLOSED`；
- A/HK/US 分别要求交易所本地时区；
- 同一标签窗口只能使用一个 Calendar Source/Version；
- 市场开市但无 Bar 时，必须有 PIT 可见且按策略核验的证券状态；
- 停牌/Halt/VCM/退市占位 Session 为零成交量、不可观察、不可成交；
- 未解释的缺 Bar、未来 revision、冲突 revision 和跨版本拼接全部失败关闭；
- 正式标签边界要求 `CalendarAlignedBars`，拒绝裸 `list[Bar]`。

### 4.3 执行引擎

- 统一 `next executable price`；
- A 股 T+1 使用实际 Session 下标；
- 涨跌停锁死状态缺失时失败关闭，不根据 OHLC 猜测；
- 停牌和零成交量不可成交；
- Market Rule、Instrument Rule 和 Cost Schedule 按日期选唯一核验版本；
- Gap-through 使用实际开盘可执行价；
- Spread、Slippage、Impact 和显式费用均进入成交合同；
- 模型成交价不会越出真实 OHLC；
- OHLC 截断后的隐含成本会按实际价格偏移同步缩放，成本报告与成交价一致；
- 当前账本明确为单标的执行账本，多标的输入直接拒绝，避免静默错配成交。

### 4.4 Target-Before-Stop / Triple Barrier

- 正式标签禁止 `BEST_CASE`；
- 支持：
  - `MARK_AMBIGUOUS`
  - `WORST_CASE`
  - 已核验低周期数据解析
- 第一项 Barrier 一旦触发，即使因 T+1、跌停锁死或停牌暂不可成交，也不能被后续另一 Barrier 改写；
- 停牌占位不触发 Barrier，也不污染 MFE/MAE；
- `label_end_time` 使用真实结果完成时间；
- 二元目标必须精确为 0/1，不允许 `0.9 -> int(0)` 式静默截断。

### 4.5 原始数据 Manifest

每个 Artifact 绑定：

- 数据类型、格式和市场；
- Provider、数据集、Provider/Schema/Adapter 版本；
- 规范化逻辑 `storage_key`；
- SHA-256、字节数、结构化行数；
- 内容时间范围和获取时间；
- `known_at` / revision 策略；
- 核验状态、来源说明和 Calendar Snapshot。

安全约束：

- 拒绝绝对路径、盘符、`..`、URL、query、凭证式语法和反斜杠；
- 拒绝根目录或任一路径组件经过 symlink/junction；
- 同尺寸文件被修改也能通过 SHA-256 发现；
- Artifact 获取时间不能晚于 Snapshot 创建时间；
- 市场数据默认要求 Calendar 绑定；
- 证券集合相关数据默认要求 PIT Universe 绑定；
- 未核验 Artifact 不能进入正式核验 Snapshot；
- Manifest JSON 读取时重新计算 Artifact ID 和 Snapshot ID；
- JSON 使用临时文件、flush、`fsync` 和原子替换写入。

### 4.6 时间序列评估与校准

- Expanding / Rolling Walk-Forward；
- 显式 gap、label-overlap purge 和 embargo；
- 禁止随机 K-Fold 作为正式金融时序验证；
- 校准样本按真实 `label_end_time` 截断，不按 signal time 偷看未结束标签；
- 纯 Python Platt 与 Isotonic 校准；
- Frozen Holdout 首次暴露必须匹配预封存配置哈希和数据 Snapshot；
- 不匹配时先持久化为不可逆 `COMPROMISED`，再抛错。

### 4.7 模型与晋级治理

- 生产基线：纯 Python Logistic Regression；
- 标准化参数仅在训练集拟合；
- LightGBM 为可选 Challenger，不是运行必需依赖；
- 完整公平比较身份绑定：
  - Train Dataset；
  - Calibration Dataset；
  - Validation Dataset；
  - Feature/Label Version；
  - Market Rule / Cost Schedule；
  - Calibration Definition / Window；
  - Top-K 定义；
  - Random Seed。
- Champion Gate 检查：
  - Brier；
  - LogLoss；
  - ECE；
  - Precision@K；
  - Top-K 净期望；
  - 最大回撤；
  - 分数桶单调性；
  - 跨 Regime 稳定性；
  - 跨时间稳定性。
- NaN/Inf 不能绕过晋级判断；
- 模型概率只是 Advisory，仍必须经过规则信号、风险闸门和数据质量闸门。

### 4.8 特征与 Qlib 边界

- 固定 158 个因果 Price/Volume 特征；
- 特征计算上下文拒绝 `as_of` 之后的 Bar；
- Train-only 标准化；
- PIT 同时点横截面排名；
- Feature Family、相关性、Permutation、Ablation 接口；
- 明确命名为 `Alpha158-style`，未声称与 Microsoft Qlib 数值等价。

当前 Qlib 审计仍保留 3 个 BLOCKER：

1. Corporate-action Golden Mapping；
2. Exact Qlib Revision Pinned；
3. Golden-data Numerical Equivalence。

### 4.9 持久化与迁移

- 两版 checksum-verified SQLite 迁移；
- 默认 CLI 为 dry-run；
- 必须显式提供数据库路径；
- 对已有数据库，dry-run 使用 SQLite 只读 URI；
- `--apply` 才会创建或修改所选数据库；
- 每版迁移独立事务，失败整版回滚；
- 已应用 migration 的 name/checksum 与源码不一致时拒绝继续；
- PIT、标签、Artifact、Snapshot、日历、状态、Model Registry、Experiment、Holdout 等表均有 append-only UPDATE/DELETE Trigger；
- 本轮迁移只在内存库和临时数据库执行，未应用到 `data/stock_tracker.db`。

## 5. 本机验证结果

### 5.1 编译与运行入口

```text
python -m compileall -q stock_tracker scripts tests_quant     PASS
python -m stock_tracker --help                               PASS
```

### 5.2 测试

```text
Quant Foundation tests_quant       131 passed
原项目 tests                         145 passed
现有 qa/unit                           3 passed
总计                                 279 passed
失败                                   0
```

### 5.3 静态检查

```text
Ruff targeted check                 PASS
Ruff E501 line-length check         PASS
BasedPyright                        0 errors
```

BasedPyright 仍会报告动态 JSON、SQLite、可选 LightGBM 和测试框架边界的提示级 warning；未通过关闭检查规则来掩盖错误。

CodexPro 的命令安全守卫会把 `ruff format` 和 `ruff format --check` 误判为高风险写操作，因此本轮没有声称 Ruff Formatter 已运行。Ruff lint 与独立 E501 检查均已真实通过。

### 5.4 覆盖率

已使用 branch coverage 对 `stock_tracker.quant` 执行完整 `tests_quant`，机器可读结果位于：

```text
docs/quant-coverage.json
```

报告保留逐文件 statement、missing line 和 branch 数据，避免在本文件手工抄写后产生漂移。

## 6. 合同 Smoke 结果

证据：`docs/quant-contract-smoke.json`

核心结果：

```text
synthetic_fixture_only                 true
investment_performance_claim           false
unsafe_raw_bar_label                   TP_FIRST
calendar_aware_label                   TIMEOUT
calendar_fix_prevents_horizon_drift    true
same_size_tamper                       true
tamper_detected                        true
migration_count                        2
pending_count                          0
production_database_modified           false
future_feature_flagged                 true
high_probability_bypasses_risk_gate    false
passed                                 true
```

该证据证明的是工程合同和失败关闭行为，不是投资表现。

## 7. 合成模型基准

证据：`docs/quant-synthetic-fixture-benchmark.json`

数据划分：

```text
Train          432
Calibration    144
Validation     144
```

校准后结果：

| 指标 | Logistic Champion | Interaction Challenger |
|---|---:|---:|
| Brier | 0.193859 | 0.146002 |
| LogLoss | 0.574998 | 0.453043 |
| ECE | 0.103938 | 0.124177 |
| Precision@30 | 0.900000 | 0.966667 |
| Top-K Net Expectancy | 0.992616 R | 1.106746 R |

Challenger 虽然提升了 Brier、LogLoss、Precision@K 和 Top-K 净期望，但 ECE 回退且跨时间稳定性未通过，因此：

```text
promoted = false
reasons = [ECE_REGRESSED, TIME_INSTABILITY]
```

这说明晋级系统不会因排序收益更好就忽略概率质量和稳定性。

当前环境没有 LightGBM，故 LightGBM Candidate 未评估；Logistic 基线和全部治理测试不依赖 LightGBM。

## 8. 负面对照

合成 Future Feature 使用未来标签构造，得到异常漂亮的 Brier，并被明确标记：

```text
future_feature_flagged          true
suspicious_advantage_detected   true
```

该结果只用于证明泄漏检测能够识别“异常优秀”，不能被解释为模型能力。

## 9. 现阶段不能宣称的内容

当前仍没有：

- 权威 A/HK/US 历史行情 Artifact；
- 权威交易日历 Provider 与修订历史；
- 完整证券停牌、Halt、退市状态历史；
- PIT 历史 Universe 与退市样本；
- Corporate Action Golden Mapping 和完整复权序列；
- 用户真实券商费用、税费与容量模型；
- 真实 Walk-Forward / Frozen Holdout 结果；
- 真实成功率、年化收益、Sharpe、最大回撤；
- Qlib Golden Data 数值等价；
- 完整多标的 Portfolio Ledger；
- 跨进程 JSONL 文件锁；
- 自动下单或实盘交易能力。

## 10. 下一阶段：Wave 2B

下一阶段应优先打通可信数据，而不是继续增加更复杂的模型：

```text
Authoritative Provider
  -> Immutable Raw Artifact
  -> SHA-256 Manifest
  -> Exchange Calendar / Instrument Status / PIT Universe
  -> Corporate Action / Adjustment
  -> PIT Fact Store
  -> Golden Cases
  -> Real Logistic Baseline
  -> Purged Walk-Forward
  -> Frozen Holdout
  -> LightGBM Challenger
  -> Shadow / Paper Execution
```

实施状态与推荐顺序：

1. **Wave 2B.1a 已实现**：Eastmoney Provider 的 exact raw fetch / deterministic parse 分离；内容寻址 Raw Artifact；descriptor 绑定端点、复权模式、请求起止范围和 parser version；Trust Tier；重放与篡改检测；独立捕获 CLI。当前默认等级仍是 `BEST_EFFORT`；
2. **Wave 2B.1b 待完成**：为 A/HK/US 建立版本化 golden raw payload、跨源 reconciliation 和抓取覆盖率/缺口报告；
3. **Wave 2B.2 待完成**：接入交易日历、停牌状态、历史 Universe 和公司行为，并组装真正的 `RESEARCH_GRADE` Snapshot；
4. 建立 A/HK/US 市场规则与公司行为 Golden Cases；
5. 运行真实 Logistic 基线与负面对照；
6. 首次封存 Frozen Holdout；
7. 最后才评估 LightGBM 和 Shadow/Paper Execution。

单个公开源响应即使已有 SHA-256，也不能自行获得 `RESEARCH_GRADE`；在完整 Snapshot 合同和真实时间外证据之前，任何真实收益或成功率结论都不具备足够的数据证据。

## 11. 常用命令

捕获一份 `BEST_EFFORT` 原始 K 线 Artifact（不会修改生产数据库）：

```powershell
python .\scripts\capture_quant_bars.py `
  --symbol 600519.SH `
  --market A `
  --start 2024-01-01 `
  --end 2024-12-31 `
  --adjust qfq `
  --output-root .\data\quant-artifacts
```

量化迁移 dry-run：

```powershell
python .\scripts\quant_migrate.py --database .\data\stock_tracker.db
```

显式应用到指定测试数据库：

```powershell
python .\scripts\quant_migrate.py `
  --database .\data\quant-test.db `
  --apply
```

合同 Smoke：

```powershell
python .\scripts\run_quant_contract_smoke.py `
  --output .\docs\quant-contract-smoke.json
```

合成模型治理基准：

```powershell
python .\scripts\run_quant_fixture_benchmark.py `
  --output .\docs\quant-synthetic-fixture-benchmark.json
```

量化测试：

```powershell
python -m unittest discover -s tests_quant -p "test_*.py"
```

全量原项目回归：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```
