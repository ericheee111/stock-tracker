# Stage 2D Implementation Handoff

> 状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 许可：`LICENSE_PENDING`
> 证据：`T3_NOT_REACHED`

## 交付

```text
stock_tracker/quant/data/corporate_action_extraction.py
scripts/extract_a_share_corporate_actions.py
tests_quant/test_corporate_action_extraction.py
tests_quant/fixtures/corporate_action_extraction/
docs/STAGE2D-CORPORATE-ACTION-EXTRACTION-CONTRACT.md
```

## 已实现

- Stage 2C raw capture 强绑定；
- frozen HTML table parser；
- PDF/XLS/XLSX structured extraction boundary；
- extraction payload/descriptor/document 内容寻址；
- source-native row schema；
- explicit Stage 2A identity mapping；
- historical identity interval validation；
- symbol rename/reuse/delisting protection；
- revision graph validation；
- candidate-only bound bundle；
- offline-only CLI；
- derived gap/ID anti-forgery checks。

## 关键审查修复

1. 删除了 revision resolver 中依赖当前时间的身份计算，改为固定 UTC 时间语义。
2. 不再静默排序来源行；乱序证据直接失败。
3. Extraction descriptor 增加 `extracted_document_id`，写入前重放 extraction payload。
4. Bound bundle 增加 `extraction_descriptor_id`，并验证 candidate raw evidence 和 BOUND identity 支持。
5. future mapping 与完全缺失 mapping 分开报告。
6. 所有 `init=False` ID 改用显式 payload 计算，避免未初始化字段自引用。

## 定向验证

定向测试覆盖：exact raw、descriptor、payload replay、HTML/structured extraction、未知字段、UTF-8、Decimal、revision、身份缺失/歧义/未来/失效/不匹配、代码复用和退市、CLI 安全与不可变写入。

最终精确测试数量与全量门禁结果记录在：

```text
docs/STAGE2D-STAGE2F-INDEPENDENT-REVIEW.md
```

## 未完成边界

- 真实 SSE/SZSE/CNINFO 网页或附件等价性证明；
- OCR；
- 通用 PDF/XLS/XLSX 自动解析；
- 真实许可和覆盖闭环；
- Trust Tier 晋级。
