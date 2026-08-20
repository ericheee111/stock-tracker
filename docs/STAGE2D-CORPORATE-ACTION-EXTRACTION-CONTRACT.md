# Stage 2D：公司行为离线抽取与显式证券身份绑定合同

> 工程状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 数据状态：`CANDIDATE_ONLY`
> 许可状态：`LICENSE_PENDING`
> 证据等级：`T3_NOT_REACHED`

## 1. 目标

Stage 2D 把 Stage 2C 的不可变原始工件转换为来源原生的公司行为行，并通过显式映射绑定 Stage 2A 的稳定证券身份：

```text
Stage 2C exact raw capture
→ immutable extraction descriptor
→ source-native extracted rows
→ explicit source-security mapping
→ Stage 2A InstrumentIdentityFact validation
→ bound candidate bundle
```

本阶段不进行网络抓取、不写生产数据库、不把解析成功等同于权威、完整或研究级。

## 2. 抽取输入

抽取函数只能接收已有的 Stage 2C `CorporateActionRawCapture`。不能把任意裸 bytes 直接包装成公司行为证据。

### 2.1 冻结 HTML 表格合同

`FROZEN_HTML_TABLE` 使用标准库 `html.parser`，只接受：

```text
data-stage2d-schema="stage2d-corporate-action-html-v1"
```

它是 synthetic/offline 工程合同，不声明等价于交易所当前网页实现。

要求：

- strict UTF-8；
- 一个且仅一个目标表格；
- 固定且完整的表头；
- 不允许嵌套表格、嵌套单元格或未知标签；
- 行必须按规范顺序提供；Adapter 不会静默排序修复证据；
- 相同 source event/revision 不允许重复；
- 抽取输入必须与 Stage 2C exact raw bytes 完全一致。

### 2.2 PDF/XLS/XLSX 与人工审核

二进制附件不进行启发式解析。它们通过严格的结构化 extraction document 表达：

```text
stage2d-corporate-action-extraction-v1
```

结构化文档必须绑定：

- extractor name/version；
- extraction method；
- reviewer note；
- source row locator/page/sheet/cell range；
- source event/security identity；
- exact dates and Decimal terms；
- revision/supersedes；
- Stage 2C raw artifact/descriptor。

## 3. 来源行不得声明内部身份

来源原生行仅能包含：

```text
source_event_id
source_security_id
symbol evidence
market/exchange evidence
action type/lifecycle
dates and exact terms
revision ancestry
source locator
```

不得包含或自行声明：

```text
instrument_id
identity_fact_id
verified
complete
trust_tier
research_grade
```

未知字段直接失败，因此不能通过增加一个字段把来源行升级为内部事实。

## 4. 显式身份映射

`SourceSecurityIdentityMapping` 明确绑定：

```text
source owner
source_security_id
Stage 2A identity_fact_id
mapping policy version
known_at / usable_from
mapping status
```

绑定时验证：

- mapping 在 `as_of` 可见；
- identity fact 在 `as_of` 可见且可用；
- identity 在事件日期有效；
- symbol、market、exchange 与来源证据一致；
- mapping policy version 一致；
- 只有一个可见 mapping。

失败状态显式区分：

```text
UNBOUND
AMBIGUOUS
FUTURE
INACTIVE
IDENTITY_MISMATCH
```

这些状态不会生成 `CandidateCorporateAction`。

## 5. 代码变更、复用与退市

绑定依据是 `identity_fact_id + effective interval`，不是当前 symbol：

- 同一证券更名/换代码：使用事件日期有效的 identity；
- 旧证券退市：历史事件仍可绑定历史有效 identity；
- 新证券复用旧代码：不能继承旧证券公司行为；
- 重上市：必须有显式新 identity interval 和 mapping。

## 6. 时间合同

保持以下时间相互独立：

```text
source_published_at / granularity
Stage 2C retrieved_at
observed_at
known_at
usable_from
extracted_at
reviewer time/evidence note
effective dates
```

非独立时间权威支持的候选继续使用：

```text
observed_at = known_at = Stage 2C retrieved_at
```

来源发布日期不能回填系统知识时间；日期粒度不会制造午夜或秒级精度。

## 7. Revision 合同

`resolve_extracted_rows_as_of()` 使用显式 `revision_id / supersedes_revision_id` 图：

- 不采用字符串或数字外观排序；
- cycle、missing predecessor、disconnected terminal 失败关闭；
- terminal revision 先解析，再供后续日期范围与对账使用；
- 后续日期移动或取消不会让旧 revision 复活。

## 8. 派生身份

以下字段均由规范内容派生，不能通过构造器或 `dataclasses.replace()` 注入：

```text
row_id
document_id
document gaps
extraction descriptor ID/key
mapping_id
binding_id
bundle gaps
bundle_id
```

Extraction descriptor 绑定：

```text
Stage 2C raw artifact/descriptor
extraction payload SHA/key
extracted document ID
extractor name/version/method
reviewer note
extracted_at
synthetic boundary
```

写 descriptor 前会重新解析 extraction payload，并要求 replay document ID 与调用方 document 完全一致。

## 9. Candidate bundle

`BoundCorporateActionCandidateBundle` 同时绑定：

```text
extracted document ID
extraction descriptor ID
raw artifact/descriptor IDs
mapping policy
as_of
binding IDs
candidate IDs
derived gaps
```

候选 raw evidence 必须与 bundle 一致；候选内部身份必须有对应的 `BOUND` 结果。

## 10. CLI

入口：

```text
scripts/extract_a_share_corporate_actions.py
```

只支持：

- 本地 extraction input；
- 已存在的 Stage 2C raw descriptor；
- 显式 artifact root；
- 不可变 extraction payload/descriptor 写入。

没有：

```text
URL fetch
crawler
database
migration apply
verified/complete/trust/promotion/model switches
```

## 11. 证据边界

本阶段只能声明：

```text
OFFLINE EXTRACTION CONTRACT IMPLEMENTED
IDENTITY BINDING CONTRACT IMPLEMENTED
SYNTHETIC ADVERSARIAL VALIDATION PASSED
```

不能声明：

```text
真实交易所附件已完整解析
真实公司行为覆盖完整
许可已清除
T2/T3 已达到
真实复权正确
```
