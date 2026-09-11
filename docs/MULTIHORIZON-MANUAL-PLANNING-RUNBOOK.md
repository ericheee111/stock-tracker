# 多周期持仓与做 T 手工计划：使用及运维

日期：2026-09-11。范围：P1–P3 手工计划功能。没有券商连接、自动买卖信号、订单、成交验证或真实绩效统计。它不替代原持仓事实，也不改变既有策略阈值。

## 1. 首次启用（显式写入新的本地计划库）

先创建自己的本地私有目录。不要复用 `data/stock_tracker.db` 或任何已存在数据库。例：目录 `D:\StockTrackerPrivate` 已存在后，在本项目 checkout 根目录执行：

```powershell
py -3.14 -m stock_tracker.portfolio_planning init --database "D:\StockTrackerPrivate\planning.sqlite"
```

这是明确的创建操作，不是 dry-run。命令拒绝覆盖，输出 `store_id`。把两项配置放到当前本地引擎的启动环境，不放入云端静态文件：

```powershell
$env:STOCK_TRACKER_PLANNING_DB = "D:\StockTrackerPrivate\planning.sqlite"
$env:STOCK_TRACKER_PLANNING_STORE_ID = "上一步输出的store_id"
```

再用原有项目启动命令启动引擎。普通应用启动只打开并审计计划库，绝不自动初始化或修改 Portfolio Schema。未配置时，原有持仓/机会功能继续工作，计划面板明确显示未启用。配置不完整、文件或schema错误时关闭计划写入，不降级为另一个空库。

## 2. 日常使用

1. 在原有持仓管理录入真实总仓。展开“持仓分层与做 T 计划”，给波段、长持、短线分配数量与核心保留量，写明依据、失效及复核日期。未分配部分保持未分类。复核日期是提醒字段，不会自动清仓。
2. 准备做 T 前，确认对应币种可用现金，以及该持仓的可卖旧量、外部未成交卖单占用、证券交易单位、最大中途总股数。两类快照为人工未验证来源，UI有效约9分钟，后端上限15分钟；不是实时券商余额。
3. 选择父计划用途、两腿方向、数量、人工买卖限价与全部费用/滑点预留。先“检查条件”，再单独确认预留。价格来自用户，不是系统生成的交易机会或预测。
4. 完全未执行且无外部委托才可取消。已转实际操作则标记待对账，所有预留继续保留。工具不会接收 Broker 回报或猜测成交。
5. 核对实际 Portfolio 数量/成本/现金及外部委托后，按币种处理全部未关闭计划。对账只关闭内部计划并清空该币种旧库存/现金确认，不自动写入 Portfolio，也不产生 COMPLETE Outcome。

所有方向同时保守预留可卖旧量和完整买入现金，不预支预计卖出款。分组、多个页面、多个同股计划共享额度。数量过期不等于外部操作消失，因此不会自动释放。

同证券父持仓被删除重建、数量/成本变化、旧计划尚未对账时必须核对后处理。网页与Portfolio跨两个库，不宣称原子快照；实际下单前仍须核对真实券商界面。

## 3. 重试、失联和错误

客户端命令包含计划库ID、命令ID和页面版本。网络错误后页面保留原命令，使用“重试未确认请求”发送相同ID，不自动生成新操作。冲突返回409并提示刷新；绝不靠删除日志解决冲突。

当重启/刷新页面导致浏览器内未确认请求信息丢失，先刷新计划列表核对已有记录，再决定是否创建新计划。服务端保存完整命令幂等记录，但不会推测两个不同ID是同一次用户意图。

`NOT_CONFIGURED / UNAVAILABLE / SNAPSHOT_STALE / REVISION_CONFLICT / POSITION_RECONCILIATION_REQUIRED / RECONCILIATION_REQUIRED` 含义不同；不要清空数据库或把报错改为成功来解除阻断。

## 4. 相对持有情景测算

仅计算用户输入的一个同币种、同起点/估值时点、无外部资金流及公司行为情景：

```text
现金变化 = 卖出数量×卖出均价 - 买入数量×买入均价 - 全部费用
相对持有净值差 = 现金变化 + (买入数量-卖出数量)×统一估值价
```

未发生的一腿填0数量、空价格；未配对数量仍计入估值。正配对差价可能因费用为负，相对持有也可能因卖飞而为负。结果不进入策略胜率、模型校准、真实绩效或可信准入。

## 5. 只读审计与备份

```powershell
py -3.14 -m stock_tracker.portfolio_planning audit --database "D:\StockTrackerPrivate\planning.sqlite" --store-id "真实store_id"
```

审计会读取并重放命令，不输出持仓细节、不访问Provider、不创建缺失文件。任何数据库恢复/文件替换都需要停止原引擎后重新打开验证；不要运行同一个计划库的离线克隆作为另一个活跃账户。

本版没有自动迁移、日志压缩或导出升级命令。命令、计划和持仓分配有明确容量上限；日志最后三条额度专门保留给三种币种的一次性全量对账，此时不再新建、修改或逐笔取消，不能截断历史记录。备份必须在引擎关闭且连接已退出后进行，避免复制活动中的SQLite文件；不要把此库上传公开Git或静态站点。

## 6. 验证与证据

固定测试入口：

```text
py -3.14 -B -m unittest tests.test_portfolio_planning tests.test_planning_api -v
py -3.14 -B scripts/run_planning_integration.py
py -3.14 -B -m basedpyright --level error stock_tracker/portfolio_planning stock_tracker/api/planning_handlers.py stock_tracker/runtime_evidence
```

浏览器工具需要本地 Playwright；可由 `PLAYWRIGHT_MODULE` 或 `PLAYWRIGHT_PATH` 指定已安装路径。`PLANNING_QA_REPORT_DIR` 决定合成截图/JSON输出目录。集成脚本只创建临时两个数据库，失败退出非0，清理不改原生产库。

结果表、独立审查身份、提交树和保护哈希见本轮 `MULTIHORIZON-P1-P3-VALIDATION.md` 及仓库外 evidence package。前端自动验收不等于完整无障碍或真实多交易日验收；内部hash日志不是独立数字签名。
