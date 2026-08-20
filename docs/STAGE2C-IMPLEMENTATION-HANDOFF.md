# Stage 2C Corporate Action Exact-Raw Adapter Implementation Handoff

> 日期：2026-08-17
> 工程状态：`READY_FOR_FULL_GATES`
> 数据状态：`CANDIDATE_ONLY / CONTRACT_ONLY / SYNTHETIC_VALIDATED`
> 许可状态：`LICENSE_PENDING`
> 研究级状态：`T3_NOT_REACHED`

## 1. 交付文件

```text
stock_tracker/quant/data/corporate_action_adapter.py
stock_tracker/quant/data/__init__.py
scripts/capture_a_share_corporate_actions.py
tests_quant/test_corporate_action_adapter.py
tests_quant/fixtures/corporate_actions/valid_effective.json
tests_quant/fixtures/corporate_actions/proposed_incomplete.json
tests_quant/fixtures/corporate_actions/sample.pdf
tests_quant/fixtures/corporate_actions/error_page.html
docs/STAGE2C-CORPORATE-ACTION-ADAPTER-CONTRACT.md
```

Source-distribution gate 同时增加上述关键 Python/SQL 文件，避免文件只存在本地工作树却遗漏 Git。

## 2. Exact-raw 交付

Raw capture 绑定：

- official HTTPS owner/family/path；
- method 与 request payload digest；
- HTTP status；
- selected normalized headers；
- Content-Type 与 Content-Length；
- continuous non-cyclic official redirect chain；
- retrieved time；
- exact byte length 和 SHA-256；
- source version 与 raw format。

相同 bytes 复用 raw storage；provenance 改变时 raw descriptor ID 改变。相同 URL 返回不同 bytes 时生成新 artifact。

Raw/descriptor 读取会重新验证：

```text
path ↔ identity
length
SHA-256
selected fields
enum values
unknown/missing fields
```

## 3. Parser 与 Candidate

当前唯一结构化 parser：

```text
corporate-action-fixture-json-v1
```

它只接受 synthetic fixture，并严格拒绝：

- unknown/missing fields；
- noncanonical Decimal；
- bool/number-as-decimal；
- NaN/Infinity；
- duplicate action/revision；
- future source publication；
- raw bytes 与 capture identity 不一致；
- synthetic relabel。

HTML/PDF/XLS/XLSX 只 capture，不猜测条款，返回 extraction-required parse descriptor。

Candidate gaps 为派生字段，覆盖 proposal、unsupported action、missing dates/terms/reference binding/no-op 等情况。

## 4. PIT 与 revision

First observation：

```text
observed_at = known_at = retrieved_at
```

Date-only publication 保持 `date`，不制造 midnight。

单份 artifact 不要求自带完整 predecessor；跨 artifact resolver 才检查：

```text
cycle
missing predecessor
disconnected terminal
future revision visibility
input-order independence
```

后续 cancellation 只影响其 known/usable 之后的 as-of。

## 5. Candidate → Core

转换始终：

```text
verified = false
```

并把 raw artifact/descriptor IDs 写入 source note。

只有支持的 action type 与有 ex-date 的候选可转换。proposal/approval 不会成为 EFFECTIVE。Core 再次校验经济条款，因此 Adapter gap 不能绕过 Stage 2B。

## 6. Offline CLI

```text
python scripts/capture_a_share_corporate_actions.py \
  --output-root <candidate-root> \
  --input-file <local-file> \
  --url <one-explicit-official-url> \
  --source-owner SSE \
  --source-family SSE_LISTED_COMPANY_ANNOUNCEMENT \
  --source-version <version> \
  --raw-format JSON
```

CLI：

- 不发现或抓取 URL；
- 不接受 DB path；
- 不 apply migration；
- 不提供 verified/complete/trust/research-grade 开关；
- 输出 evidence boundary；
- output root 不安全时非零退出。

## 7. 定向验证

```text
python -m unittest discover -s tests_quant -p "test_corporate_action_adapter.py" -v
25 / 25 PASS
```

验证包括：

- exact raw round-trip 和 same-size tamper；
- same URL changed bytes；
- same raw/provenance separation；
- descriptor/key tamper；
- owner/family/domain/redirect/status/content-type/content-length；
- HTML error page；
- strict JSON/Decimal；
- direct parser bytes/capture binding；
- PDF extraction required；
- proposal no-factor；
- missing terms derived gaps；
- revision as-of/cycle/missing/disconnected；
- symbol change/stable instrument；
- synthetic relabel；
- offline CLI/no DB/trust switches。

## 8. 待最终记录的门禁

提交前补录：

```text
full tests_quant
full tests
compileall
quant smoke
synthetic benchmark
migration dry-run
pip check
ruff
git diff --check
DB SHA before/after
committed-tree validation
```

## 9. 明确未完成

```text
真实 SSE/SZSE/CNINFO 网络抓取与覆盖
许可闭环
PDF/XLS/XLSX extractor
真实多来源 reconciliation
真实 company-action coverage report
真实 adjusted bars
T2/T3 promotion
```

必须继续保持：

```text
CANDIDATE_ONLY / CONTRACT_ONLY / SYNTHETIC_VALIDATED
LICENSE_PENDING
T3_NOT_REACHED
```
