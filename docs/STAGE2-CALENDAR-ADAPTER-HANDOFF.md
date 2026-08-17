# Stage 2A Agent B：A 股 Calendar exact-raw Adapter Handoff

> 日期：2026-08-14
> 状态：`CONTRACT_ONLY / SYNTHETIC_VALIDATED / LICENSE_PENDING`
> Trust 结论：Adapter 不分配 Trust Tier；当前产物只能作为 `T2 candidate evidence` 的工程输入，不是 T2，更不是 T3

## 1. 修改文件

```text
stock_tracker/quant/data/calendar_adapter.py
scripts/capture_a_share_calendar.py
tests_quant/test_calendar_adapter.py
tests_quant/fixtures/calendar/*.html
docs/STAGE2-CALENDAR-ADAPTER-HANDOFF.md
```

未修改 `stock_tracker/quant/core/**`、`stock_tracker/quant/storage/**`、`stock_tracker/quant/data/__init__.py`、Agent C/D 文件或 `data/stock_tracker.db`。未执行 Git add/commit/merge/push/reset/clean/checkout/switch/restore/stash。

## 2. 数据格式

### Exact raw

Raw bytes 以 SHA-256 内容寻址保存：

```text
raw/exchange-calendar/{sse|szse}/{raw_sha256}.{html|pdf|docx|xls|xlsx}
```

每次 HTTP 捕获另有不可变 descriptor：

```text
descriptors/exchange-calendar/{descriptor_id}.json
```

Descriptor schema 为 `a-share-calendar-raw-capture-v1`，绑定：

- raw content SHA-256、byte length、storage key；
- request URL、GET/POST、参数/请求体 canonical digest；
- HTTP status、选定 response headers、Content-Type；
- 完整 redirect hops；
- UTC `retrieved_at`；
- source owner、source family/version；
- parser version、raw format。

`artifact_id` 是 exact bytes 的 SHA-256；`descriptor_id` 绑定本次请求、响应和版本元数据。同 bytes 可以复用同一 raw 文件，但不同 capture/parser provenance 产生不同 descriptor。同 URL 返回不同 bytes 必然产生新 artifact 和新 descriptor，不覆盖旧文件。

### Candidate facts

`calendar-html-table-v1` 是纯函数 parser：

```text
raw bytes + CalendarProvenance + parser version
  -> CalendarCandidateDocument
  -> CandidateCalendarFact[] + explicit gaps
```

Candidate fact 显式保存：

```text
exchange / civil_date / status / session_kind / open_time / close_time
notice_id / notice_type
source_published_at / source_published_granularity
observed_at / retrieved_at / known_at / usable_from
effective_from / effective_to
revision_id / supersedes_revision_id
source_uri / raw_artifact_id / parser_version
source_owner / source_family / source_version
```

`assemble_calendar_candidates()` 输出可直接交给现有 `TradingCalendar` 的 `CalendarCoverage` 和 `CalendarDay[]`，但二者固定 `verified=False`。结果没有 `complete` 或 `trust_tier` 字段。

## 3. 来源假设

只接受 Agent A 冻结的官方来源族：

- SSE `closed/list` archive；
- SSE official annual/holiday/temporary/technical notice detail；
- SSE official notice attachment；
- SZSE `notice/general`；
- SZSE official notice detail；
- SZSE official notice attachment。

URL 必须是对应 owner 的官方 HTTPS 域名和冻结路径族。不使用未文档化网页 API、猜测参数、AKShare 包装结果、第三方 JSON/CSV 或解析后重序列化内容作为 exact raw。

HTML、PDF、DOCX、XLS、XLSX 都可以独立 capture；公告 HTML 与附件必须分别调用 capture，分别形成 artifact。当前确定性事实 parser 只冻结 `calendar-html-table-v1`。PDF/DOCX/XLS/XLSX 可以 exact capture，但在没有冻结、经 fixture 证明的 source-specific parser 前只报告 parser gap，不产生 candidate facts。

当前 HTML parser 使用严格 daily/exception table 合同，不冻结 SSE/SZSE 内部动态页面 selector。真实 SSE/SZSE 历史公告正文、PDF 和 Office 附件的 source-specific selector/parser 尚待在许可确认和 representative exact-raw fixtures 获得后逐版本增加。

## 4. Exact-raw 规则

- 先原子保存 bytes，再允许 parser 运行；解析失败仍保留 raw 供隔离和审计。
- Storage key 只由 raw SHA-256 决定；旧 bytes 永不原地覆盖。
- Descriptor 自身内容寻址，loader 重算 descriptor identity。
- Loader 同时核对 byte length 和 SHA-256；等长篡改也失败。
- Descriptor 拒绝 unknown fields，防止注入 `verified`、`complete` 或 Trust 字段。
- HTTP 4xx/5xx 和 HTTP 200 error page 均不能产生 calendar facts。
- 附件和公告页面是独立 artifact，不把解析后的 JSON/CSV 当 raw。
- Raw 数据只写显式 `--output-root`，CLI 不导入或访问生产 SQLite。

## 5. `known_at` / `usable_from` 策略

- `source_published_at`、`observed_at`、`retrieved_at`、`known_at`、`usable_from`、effective interval 和 revision chain 分开保存。
- `DATE` publication 只能接收 Python `date`；传入午夜 `datetime` 会失败，禁止伪造 `00:00:00`。
- `SECOND` publication 必须是 timezone-aware datetime。
- `known_at` 不得晚于可证明 `observed_at`；`observed_at` 不得晚于 `retrieved_at`。
- 强制 `known_at <= usable_from`。
- CLI 未显式给 `known_at` 时，只能以本次 `observed_at/retrieved_at` 作为首次可证明已知时间。
- CLI 强制显式提供 `--usable-from`。无法证明盘前可得时，调用方必须传下一交易 session 的保守时间；Adapter 不猜测或提前。
- annual exception 模式会把周末设为 CLOSED、工作日暂设 OPEN，并显式产生 `WEEKDAY_OPEN_BASELINE_INFERRED` 与 `TEMPORARY_AND_TECHNICAL_NOTICE_COVERAGE_UNPROVEN` gaps。
- 后续 holiday/temporary/technical/revision 若改变既有日期，必须给 `supersedes_revision_id`；低优先级 notice 不能覆盖已存在的高优先级修订。
- 所有 civil date 都必须存在。Explicit daily 输入缺日、重复、乱序或越界均失败关闭。

## 6. 验证结果

执行：

```text
python -m unittest discover -s tests_quant -p "test_calendar_adapter.py" -v
```

结果：23 tests，全部通过。全部 fixtures 和 CLI 网络失败路径均离线；没有测试访问互联网。主车道 Review 额外补充：redirect chain 的每一跳都必须保持在对应 SSE/SZSE 官方 HTTPS 域名内，禁止从官方入口跳到第三方域后仍按官方 artifact 记账；CLI 现在除 raw descriptor 外还持久化独立 `a-share-calendar-parse-descriptor-v1`，绑定 notice/PIT/effective/revision provenance，D 可从 parse descriptor + exact raw 完整重放同一 Calendar document。

执行：

```text
python -m compileall -q stock_tracker tests tests_quant scripts
```

结果：exit 0。

三个 Python 变更文件的当前 LSP diagnostics 均为 `No diagnostics found`。HTML fixture 的 Biome LSP 未安装；未擅自安装全局依赖，HTML 由实际 parser 回归覆盖。

执行：

```text
git diff --check
```

结果：exit 0。命令仅报告并行工作树中既有 tracked 文件的 LF→CRLF 提示，无 whitespace error；Agent B 新增未跟踪文件另经 trailing-whitespace 扫描。

手工 QA 使用 `annual_calendar.html` 的 exact bytes 驱动 CLI 主入口，并将 `_fetch` 替换为离线响应，避免互联网访问。结果：CLI exit 0，输出 64 字符 artifact/descriptor/document IDs、官方 source family、UTC `retrieved_at` 和四项显式 gaps；临时 output root 内观察到 1 个 raw file 与 1 个 descriptor JSON，随后自动清理。

## 7. 负向覆盖

离线测试覆盖：完整 Calendar、周末、法定休市、半日/特殊 session、annual + later holiday notice、temporary revision、同 URL bytes 替换、HTML error page、HTTP error、缺一天、重复日期、日期乱序、错误时区、DATE publication、未来 `usable_from`、`known_at > usable_from`、跨 source/version、等长 raw tamper、parser version change、unknown descriptor/table fields、附件独立 artifact、Adapter 不暴露 complete/Trust、CLI 缺 output root、CLI 网络失败 non-zero。

## 8. 当前最大 Trust Tier 与已知缺口

Adapter 自身的当前 Trust Tier：**未分配**。

当前能力上限：可生成带 raw provenance 和 gaps 的 **T2 candidate evidence**，但本交付只有 synthetic fixtures，不构成真实 T2 证据。

已知缺口：

1. 没有获得许可确认后的真实官方 raw bundle；
2. 没有冻结真实 SSE/SZSE 公告正文、PDF、DOCX、XLS/XLSX 的 source-specific parser；
3. 年度公告不能证明临时停市、技术停市和后续更正已无缺口；
4. SSE/SZSE 跨所和跨来源 reconciliation 未完成；
5. 公告 archive 分页、历史连续性、静默附件替换和首次捕获前旧版本仍未闭环；
6. 没有生成 verified/complete Calendar Snapshot；
7. 没有 T3 联合 Manifest、历史 Universe、Security Status 或 Corporate Action 绑定。

## 9. 许可边界

状态保持 `LICENSE_PENDING`。Raw bytes 不得提交 Git、公开再分发或向第三方提供。当前仓库只包含 synthetic HTML fixtures；CLI 真实 capture 必须写到明确的仓库外或被可靠忽略的 output root，并遵守低频、内部审计、无再分发边界。技术可下载不代表获得长期保存、批量抓取或衍生数据授权。

## 10. 是否可交给 Agent D

**可以交给 Agent D 做 schema/fixture 级 reconciliation 与 gap-report 集成。** Agent D 可消费 `CalendarAdapterResult.coverage`、`days`、`candidate_facts`、`gaps`、descriptor ID 和 raw artifact ID。

交接不表示可以组装真实 verified/complete Snapshot，也不表示达到 T2/T3。Agent D 仍必须验证 descriptor/raw identity、civil-date coverage、source/version 混用、OPEN/CLOSED 冲突、revision chain、`known_at/usable_from` 和所有 unresolved gaps；其报告只能阻断或降低 Trust，不能升级 Trust。
