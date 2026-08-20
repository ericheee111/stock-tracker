# Stage 2C：A 股公司行为 Exact-Raw Evidence Adapter 合同

> 日期：2026-08-17
> 工程状态：`IMPLEMENTED / SYNTHETIC_VALIDATED`
> 数据证据状态：`CANDIDATE_ONLY / CONTRACT_ONLY`
> 许可状态：`LICENSE_PENDING`
> 研究级状态：`T3_NOT_REACHED`

## 1. 阶段目标

Stage 2C 的任务是把公司行为来源处理拆成可审计的三个层次：

```text
exact raw bytes
→ immutable raw/parse descriptor
→ unverified candidate facts
```

本阶段不构建爬虫、不宣称全市场覆盖，也不把候选直接写入生产 SQLite 或晋级为可用于真实回测的 verified fact。

## 2. 冻结来源边界

当前代码只允许显式的官方 HTTPS owner/family：

```text
SSE_LISTED_COMPANY_ANNOUNCEMENT
SSE_ANNOUNCEMENT_ATTACHMENT
SZSE_LISTED_COMPANY_ANNOUNCEMENT
SZSE_DISCLOSURE_ATTACHMENT
CNINFO_DISCLOSURE_ATTACHMENT
```

owner domain 分别冻结为：

```text
sse.com.cn
szse.cn
cninfo.com.cn
```

URL path allowlist 只用于防止来源身份降维，不代表这些路径、参数或接口已经获得长期稳定性和许可证明。Stage 2C 不做自动 discovery，也不把动态查询参数冻结成稳定公共 API。

## 3. Exact raw capture

`capture_corporate_action_raw()` 在任何解码或规范化之前保存原始字节。

Raw identity 绑定：

```text
request_url
request_method
request_payload_digest
response_status
selected response headers
content_type
redirect_chain
retrieved_at
source_owner
source_family
source_version
raw_format
byte_length
raw_sha256
```

关键规则：

- `artifact_id = SHA-256(exact raw bytes)`；
- 相同 URL、不同 bytes 必须生成不同 artifact；
- 相同 bytes 可以复用 raw storage，但不同 provenance 生成不同 descriptor；
- `Content-Length` 若存在必须与 exact bytes 一致；
- HTTP error、Content-Type mismatch 和 HTML error page 失败关闭；
- redirect 必须保持在 owner 官方域名，链必须从 request URL 连续，cycle 禁止；
- descriptor path 与 descriptor ID、raw storage path 与 artifact ID 必须互相匹配；
- 读取时一次性读入 bytes 后同时检查 length 和 SHA，避免先 stat/hash 后再次读取造成 TOCTOU 身份漂移。

## 4. 时间合同

对于非独立时间权威支持的 first observation：

```text
observed_at = known_at = retrieved_at
```

来源网页或公告中的发布时间单独保留：

```text
source_published_at
source_published_granularity
```

粒度：

```text
DATE     只允许 YYYY-MM-DD，保持 Python date
SECOND   必须是 timezone-aware datetime
UNKNOWN  published value 必须为 null
```

日期粒度绝不伪造 `00:00:00`。来源发布时间不得晚于系统 `known_at`，但也不能用来把 `known_at` 回填到抓取之前。

## 5. 解析边界

### 5.1 当前可解析格式

Stage 2C 只实现一个严格、离线、synthetic-only JSON fixture schema：

```text
stage2c-corporate-action-fixture-v1
```

其用途是验证：

- PIT 时间和 revision 图；
- Decimal、日期、身份和来源绑定；
- 生命周期与缺口；
- deterministic candidate/document identity。

该 parser 永远输出：

```text
synthetic_fixture = true
verified = 不存在
complete = 不存在
trust_tier = 不存在
promotion = 不存在
```

将 fixture 的一个 flag 改成 `false` 不能把它重标为真实证据。

### 5.2 非结构化格式

以下格式可以 exact-raw capture：

```text
HTML
PDF
XLS
XLSX
```

但本阶段不进行启发式解析。Parse Descriptor 返回：

```text
EXTRACTION_REQUIRED_<FORMAT>
```

PDF/XLS/XLSX 不会因为文件扩展名或文本片段而被猜成公司行为条款。未来 extractor 必须单独版本化、内容寻址，并绑定 raw descriptor/artifact。

## 6. Candidate schema

候选绑定：

```text
action_id
instrument_id
identity_fact_id
symbol-at-event
market/exchange
action_type
lifecycle
source publication time/granularity
observed/retrieved/known/usable time
record/ex/payment/share-listing/effective dates
exact Decimal terms
revision_id / supersedes_revision_id
source URI / owner / family / version
raw_artifact_id / raw_descriptor_id
parser_version
```

### 6.1 Lifecycle

候选层区分：

```text
PROPOSED
APPROVED
IMPLEMENTATION_ANNOUNCED
EFFECTIVE
COMPLETED
CANCELLED
CORRECTED
```

只有 `EFFECTIVE / COMPLETED` 可以映射为 Stage 2B core `EFFECTIVE`；proposal/approval/implementation announcement 最多映射为 `ANNOUNCED`，因此不能产生 adjustment factor。

### 6.2 Action type

候选层保留：

```text
CASH_DIVIDEND
STOCK_DIVIDEND
SPLIT
REVERSE_SPLIT
RIGHTS_ISSUE
PLACEMENT_OR_ISSUANCE
MERGER_OR_CONVERSION
COMBINED
OTHER
UNKNOWN
```

placement、merger/conversion、other、unknown 当前保持 explicit unsupported，不会猜测为 split 或 cash dividend。

## 7. Decimal 与字段严格性

经济字段必须是 canonical decimal string：

```text
"1"
"1.2"
"0.1"
```

拒绝：

```text
1.2
true
"1.20"
"01"
NaN
Infinity
负的比例/金额
```

JSON request digest 和 fixture/descriptor JSON 同样拒绝 NaN/Infinity。

Parser 对顶层和 row 采用 exact field set：未知字段、缺失字段、重复 action/revision、非法日期和身份均失败关闭。

## 8. 派生 gaps

Candidate `gaps` 和 Document `gaps` 均为 `init=False` 派生字段，不能通过构造或 `dataclasses.replace()` 注入/删除。

当前可能的 gap 包括：

```text
DATE_ONLY_PUBLICATION_NO_INTRADAY_PRECISION
ACTION_NOT_IMPLEMENTED
UNSUPPORTED_ACTION_TYPE_*
MISSING_EX_DATE
MISSING_RECORD_DATE
MISSING_EFFECTIVE_DATE
MISSING_AUTOMATIC_SHARE_RATIO
MISSING_SHARE_LISTING_DATE
MISSING_CASH_DIVIDEND_PER_SHARE
MISSING_RIGHTS_ENTITLEMENT_RATIO
MISSING_RIGHTS_SUBSCRIPTION_PRICE
MISSING_CURRENCY
MISSING_REFERENCE_PRICE
MISSING_REFERENCE_PRICE_SNAPSHOT_ID
NO_EFFECTIVE_ECONOMIC_TERMS
EXTRACTION_REQUIRED_*
```

Gap 不会自动填补。缺少配股价、参考价、reference snapshot 或 listing date 时，不会生成猜测因子。

## 9. Revision 与 as-of resolver

单份更正公告不一定同时包含被替代的旧公告。因此：

- 单个 document 只验证其自身 row schema 和 duplicate action/revision；
- 跨 artifact 汇总时由 `resolve_corporate_action_candidates()` 验证完整 revision graph。

Resolver 按以下 stream identity 分组：

```text
(instrument_id, source_owner, source_family, source_version, action_id)
```

并验证：

- future known/usable revision 不进入过去 as-of；
- cycle；
- missing predecessor；
- disconnected multiple terminals；
- input order independence。

后续取消不会改写取消公布之前的 Snapshot。

## 10. Candidate → core 边界

`CandidateCorporateAction.to_core_fact()`：

- 始终设置 `verified=false`；
- source note 绑定 raw artifact/descriptor IDs；
- 缺少 `ex_date` 失败；
- unsupported action type 失败；
- core contract 再次验证 no-op、rights、currency、reference snapshot 和 date semantics。

因此 Adapter 本身没有 Trust 晋级能力。

## 11. Offline CLI

入口：

```text
scripts/capture_a_share_corporate_actions.py
```

CLI 仅支持：

- 一个本地 input file；
- 一个显式官方 URL；
- 一个显式 output root；
- exact raw + descriptor 写入；
- synthetic JSON deterministic replay 或 extraction-required 结论。

CLI 不提供：

```text
crawler/discovery
bulk harvest
database path
migration --apply
verified/complete/trust-tier/research-grade switches
model training
```

不安全 output path 返回非零退出码。

## 12. 证据边界与下一步

本阶段测试只证明工程合同，不证明官方真实数据覆盖或许可。

必须保持：

```text
CANDIDATE_ONLY / CONTRACT_ONLY / SYNTHETIC_VALIDATED
LICENSE_PENDING
T3_NOT_REACHED
```

后续 Stage 2D 应首先完成：

1. 独立审查真实 SSE/SZSE/CNINFO artifact 的许可与稳定性；
2. 版本化 PDF/XLS/XLSX extraction descriptor；
3. 多来源 action reconciliation 与 coverage gaps；
4. 将真实 reference price snapshot、Calendar、Universe 与 raw Bar Snapshot 联合绑定；
5. 在真实数据证据闭环前仍禁止生成 research-grade adjusted bars。
