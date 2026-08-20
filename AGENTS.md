# AGENTS.md

本文件适用于整个 `stock-tracker` 仓库。更深目录如未来存在自己的 `AGENTS.md`，仅在其目录范围内补充或收紧本规则，不得削弱本文的金融正确性、数据真实性和安全边界。

## 1. 项目使命

`stock-tracker` 是一个 **A 股优先、港股通第二、美股第三** 的个人交易决策驾驶舱。

产品每天首先回答：

> 今天该怎么操作？

系统需要把可信行情、市场环境、板块状态、个股特征、公告事件、模型输出、持仓和组合风险，转化为可解释的动作：

- 当前可执行；
- 等回踩；
- 等突破；
- 继续持有；
- 风险预警；
- 减仓；
- 部分止盈；
- 保留 Trend Runner；
- 退出；
- 当前回避；
- 数据不足，禁止决策。

模型与算法准确率是底层核心竞争力；交易计划、大行情识别、事件理解、风险控制、策略战绩和 Replay 是把准确率转化成实际价值的产品能力。两者都必须持续迭代。

当前系统只提供辅助判断，不承诺收益，不直接下单。

## 2. 规范优先级

处理需求、设计或代码冲突时，按以下顺序执行：

1. 当前用户明确指令；
2. 本文件；
3. `docs/PRD-股票辅助判断与交易参考网站.md`；
4. `docs/VALIDATED-STRATEGY-ML-LIBRARY.md`；
5. `docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md`；
6. `docs/HANDOFF.md` 与专项 handoff；
7. 其他架构、设计和历史说明。

历史文档可能包含旧版本号、旧测试计数或旧路线。不得因为旧文档写着“已完成”，就跳过当前代码与当前 commit 的验证。

## 3. 产品已冻结方向

开发时默认以下产品决策已经冻结：

- 首页核心是“今日作战简报 + AI 参谋摘要”；
- 首页以动作和状态为主，数字为辅；
- Core Opportunity 首页通常只展示 3—5 个；
- Core Opportunity Radar 与 Big Trend Radar 分开；
- Big Trend 首版优先板块、个股、龙头/中军和二次启动；
- `EMERGING` 只代表早期观察，不能单独触发买入；
- 默认风险模式为 `BALANCED`；
- 软阻断可展示小仓位激进方案，硬阻断永远不可绕过；
- 概率采用“模型倾向 + 校准概率”双层展示；
- 真实样本或校准不足时，概率必须为 `null`；
- Event Intelligence 是核心模块；
- 用户可手动提供账户净值、现金、持仓股数和成本价；
- 仓位按风险计算，并受现金、流动性、单股上限、Portfolio Heat 和集中度共同约束；
- Exit 独立于 Entry；
- 大行情采用部分止盈 + Trend Runner；
- Strategy Scoreboard 和 Point-in-Time Replay 是核心能力；
- 当前零成本或近零成本优先；
- 当前不接券商，未来先只读行情，再单独评估执行；
- LLM 只做结构化抽取和解释，不直接决定买卖；
- 自动交易不属于当前版本。

不得在没有重新进行产品对齐的情况下，把项目退回成普通行情看板、技术指标大杂烩或单一“强买/强卖”网站。

## 4. 仓库结构

```text
config/                  TOML 配置
stock_tracker/           运行产品代码
  api/                    REST、静态文件和 SSE
  collector/              Provider、Router、Scheduler
  core/                   配置、时钟、类型、Store、EventBus
  data_quality/           数据质量和健康状态
  features/               运行态指标、证据、Regime、Sector
  signals/                评分、风控、状态机和信号管理
  strategies/             S1/S2/S3 等规则候选
  storage/                运行 SQLite 与 Repository
  quant/                  独立量化研究合同
web/                      静态前端
scripts/                  启停、捕获、迁移和验证脚本
tests/                    运行产品测试
tests_quant/              量化合同测试
docs/                     PRD、算法库、架构、Handoff 和证据
qa/                       前端/可视化 QA 工具
```

## 5. 运行链与研究链必须隔离

### 5.1 运行链

运行链用于：

- 页面展示；
- 候选发现；
- 规则信号；
- 持仓监控；
- 站内提醒。

运行链当前以 `MarketStore`、`Repository` 和根目录 `data/stock_tracker.db` 为主要缓存/存储。

### 5.2 研究链

`stock_tracker.quant` 用于：

- Point-in-Time 数据合同；
- Manifest 与不可篡改身份；
- 交易日历和证券状态；
- 标签；
- 执行模拟；
- walk-forward；
- 模型、校准和晋级治理。

研究链不得在 import 时：

- 启动采集；
- 修改生产数据库；
- 接入生产信号；
- 自动训练或自动晋级；
- 启用自动交易。

### 5.3 禁止自动回流

以下内容不得自动成为正式研究数据：

- 页面 SQLite `bars`；
- Provider 内存中的已解析对象；
- 单一公开源返回后重新序列化的数据；
- 未绑定交易日历、历史 Universe、证券状态和公司行为的数据。

正式回测、校准和模型晋级必须使用达到相应用途合同的数据快照。

## 6. 数据可信等级

统一使用：

```text
T0 UNKNOWN
T1 BEST_EFFORT
T2 OPERATIONAL_VERIFIED
T3 RESEARCH_GRADE
T4 FROZEN_HOLDOUT
```

最低用途：

- T0：仅故障排查；
- T1：UI、候选发现；
- T2：规则信号、Paper/Shadow；
- T3：回测、训练、校准；
- T4：最终模型晋级。

Trust Tier 只能由新增证据升级，不能由调用方传一个布尔值、改 descriptor 或重算 ID 自我升级。

合成 fixture 只证明工程合同，不能声称真实胜率、收益率、Sharpe、最大回撤或投资表现。

## 7. 金融正确性硬规则

触及数据、特征、标签、回测、模型、评分、风险、状态机或仓位时，必须遵守：

1. 所有正式时间语义使用 Point-in-Time；
2. `known_at <= usable_from <= as_of`；
3. 收盘后信息不得用于当日收盘前决策；
4. 不得随机打散金融时间序列做正式验收；
5. 必须使用 next executable price；
6. A 股处理 T+1、涨跌停、停牌、交易单位和费用；
7. 港股处理延迟、价差、流动性、跳空、VCM 和交易单位；
8. 美股处理财报、盘后跳空、行业和中线周期；
9. 回测必须包含交易成本、价差、滑点和必要的冲击成本；
10. 历史 Universe 不能用今天的成分回填；
11. 不能忽略退市样本；
12. 公司行为必须版本化并可复现；
13. 概率必须经过时间外校准；
14. Opportunity Score 不得除以 100 冒充成功概率；
15. Exit 不能简单使用 `-EntryScore`；
16. Big Trend 不能只是普通突破分数放大；
17. LLM 不能直接给股票增加或减少买卖分；
18. 复杂模型只有稳定击败 Rule/Logistic 基线后才可晋级。

当信息不完整时，优先失败关闭，不得通过猜测让结果“看起来可用”。

## 8. 安全类型与失败关闭

安全边界字段必须严格校验实际类型。

- 布尔字段必须满足 `type(value) is bool`；
- 三态布尔必须满足 `value is None or type(value) is bool`；
- 不接受 `0/1`、`"true"/"false"` 或 truthy/falsy 对象；
- boolean 不得悄悄作为 integer；
- Mapping identity 的 key 必须是字符串；
- 无时区 datetime 不得进入正式量化身份；
- NaN/Infinity 不得进入 fingerprint 或模型身份；
- TOML 文件缺失可以按明确策略使用缺省值；文件存在但语法或安全类型错误必须失败；
- 完整性 ID 必须在 schema/type 验证之后验证，重算 ID 不能绕过类型合同。

## 9. 数据真实性和延迟

所有实时或准实时输出尽量携带：

```text
source_timestamp
received_at
computed_at
displayed_at
observed_age_ms
data_status
source
```

数据状态至少区分：

```text
LIVE
DELAYED
STALE
UNKNOWN
```

规则：

- 港股或美股延迟行情不得包装为秒级实时；
- EOD Bar 不得标为盘中 `LIVE`；
- 上游源时间戳不可靠时，不得标“实时”；
- `STALE` 或 `UNKNOWN` 不得产生新的强执行信号；
- API 和 UI 必须诚实显示数据状态；
- 免费源随时可能失效，Provider Adapter 必须可替换；
- API 层不得自行访问上游，Collector 是唯一上游访问者；
- 本地行情 Sidecar 也属于外部 Provider，不因“本机读取”自动升级 Trust；
- `free-stockdb` 等 Sidecar 默认关闭，只允许 loopback、只读和显式 RAW 查询；
- Sidecar 的复权因子、板块成分和当前证券列表不得直接进入 PIT 回测、训练或模型晋级；
- Sidecar 服务端口不得暴露公网，真实启用前必须固定发行版、二进制、manifest 和数据快照身份。

## 10. 事件和 LLM 边界

事件来源优先级：

1. 交易所、法定披露和监管机构；
2. 公司公告、财报和 IR；
3. 政府部门；
4. 高可信新闻；
5. 普通聚合；
6. 社交和传闻。

事件至少保留：

```text
source
published_at
known_at
usable_from
affected_entities
authority
materiality
novelty
surprise
confirmed
raw_artifact_id
parser_version
```

LLM 可以抽取、分类、实体映射、对比和解释；不得：

- 编造原文不存在的数字；
- 把传闻标为事实；
- 直接改变交易状态；
- 绕过追高、赔率、流动性和组合硬门；
- 覆盖结构化模型输出。

## 11. 模型与策略治理

正式梯队：

```text
Rule-only baseline
Logistic Regression baseline
LightGBM 或等价树模型 Candidate
DoubleEnsemble / TRA Challenger
复杂深度模型 Shadow Research
```

新模型至少在相同数据、标签、Universe、成本和切分下比较：

- Brier；
- LogLoss；
- Precision@K；
- Net Expectancy；
- 最大回撤；
- Worst 5%；
- 分数桶单调性；
- Regime 稳定性；
- 时间稳定性；
- 参数邻域稳定性。

任何一次实验必须登记 hypothesis、数据身份、特征版本、标签、切分、成本、trial 数量、结果和晋级决定。

Frozen Holdout 一旦暴露，就不能继续称为未见 holdout。

## 12. Core Opportunity 与 Big Trend

### Core Opportunity

- 目标是少而精、当前或近期可执行；
- 首页通常 3—5 个；
- 优先 Precision@K、Net Expectancy、风险和校准；
- 同股票多策略命中应合并；
- Top-K 应有板块/主题多样性。

### Big Trend

状态建议：

```text
NONE
EMERGING
CONFIRMING
TRENDING
MATURE
DISTRIBUTING
BROKEN
```

- `EMERGING` 只观察；
- `CONFIRMING/TRENDING` 可进入交易计划；
- `MATURE` 不追高；
- `DISTRIBUTING` 触发 WARNING/TRIM；
- `BROKEN` 关闭 Trend Runner 并评估 EXIT。

Big Trend 评估捕获率、提前量、误报率、过早退出率和 Trend Runner 贡献，不只看普通胜率。

## 13. 持仓、仓位和退出

用户持仓至少支持：

```text
symbol
market
shares
average_cost
```

组合设置至少支持：

```text
account_equity
available_cash
per_trade_risk_pct
max_position_pct
max_portfolio_heat_pct
max_sector_pct
max_theme_pct
risk_mode
```

建议股数应根据入场与失效距离计算，再受现金、交易单位、流动性和组合约束。

成本价用于盈亏、实际 R 和盈利保护，不能因为用户被套而改变客观失效判断。

退出状态应支持：

```text
HOLD
WARNING
TRIM
PARTIAL_TAKE_PROFIT
TREND_RUNNER
EXIT
```

严重逻辑失效允许直接 `HOLD -> EXIT`，但必须记录原因。

## 14. API 和 UI 合同

API/前端改动必须保持：

- JSON 字段稳定；
- 缺失数据使用 `null` 或明确状态，不填伪值；
- 所有用户可见字符串转义；
- 不渲染原始对象或 JSON 垃圾；
- 数据状态、as-of 和证据可见；
- 首页动作优先，数字辅助；
- 正反理由同时展示；
- “为什么不能买”是一级输出；
- UI 不把合成战绩或 Model Score 写成真实成功率；
- REST 只读本地状态，不因为页面刷新访问上游；
- 含账户净值、持仓和建议股数的 `/api/brief/today`、`/api/portfolio*` 必须视为私有 API；本机免认证必须同时校验 loopback 客户端与 localhost Host，公网/反向代理必须认证或失败关闭；
- 私有访问值只能来自运行环境或当前浏览器会话，禁止写入仓库、公开前端、日志和错误响应；
- SSE 只推送有意义的状态变化，避免重复轰炸。

修改前端后，尽量运行 `qa/` 下相关契约或 Playwright 检查；不能运行时必须明确说明。

## 15. 编码约定

- Python 目标以当前仓库支持版本为准，运行代码保持标准库优先；
- 不因便利随意增加运行时第三方依赖；确需新增时必须说明成本、体积、安全和部署影响；
- 使用类型注解；
- 纯计算优先纯函数；
- dataclass/Enum 作为核心合同；
- 避免重复实现相同指标或同一数据映射；
- 不使用宽泛 `except Exception` 静默吞掉金融正确性错误；
- 允许在进程边界捕获异常并记录，但内部合同错误应显式暴露；
- 时间、货币、市场、周期和单位必须在命名或类型中明确；
- 不把市场规则硬编码成永远不变的常识；
- 配置解析和安全字段采用 fail-closed；
- 变更必须附针对性回归测试。

## 16. Git 与并行工作保护

开始任何任务前：

1. 查看当前分支和工作树；
2. 识别用户或其他 Agent 的已有修改；
3. 只修改任务所需文件；
4. 不覆盖、不回滚、不格式化无关文件；
5. 不删除不理解的未跟踪文件；
6. 不把运行数据库、日志、缓存、截图或密钥加入 Git。

当前仓库可能同时存在 WorkBuddy、Codex 或人工修改。提交前必须列出实际纳入文件。

未经明确授权，不执行：

- `git reset --hard`；
- `git clean`；
- 覆盖式 checkout/restore；
- rebase 或历史重写；
- 删除本地数据；
- 修改生产数据库；
- commit、merge 或 push。

用户明确要求提交/推送时，仍必须先完成适当验证，并避免混入无关修改。

## 17. 数据库规则

根目录 `data/` 是运行数据，默认不入库。

- 不要读取、复制或展示用户敏感交易数据，除非任务需要；
- 量化迁移默认 dry-run；
- 只有显式 `--apply` 才能修改目标数据库；
- 未经明确授权，禁止对 `data/stock_tracker.db` 使用 `--apply`；
- 涉及 migration 的验证应记录数据库前后 SHA-256；
- 测试优先使用临时目录和临时数据库；
- 连接必须正确关闭，尤其 Windows 下避免文件锁残留。

## 18. 标准验证命令

根据改动范围选择最小充分验证；合并或发布前运行完整门禁。

### 运行产品测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

### Quant 测试

```bash
python -m unittest discover -s tests_quant -p "test_*.py" -v
```

### 编译检查

```bash
python -m compileall -q stock_tracker tests tests_quant scripts
```

### Quant 合同 Smoke

```bash
python scripts/run_quant_contract_smoke.py
```

### 合成 Fixture Benchmark

```bash
python scripts/run_quant_fixture_benchmark.py
```

### Migration Dry-run

```bash
python scripts/quant_migrate.py --database data/stock_tracker.db
```

该命令默认只读；禁止随意添加 `--apply`。

### 依赖检查

```bash
python -m pip check
```

### Today Action 前端与真实 API 集成

```bash
node qa/ui/today_action_qa.cjs
python scripts/run_stage1_today_integration.py
```

第一条验证 Mock/schema 降级合同；第二条使用临时 SQLite、真实 Python API 和真实 Web 文件运行 Playwright，不访问生产数据库或外部 Provider。

### 本地自检

```bash
python -m stock_tracker --once
```

该命令可能访问真实上游，仅在网络和任务允许时运行；离线单元测试不得依赖网络。

## 19. 合并与发布门禁

声称“可合入”或“可发布”前至少确认：

- 当前 diff 已审阅；
- 关键源码没有被 `.gitignore` 误排除；
- `compileall` 通过；
- 受影响 tests 通过；
- 重大变更运行完整 legacy + quant tests；
- Quant 合同 smoke 通过；
- migration 保持 dry-run；
- 生产数据库未改变；
- fresh committed-tree 可以导入关键模块；
- 证据对应当前 commit；
- 合成结果明确标为 synthetic；
- 没有把未验证概率、战绩或收益展示给用户；
- 没有误带入并行工作。

## 20. Agent 任务边界

### Codex 主做或强制 Review

- PRD 与核心架构；
- Point-in-Time；
- Calendar/Status/Universe/Corporate Action；
- 标签；
- 回测和执行模拟；
- 核心评分和 Risk Gate；
- Big Trend；
- Sector Rotation / Leader-Lag；
- Event 影响模型；
- Exit 和仓位；
- 模型、校准和晋级；
- Strategy Scoreboard 口径；
- Replay 时间正确性；
- 归因、反证和 multiple-testing 治理。

### 普通工程 Agent 可实现

在规格冻结后：

- Provider Adapter；
- CRUD；
- REST/SSE；
- 前端；
- 配置；
- 日志和运维；
- 普通导入导出；
- 已冻结公式的批量实现；
- 测试扩展。

任何触及 `quant/`、`labels/`、`backtest/`、`models/`、`signals/scoring.py`、`signals/risk_gate.py`、核心 `sector`、仓位或 Exit 语义的修改，合并前必须进行金融正确性 Review。

## 21. 当前推荐开发顺序

以新版 PRD 为准：

1. Stage 0：PRD 冻结与现状 Gap Audit（已完成）；
2. Stage 1 核心：Today Action 决策合同、Portfolio 后端、真实 Brief API 与首页（已完成）；
3. Stage 1.1：Portfolio 设置/持仓编辑 UI 与真实 REST CRUD 验收（当前下一步）；
4. Stage 2：A 股数据与决策质量；
5. Stage 3A/3B：Event Intelligence + Big Trend v1；
6. Stage 3C.1：可选本地行情 Sidecar 隔离合同，默认关闭；
7. Stage 3C.2/3C.3：固定发行版与真实数据审计，通过后再进入 WARM/COLD Shadow；
8. Stage 4：Strategy Scoreboard + Replay；
9. Stage 5：真实数据上的模型准确率迭代；
10. Stage 6：港股通与美股独立扩展；
11. Stage 7：可选券商只读能力及单独审批的执行路线。

不要因为已有旧 Wave 编号而跳过用户价值和依赖关系。

## 22. 交付汇报要求

最终汇报必须区分：

- 实际修改了什么；
- 没有修改什么；
- 运行了哪些命令；
- 哪些通过、哪些失败；
- 数据库是否变化；
- 是否使用真实数据或仅 synthetic fixture；
- 是否 commit / merge / push；
- 当前已知限制；
- 下一步建议。

不得声称未实际完成的动作，不得用历史测试结果替代当前验证，不得把“代码存在”写成“真实策略已验证”。
