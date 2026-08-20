# Stage 2E：公司行为多源对账、覆盖与晋级资格合同

> 工程状态：`IMPLEMENTED / TARGETED_VALIDATED`
> 数据状态：`CANDIDATE_RECONCILIATION_ONLY`
> 许可状态：`LICENSE_PENDING`
> 证据等级：`T3_NOT_REACHED`

## 1. 目标

Stage 2E 比较多个 Stage 2D bound candidate bundle，产生冲突、覆盖与缺口报告：

```text
identity-bound candidate bundles
+ explicit logical-action mappings
+ candidate coverage claims
+ versioned reconciliation policy
→ deterministic reconciliation report
→ promotion eligibility for independent verification
```

Reconciliation 不能创造权威性、完整性或 Trust Tier。

## 2. 输入边界

只接收：

- Stage 2D `BoundCorporateActionCandidateBundle`；
- 显式 `CandidateActionMapping`；
- candidate-only `CoverageClaimCandidate`；
- `ReconciliationPolicy`；
- `as_of`。

Adapter、bundle 或 coverage claim 都没有 `verified=true` 或 `complete=true` 的接口。

## 3. 逻辑事件映射

不同来源的 source event ID 只能通过显式 mapping 归到同一 logical action：

```text
candidate_id
→ logical_action_id
+ mapping policy version
+ mapping note
```

禁止仅按当前 symbol、相同日期或相似金额自动合并。

以下情况失败关闭或产生 gap：

- 一个 candidate 有多个 logical mapping；
- mapping 引用不存在的 candidate；
- 同一来源 owner 下多个不同 action ID 被映到同一 logical action；
- 不同 stable instrument 被错误映到同一 logical action；
- policy version 不一致。

## 4. As-of 与 revision

所有 bundle/candidate 按 `as_of` 过滤：

- future bundle 保留在输入证据身份中，但不进入过去 report；
- future candidate 显式报告；
- 每个来源的 revision terminal 由显式 supersedes 图选择；
- cycle、missing predecessor、disconnected terminal 失败关闭；
- 后续更正或取消不会重写早期 report。

## 5. 对账字段

对同一 logical action 的可见 terminal candidates 比较：

```text
stable instrument / identity fact
lifecycle
action type
ex_date
record_date
payment_date
share_listing_date
effective_date
automatic share ratio
cash dividend
rights ratio and subscription price
currency
reference price
reference price evidence ID
source evidence and revision terminal
```

冲突类型是显式、内容寻址的 `ReconciliationConflict`。

## 6. Coverage Claim

Coverage claim 只是候选声明，必须绑定：

```text
instrument_id
source owner/version
start/end interval
known_at/usable_from
surveyed source event IDs
coverage note
license status
```

每个 terminal candidate 都必须找到唯一匹配 claim：

- instrument 一致；
- owner/version 一致；
- event date 位于 claim interval；
- source event ID 被 claim 明确列出。

无 claim、多个 claim、缺主源 claim 均阻断资格。

## 7. Promotion Eligibility

输出状态只有：

```text
NOT_ELIGIBLE
ELIGIBLE_FOR_INDEPENDENT_VERIFICATION
```

后者只表示可送独立验证，不表示：

```text
verified
complete
T2/T3
research-grade
model-ready
```

Policy 可要求：

- 指定 authoritative primary owner 的 candidate 和 coverage；
- 最小独立来源数；
- attachment evidence；
- reference-price evidence；
- 无字段冲突；
- 无 unresolved candidate/bundle gap；
- 唯一 revision terminal；
- license 已清除。

当前真实许可仍为 `PENDING`，因此真实样式输入必须保持 `NOT_ELIGIBLE`。

仅有一个明确 synthetic-only policy test 可以到达：

```text
ELIGIBLE_FOR_INDEPENDENT_VERIFICATION
```

而且 synthetic flag 不能改标为真实。

## 8. 确定性身份

内容寻址对象包括：

```text
action mapping
coverage claim
policy
conflict
reconciled logical action
promotion eligibility
reconciliation report
```

输入 bundle/source/order 不改变 report ID。任何候选、证据、时间、条款、coverage、license 或 policy 变化都会改变 ID 或失败关闭。

## 9. CLI

入口：

```text
scripts/reconcile_a_share_corporate_actions.py
```

CLI 采用严格 synthetic request schema，嵌套对象使用 exact field set。增加 `verified`、`complete`、`trust_tier`、`promotion` 等未知字段会失败。

CLI：

- 只读本地 JSON；
- 只写 report；
- 不访问网络或 SQLite；
- 不执行 migration；
- 不改变任何 Trust/coverage 状态；
- 不允许输出覆盖输入；
- 不同内容不能覆盖已有输出。

## 10. 证据边界

Stage 2E 证明的是 reconciliation 工程合同，不是市场事实完整性。继续保持：

```text
LICENSE_PENDING
T3_NOT_REACHED
NO_REAL_PROMOTION
```
