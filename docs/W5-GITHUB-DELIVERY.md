# W5 GitHub 受控发布记录

日期：2026-09-11。状态：`REVIEW_PASSED / USER_AUTHORIZED / RELEASE_VALIDATED / REMOTE_CONFIRMATION_PENDING`。

## 精确身份与授权范围

- GitHub：`ericheee111/stock-tracker`，目标 `refs/heads/main`。
- 发布前已在线核验远端：`082a6dac310388ec10c8a432427a40e275bcd7ae`。
- 审查候选：`139ce41baea404c9dd54798562e5f3ad85ee73f8`。
- 审查 tree：`e1bfc6b2465e0e2b48ba1a1d67b087f071a7ecf6`。
- W5 基线：`788ffdb1cc3e770f99d63bdd10d923f46d9b1ddd`。
- 2026-09-11 ChatGPT 独立审查：PASSED；T1/T2/U1/U2/U3 CLOSED。
- 用户授权同步交接/发布记录、安全合并并 push；暂不开始 B1。

原 main 是候选祖先。独立发布分支 `codex/w5-release` 从已核验 origin/main 创建，
通过 `git merge --ff-only` 接入完整候选及其 Stage 4G.1 依赖历史，无冲突、无历史重写。
后续仅同步交接及本发布记录；实现、测试、UI 与精确审查候选保持相同 Git blob。

## 工作区保护

原 main 的 HEAD、dirty status、现存 tracked/untracked 文件散列，以及生产 DB/WAL/SHM
只读散列均在操作前后核对。原 Codex、W5、四个 WB 工作树保留。没有 reset、clean、stash、
生产 SQLite 连接或迁移。发布只从隔离工作区普通 push `HEAD:refs/heads/main`，不 force。
本机 dirty main 的分支指针故意保持发布前位置；更新的是远端 main 与 origin/main。
不要在保留的工作区盲目 pull/reset；其 UI 内容已作为独立 overlay 纳入审查候选。

## 验证口径

独立审查先前已通过 Runtime 739 passed + 1 skipped、Quant 724、相邻 114、WB1 36、
WB2 36、UI 54、Today Mock/临时 API Today/Portfolio 17/17/13。657 个文件与 archive/blob
核对一致。以上是审查时证据，不冒充本次发布门禁；本次实际门禁与远端确认在下方补记。

证据分别保存在仓外 `w5-integration-139ce41/CHATGPT-REVIEW-20260911` 与
`w5-release-20260911`，包含命令 argv/cwd/退出码、完整日志和保护对象散列。
历史运行器失败和 ResourceWarning 不删除，不把生产 :8080 跳过计作通过。

## 未改变的边界

Runtime Artifact/Outbox Worker 的条件接线不等于自动 Outcome Collection 完成。
B1 物理 Store、Market Path/B2、Trusted Admission、真实 Coverage/战绩均未在本发布实施。
UI 行情、账户和持仓测试使用 synthetic fixture/临时 SQLite；没有真实成交或投资表现声明。
自动交易保持关闭；此次 GitHub 源码发布不是网站部署或生产 Engine 启用。


## 本次发布门禁（2026-09-11）

所有最终门禁 exit 0：标准 unittest Runtime 740 testsRun（739 passed、1 skipped），
Quant 724，WB1/WB2/R4.1 合并专项 83，相邻 114，source distribution/no-bytecode 2。
UI 单次矩阵 54/54，格式 49，U1/U2/U3 unit 5，runner 实际对照 8；
Today Mock 17、临时真实 API Today 17、Portfolio CRUD 13。
Ruff、runtime_evidence 全包 basedpyright、短外部 pycache compileall、pip check、
Quant smoke、synthetic benchmark 与 diff 检查均通过。

完整 Runtime 的生产 :8080 探针不可达而跳过，既有告警保留。首次发布范围核验因 Git
默认转义中文路径而失败；改用 NUL 分隔原路径后通过，只修正仓外核验脚本，未改候选源码。
实际方法计数与 subcase 不相加；成功 subcase 数未由标准 unittest 单独计量。

本次提交明确只纳入五份文档：CHATGPT_HANDOFF.md、docs/HANDOFF.md、PRD、
docs/W5-INTEGRATION-HANDOFF.md 和本文。正式推送前再验证 fresh committed checkout
导入、分发及实现/测试/UI 与已审查候选的相同 blob。远端确认结果随后追加。
