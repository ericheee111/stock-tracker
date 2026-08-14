# Stage 1 并行执行计划｜Today Action MVP

> 适用基线：PRD v1.0 + 根 `AGENTS.md`
> 工作区：`D:\Projects\stock-tracker`
> 目标：在不伪造概率、主升浪、策略战绩或数据等级的前提下，并行完成 Stage 1 的核心合同、Portfolio 后端和首页 UI 骨架。
> 集成状态（2026-08-14）：Lane A/B/C/D 已完成并经过跨车道 Review；真实 `/api/brief/today` 与 Web 已接线。下一切片为 Portfolio 编辑 UI。

---

## 0.1 集成 Review 结论

- 三路代码已合并到同一工作树并补齐共享 dataclass 的直接构造安全边界；
- 持仓事实允许零碎股，PositionSizer 的新开仓建议继续遵守市场 lot size；
- Portfolio Repository、REST、Today Brief、前端 Mock 与真实 API 共用同一 schema；
- `/api/brief/today` 与 `/api/portfolio*` 已增加本机直连/公网认证失败关闭；
- Mock Playwright 与真实 Python API + Web Playwright 均通过；
- Big Trend、真实概率、真实战绩和自动交易仍未启用；
- 本文后续 Lane 说明保留为实施记录，不再代表待派发任务。

---

## 1. 所有 Agent 必读顺序

1. `AGENTS.md`
2. `docs/PRD-股票辅助判断与交易参考网站.md`
3. `docs/PRODUCT-GAP-MATRIX-v1.0.md`
4. `docs/STAGE1-PARALLEL-EXECUTION-PLAN.md`
5. `docs/STAGE1-API-CONTRACT-v1.md`
6. `docs/HANDOFF.md`
7. 涉及模型、回测或概率时，再读 `docs/VALIDATED-STRATEGY-ML-LIBRARY.md`
8. 涉及 Quant 时，再读 `docs/CODEX-QUANT-FOUNDATION-INTEGRATION.md`

任何旧 v0.4 Wave、T1–T15 或历史完成声明若与新版 Stage 计划冲突，以以上文件为准。

---

## 2. 并行总原则

- 所有 Agent 使用同一当前工作树时，只允许修改自己的文件范围；
- 不执行 `checkout/switch/reset/clean/stash/rebase/restore`；
- 不删除或覆盖未知未跟踪文件；
- 不 commit、merge 或 push，除非用户另行明确授权；
- 不修改 `data/stock_tracker.db`；
- 数据库测试只使用临时数据库；
- 不使用 Opportunity/100 伪造概率；
- 不把旧 SectorStage 冒充 Big Trend；
- 不把 synthetic benchmark 写成真实战绩；
- 不让 API 或前端直接访问上游 Provider；
- 每个 Agent 最终必须列出修改文件、测试、限制和未完成项。

---

## 3. Lane A｜ChatGPT 5.6 Sol Pro + CodexPro

### 所有权

```text
stock_tracker/decision/types.py
stock_tracker/decision/action_mapper.py
stock_tracker/decision/__init__.py
tests/test_decision_types.py
tests/test_action_mapper.py
```

### 任务

- 冻结 Stage 1 产品层严格类型；
- 新增 `ActionState`、`RiskMode`、Blocker、Probability Evidence 等合同；
- 建立旧 `SignalState` → 新产品动作的确定性映射；
- 建立最低安全持仓退出基线：只有 LIVE 可信价格跌破结构失效位才可产生 EXIT；
- STALE/UNKNOWN 数据必须产生 `DATA_BLOCKED`，不能伪造买入或卖出；
- 保持 import 无网络、无数据库、无 Quant 副作用；
- 增加严格类型和动作映射测试。

### 禁止修改

- Storage/Repository/API；
- Web/QA；
- 现有 `signals/state_machine.py`；
- Quant；
- 生产数据库。

---

## 4. Lane B｜Codex A：核心金融逻辑完成与 Review

### 推荐模型

```text
GPT-5.6 Sol
Reasoning effort: Extra High / xhigh
```

### 所有权

```text
stock_tracker/decision/position_sizing.py
stock_tracker/decision/trade_plan.py
stock_tracker/decision/brief.py
stock_tracker/decision/ranking.py           # 如确有必要
tests/test_position_sizing.py
tests/test_trade_plan.py
tests/test_decision_brief.py
```

### 任务

1. 先 Review Lane A 的 `types.py` 和 `action_mapper.py`，但不要直接改；若发现 blocker，写进最终报告并给最小 patch 建议。
2. 实现 long-only、fail-closed PositionSizer：
   - 账户净值；
   - 可用现金；
   - 单笔风险预算；
   - 单股硬上限；
   - Portfolio Heat 剩余额度；
   - 板块/主题暴露；
   - A 股 100 股交易单位；
   - 港股 lot size 必须显式提供；
   - 美股默认 1 股；
   - entry 必须高于 invalidation；
   - NaN/Inf/bool/负数全部拒绝；
   - 输出限制因子和实际风险。
3. 实现 TradePlan：
   - entry zone、trigger、no-chase、invalidation、targets、RR；
   - hard/soft blockers 分离；
   - probability 允许为 null；
   - hard blocker 下禁止 aggressive plan；
   - aggressive plan 风险预算必须低于 balanced plan；
   - 不生成自然语言模型判断，只生成确定性结构。
4. 实现 DecisionBrief / Core Selector：
   - Core 默认最多 5 个；
   - 动作优先于纯 Opportunity；
   - 概率为空时 `ranking_mode=RULE_EVIDENCE`；
   - 同板块配额；
   - 同 symbol 去重；
   - 持仓动作独立排序；
   - 只生成 summary facts，不调用外部 LLM。
5. 运行针对性和完整回归。

### 禁止修改

- Storage/Repository/API；
- Web/QA；
- Quant；
- `signals/state_machine.py`；
- 生产数据库；
- Lane A 文件，除非用户后来明确批准修复 blocker。

---

## 5. Lane C｜Codex B：Portfolio 持久化与 REST API

### 推荐模型

```text
GPT-5.6 Sol
Reasoning effort: High
```

若该 Codex 同时承担 schema migration、HTTP 方法、严格 payload 校验和并发/Windows 文件锁审查，可改为 Extra High。

### 所有权

```text
stock_tracker/storage/schema.sql
stock_tracker/storage/repository.py
stock_tracker/core/store.py
stock_tracker/api/server.py
stock_tracker/api/handlers.py
stock_tracker/api/serializers.py
tests/test_portfolio_repository.py
tests/test_portfolio_api.py
tests/test_server_methods.py
```

### 任务

只做 Portfolio/Position CRUD，不实现 DecisionBrief 或排名算法：

1. 增加 `portfolio_profile`：
   - account_equity；
   - available_cash；
   - risk_mode；
   - per_trade_risk_pct；
   - max_position_pct；
   - max_portfolio_heat_pct；
   - max_sector_pct；
   - max_theme_pct；
   - updated_at。
2. 保持现有 Position schema 兼容；提供严格新增、修改、删除接口。
3. 实现：
   - `GET /api/portfolio`；
   - `PUT /api/portfolio/profile`；
   - `POST /api/portfolio/positions`；
   - `PATCH /api/portfolio/positions/{id}`；
   - `DELETE /api/portfolio/positions/{id}`。
4. JSON 解析失败必须 400，不能静默变成 `{}`；
5. 非数字、bool 冒充数字、NaN/Inf、负值、非法 Market、空 symbol 全部拒绝；
6. API 只读写本地 Store/Repository，不访问 Provider；
7. 使用临时数据库测试幂等建表和 CRUD；
8. 不对生产数据库运行真实迁移；
9. 不接 `/api/brief/today`，等待 Lane B 合并后再做集成。

### 禁止修改

- `stock_tracker/decision/**`；
- Web/QA；
- Quant；
- 信号评分/风控/状态机；
- 生产数据库。

---

## 6. Lane D｜WorkBuddy：Today Action 前端与 QA Mock

### 所有权

```text
web/index.html
web/js/app.js
web/js/components.js
web/js/api.js
web/css/cockpit.css
web/js/today.js                 # 推荐新增
web/css/today.css               # 推荐新增
qa/fixtures/today-brief-v1.json
qa/ui/today_action_qa.cjs
qa/ui/today_action_shot.cjs
```

### 任务

1. 按 `docs/STAGE1-API-CONTRACT-v1.md` 的 mock contract 实现首页 A+D 混合布局：
   - AI/确定性参谋摘要区域；
   - 市场姿态；
   - 今天建议你做；
   - Core 3—5；
   - 持仓需要处理；
   - 今日不要做；
   - 数据/模型证据状态。
2. 动作和状态为主，数字为辅；
3. 概率为 null 时显示“真实样本或校准证据不足，暂不展示概率”；
4. `big_trend.status=NOT_AVAILABLE` 时隐藏正式主升浪列表或显示“正式算法尚未启用”，不得拿 SectorScore 冒充；
5. 所有文本使用现有 `esc()` 或等价安全转义；
6. 不渲染原始 JSON；
7. 保持移动端和桌面端；
8. 使用 fixture 完成无后端依赖的 DOM/Playwright 契约测试；
9. 可以增加 API 客户端函数，但真实后端尚未实现时必须保留明确 fallback，不得改后端。

### 禁止修改

- Python 后端；
- Storage/API server；
- `stock_tracker/decision/**`；
- Quant；
- 生产数据库。

---

## 7. 并行依赖与合并顺序

```text
Lane A：核心类型/动作映射 ──────┐
                                  ├─> Integration Review
Lane B：Sizer/Plan/Brief ─────────┤
                                  ├─> /api/brief/today 接线
Lane C：Portfolio CRUD/API ───────┤
                                  ├─> Frontend real API integration
Lane D：Frontend + mock QA ───────┘
```

推荐收敛顺序：

1. Lane A Review；
2. Lane B Review；
3. Lane C Review；
4. 合并核心类型 + 后端 Portfolio；
5. 新建 `/api/brief/today` 集成切片；
6. 将 Lane D 从 mock 切到真实 API；
7. 全量测试和人工 UI 验收；
8. 用户明确授权后再 commit/push。

---

## 8. 共同验收命令

```bash
python -m compileall -q stock_tracker tests tests_quant scripts
python -m unittest discover -s tests -p "test_*.py" -v
python -m unittest discover -s tests_quant -p "test_*.py" -v
python scripts/run_quant_contract_smoke.py
python scripts/run_quant_fixture_benchmark.py
python scripts/quant_migrate.py --database data/stock_tracker.db
python -m pip check
```

前端 Lane 另外运行 `qa/` 下新增的契约与 Playwright 检查。

---

## 9. 完成定义

并行阶段结束时必须满足：

- 各 Lane 没有修改他人所有权文件；
- 所有概率空值诚实保留；
- Big Trend 未实现时没有伪输出；
- hard blocker 无法被 aggressive 模式绕过；
- DATA_BLOCKED 不会自动变成 EXIT；
- 生产数据库 SHA-256 不变；
- 没有 commit/merge/push，除非用户另行授权；
- 每个 Agent 提供可审查的 changed-files 和测试报告。
