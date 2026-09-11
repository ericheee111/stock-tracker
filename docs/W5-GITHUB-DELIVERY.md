# W5 GitHub 受控发布记录

日期：2026-09-11。状态：`GITHUB_DELIVERED / B1_NOT_STARTED`。

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


## GitHub 已确认的交付

- 实现及交接同步提交：`0af6c2ac84f9e82134d62e190987ff59f81f3885`。
- 对应 tree：`86bf5647227a68ab6e4b04c63cdda69583ca01e4`。
- 普通 push `HEAD:refs/heads/main` exit 0；随后在线 `git ls-remote` 与本地 origin/main
  均精确等于上述提交。旧远端 `082a6dac310388ec10c8a432427a40e275bcd7ae` 已快进，未 force、未重写历史。
- 原本机 main 仍为 `082a6dac310388ec10c8a432427a40e275bcd7ae`；其 HEAD/status、原 tracked/untracked 文件和
  DB/WAL/SHM 散列全部与发布前相同，原其他七类保护对象的实际清单见仓外记录。
- 本段及 W5/ChatGPT 交接完成状态作为后续纯文档提交推送；最终远端 SHA/tree 与
  再次保护核验写入仓外 `FINAL-DELIVERY.json`，不将本文件已发布前驱 SHA 冒充最终 HEAD。
- 未启动 GitHub Pages workflow（当前仅 workflow_dispatch），没有网站部署、Engine
  启用或真实数据采集；暂不开始 B1。

记录工具曾因 NUL 分隔清单的尾部空元素而中止提交前校验；已修正仓外脚本，明确五文件
清单后才提交。一次辅助命令因包含 NUL 被执行器拒绝，未执行任何 Git 动作。产品源码未变。
