# N1–N3 数值基线与日线窗口：交付验证

2026-09-14。状态：IMPLEMENTED / INDEPENDENT_REVIEW_PASSED / SYNTHETIC_ENGINEERING_GATES_PASSED。不是策略晋级或真实收益验证。

## 身份

基线 `cdf8c1b4114252ead3bf5eb19d2f624be8929bd8`。N1/N2实现 `58474e387e1823a3dc8c568e982f62f7fd99a8c3`，N3实现 `d40c05f3c3cfdb8e5db033bfe8e1f35d9cd2b404`；独立审查修复后实现 `b299363b6358bda2868362c84d5be83f55d29374` / tree `3b2cd9d5b596da7e0daddc2725aae51ef2d94405`。后续文档提交的final SHA/tree与push回执见外部DELIVERY.json，避免commit自引用。

## 实际范围

N1：Runtime HTTPS系统CA/hostname验证，证书失败不降级；研究通道的URL/headers/redirect/response-size合同仍独立。本轮未调用真实Provider，也不将旧HTTP源称为已加密/认证。Quant精确数值类型、有限性、费用非负、k/bins/epsilon边界和溢出检查。max_drawdown是非空简单收益率的复利回撤、不是R倍数，不支持未声明杠杆/现金注入；成本省略仍代表明确零成本假设。profit_factor保留全盈利无损失时的历史infinity计算哨兵，不进入本轮公共JSON或真实战绩。

N2：保留已有有效数值域公式。44组指标、3组评价向量，共414字段逐项相等；刻意改变公式或来源元数据必须失败。RSI/ATR为rolling平均而非Wilder，平盘RSI=100和EMA短样本均值为旧约定，并非新策略判断。

N3：详情页显示20/60/120/252根日线窗口、样本量、方法/warmup、日期及计算时点。只读一次最多260根本地数据，旧详情指标仍最后80根、历史表30根。同日排除，混证券/周期/来源、重复/乱序、未来日期及非法OHLCV失败关闭；naive日期仅历史日期标签，不附UTC。样本充足不等于日历完整、复权一致、PIT或可交易。UI验证symbol/market/interval，缺值显示—、真实0保留；52周标签改为实际窗口收盘排名。没有信号权重、风险比例或模型晋级改动。

## 本轮实际结果

| 门禁 | 结果 |
|---|---|
| 指标/诊断/API/metrics/基线与相邻聚焦 | 89 tests，exit 0 |
| Full Runtime | 902 run / 901 passed / 1 expected skip；exit 0 |
| Full Quant | 737 passed；exit 0 |
| Stage4G/4F相邻 | 114 passed |
| Source distribution/no bytecode | 2 passed |
| 冻结兼容性 | 414字段零变化；BASE原始blob重新核对 |
| 新诊断Chromium | 18/18（含市场/周期/异常与转义反例、360/1440布局、键盘展开） |
| Today Mock / 临时真实API Today | 18/18、18/18 |
| 临时 Portfolio CRUD | 13/13 |
| 手工计划真实API/browser | 27/27 |
| 计划生命周期 | 18/18 |
| 新/重写核心及指定受影响文件 Ruff | pass |
| 两旧文件继承lint | serializers 15→15，feature_snapshot 3→3；新增0，不宣称全仓无lint问题 |
| indicators/diagnostics/metrics basedpyright | 0 errors |
| compileall(外部pycache)/pip check | pass |
| Quant smoke / fixture benchmark | pass；synthetic-only，未晋级 |
| 精确实现checkout | 698/698文件逐字节等于Git blob；89聚焦、分发、414对照和18浏览器再次通过 |

实现归档SHA-256 `1220954b58e38b340c239780095637c055bac25331ba13e3a876f36584ca052e`。仅合成测试和临时数据库；没有完整54场景/逐像素视觉或无障碍认证声明。既有临时HTTP/SQLite ResourceWarning原样保留。

## 首次独立审查与红绿闭环

首次d40c05f审查BLOCKED，三项为来源SHA、混合大整数溢出、前端市场/周期身份。来源根因是旧脚本将git文本解码/换行规范化并strip后hash，误标为原始源码hash。修复用原始BASE blob重新执行全部414预期并确认数值不变；仅更正元数据并更新fixture整体hash为 `9cdd460d4619da5a91bd9fa470504bbed61d1932e33ef107fb9f373006fa662f`。验证脚本在有Git时实际核对源字节，无Git时明确PINNED_MANIFEST_ONLY，不声称重读历史Git。

混合int/float反例先实际抛OverflowError，再添加特定算术边界返回未知；不吞一般异常。前端两项反例先出现16/18，再修复为18/18。新源码身份负例与全部89聚焦通过后复审。最终独立报告见 `NUMERICAL-BASELINE-20260914-INDEPENDENT-REVIEW.md`；独立审查实际执行范围以报告为准，完整测试是本对话执行结果，不能归到审查者名下。

首次开发还保留了新增serializer缺math导入、类型收窄、fixture helper直接module导入失败（全Quant discover已覆盖）、新QA未展开details导致读不到转义文本等失败记录。没有删除失败日志或把expected改成新公式结果。

## 复跑入口

```text
py -3.14 -X utf8 -B scripts/run_numerical_baseline.py
py -3.14 -X utf8 -B -m unittest tests.test_numerical_baseline tests.test_indicator_baseline_vectors tests.test_indicator_diagnostics tests_quant.test_metric_contract_edges tests.test_indicators tests.test_api_indicators tests.test_feature_snapshot -q
py -3.14 -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v
py -3.14 -X utf8 -B -m unittest discover -s tests_quant -p "test_*.py" -q
node qa/ui/indicator_diagnostics_qa.cjs
node qa/ui/today_action_qa.cjs
py -3.14 -X utf8 -B scripts/run_stage1_today_integration.py
py -3.14 -X utf8 -B scripts/run_planning_integration.py
node qa/ui/planning_lifecycle_qa.cjs
```

使用已安装Playwright或PLAYWRIGHT_MODULE/PLAYWRIGHT_PATH，TEMP/TMP/PYTHONPYCACHEPREFIX在外部目录。所有命令/退出码/日志索引、lint差异、raw blob来源核验、独立输出与push回执保存在 `D:\Projects\stock-tracker-review\numerical-baseline-20260914\`。

## 保护与剩余边界

生产DB/WAL/SHM未连接、未迁移，仅文件hash检查。DB `ce4156bf641e061d86ce944167ad2b1347f2437c130a7cf6eee26892fb78cbb7`；WAL `aff2033d7ed258e0033c8756a89a786742b7f7710b3ac099f4f63e8cfa70514e`；SHM `5b03f0ffeb0cf7b2827129308e34570f6a9db4fcd8d58c52b6e63c6f51c071b3`。原main、先前所有worktree及未审查算法草稿保持原样。没有启动真实Engine、开启Broker/XTP写API或自动下单。

N4候选证据评分、N5可信周/日分层、B1d语义ReadPort、B2/B3、真实fill/Shadow/4H仍待实施。后续设计见 `NUMERICAL-NEXT-STAGES-20260914.md`，不能把本轮数值诊断当作已验证多周期策略。
