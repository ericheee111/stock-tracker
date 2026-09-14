# N4 评分解释与声明输入研究：使用及恢复

2026-09-14。本文件说明当前候选接口，不是交易建议或策略有效性报告。所有示例为synthetic；费用/周期示例不是用户默认参数。

## 页面

在证券详情展开“评分依据核对”。左列是同一份有界缓存副本上的旧规则重新计算，右列是显式缺失候选，第三列为差值。它不是现有Signal的历史分数，不参与排序、持仓数量或风险约束。完整依赖不足时总分为 `—`，仍可查看已知贡献项。真实RSI零保留，MA60/MACD/ROC预热不足不代填。当前Runtime的Regime/Sector缺少可靠证券/市场/时间绑定，所以API不借用全局上下文；部分总分为空是设计状态。

只读取一次最多260根本地日线缓存，比较使用过去日期的最后80根；同日行排除。原详情指标80根和表格30根维持。没有上游请求，没有新数据库、订单、模型训练或策略启用开关。行情源无时区时间按旧标签展示；无法升级为PIT。可选解释失败会报告不可用，不破坏原详情。

## 固定评分反例（stdout，只读）

从仓库根目录、Python3.14执行：

```text
py -3.14 -X utf8 -B scripts/run_evidence_comparison.py
py -3.14 -X utf8 -B scripts/run_evidence_comparison.py --case rsi-zero --details
```

默认12个固定场景，名称不得重复或拼错；正常完成exit0，反例断言不符合exit1，命令输入非法exit2。参数无生产DB/Provider选项。固定场景包含非交易日期，不能将它们当作交易日连续性证据。

## 完整周线（声明输入，不连生产数据）

```text
py -3.14 -X utf8 -B -m stock_tracker.features.horizon_cli --fixture week
py -3.14 -X utf8 -B -m stock_tracker.features.horizon_cli --fixture experiment
py -3.14 -X utf8 -B -m stock_tracker.features.horizon_cli --input D:\Temp\declared-input.json --output D:\Temp\new-n4-report.json
```

`--fixture` 只运行内置合成样例；`--input` 只读调用方显式提供的UTF-8 JSON（最多2MiB），不抓行情或读SQLite。输出默认stdout；只有显式 `--output` 才新建 `.json` 文件，既有文件绝不覆盖。输出目录必须事先存在，请使用单独临时/报告目录，不放进物理Evidence Store或生产data目录。写入中断后的残留报告不作为通过证据；更换新输出路径重新运行，不以手工编辑结果冒充重算。

### 时区数据前提与失败恢复

周线计算需要解释器可以解析对应市场的IANA时区数据库；某些Windows解释器可能缺少该数据。缺少时区时输出结构化 `n4-offline-horizon-error-v1`、`code=TIMEZONE_DATABASE_UNAVAILABLE`、exit2，不产生成功报告或输出文件。请在正常环境配置中核对系统时区数据或已批准的tzdata依赖；本工具不自动安装、不静默改成固定UTC偏移、不猜测美国夏令时。实验分区仅使用已明确偏移的时间戳，其不需要市场时区的路径仍可运行。记录失败，不将缺依赖解释为市场无数据。

### 周线文档

schema=`n4-declared-week-input-v2`；顶层字段恰为schema/symbol/market/as_of/calendar/observations。用 `fixture_document('week')` 生成示例对象再查看结构；它是演示数据不是交易所日历。

Calendar每条必须包含day、market、is_open、close_at、known_at、usable_from、evidence_id。闭市close_at为null。Observation每条包含symbol、market、day、close_at、known_at、usable_from、open/high/low/close（十进制字符串）、volume（整数）、price_basis_id/source_id。字段必须完整，不接受扩展字段、重复JSON键、NaN或隐式数字转换。

每个当地周从周一到周日的七个日期必须显式给出，最多53周。不猜节假日。OPEN日恰好一根已结束日线、CLOSED日无bar，来源和价格口径一致。只接受已过下一周一当地00:00的周；未结束周不提前输出。`latest_known_at` 和 `latest_usable_from` 包含日历与bar所有依赖；`available_at` 还不早于周界，不能用周五时点引用尚未闭合的完整周。无OPEN日输出NO_OPEN_SESSIONS，不伪造OHLC为0。

### 实验清单

schema=`n4-experiment-input-v2`。spec为purpose/market/lookback_sessions/horizon_sessions/review_every_sessions/label_policy_id/exit_policy_id/cost_model_id/price_basis_id。必须显式给出，持仓用途为SWING、LONG_TERM或SHORT_TERM；T操作不是第四类独立episode。

samples包含sample_id、episode_id、purpose、market、decision_at、feature_known_at、feature_usable_from、label_end_at、label_known_at、label_usable_from、source_snapshot_id、definition_id。特征必须在决策前可用。train_start/calibration_start/validation_start/validation_end必须递增且不晚于as_of；`embargo_microseconds` 是显式经过时长，不冒充交易日数量。每个样本分配或给出purge原因；标签可用时间跨越分区截止或隔离间隙即purge，不仅检查事件日期。重复episode拒绝，不能用多次T扩大独立样本数。分区有样本仅表示可作fixture对照，不是样本量足够或任何预测模型通过。

## 明确不足

当前哈希是输入重现承诺，不证明日历、source、price basis或获知时间。所有研究清单固定DECLARED_NOT_AUTHORITY_VERIFIED/research_grade=false/model_fitted=false/auto_promote=false/auto_trade=false。未连接实际Source Authority、未训练模型、未消费holdout、未实测收益；后续复用已有Quant训练/验证门禁而不另造平台。B1d/B2/B3/实际fill草稿仍待单独收口。

## 回归入口

```text
py -3.14 -X utf8 -B -m unittest tests.test_evidence_comparison tests.test_evidence_diagnostic_api tests.test_evidence_http tests.test_evidence_recipe_identity tests.test_horizon_research tests.test_horizon_cli tests.test_n4_continuation_edges tests.test_quote_age_clock_basis tests.test_n4_review_closure -v
node qa/ui/evidence_comparison_qa.cjs
```

浏览器命令使用已安装的Playwright，可由PLAYWRIGHT_MODULE或PLAYWRIGHT_PATH指定；截图/结果写外部临时目录或显式EVIDENCE_QA_REPORT_DIR。不清除运行器安全保护。错误必须保留原始退出码和日志，不能因为出现部分PASS文字就宣告通过。

## 报价状态与缺失依赖核验

解释区显示LIVE/DELAYED/STALE/UNKNOWN的**来源声明**，明确未经独立认证。非LIVE声明同时显示警告，字段缺失、非法类型或警告矛盾会使解释区不可用；不能据此升级数据Tier或授权交易。任何贡献项缺失都必须使依赖总分不可用；分组missing覆盖子项missing，分数组不能另填不同的证据族值。渲染层只做结构一致性校验，不另外复制一套Python评分算法。
