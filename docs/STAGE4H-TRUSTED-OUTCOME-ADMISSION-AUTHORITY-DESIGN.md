# Stage 4H — Trusted Outcome Admission Authority

状态：`DESIGNED / BLOCKED_ON_STAGE4G1_SIGNING_TRUST_ANCHOR_TIME_ANCHOR`

日期：2026-08-31

## 1. 目的

Stage 4G Core 与未来 Stage 4G.1 Operational Adapter 只能产生候选 Outcome 证据。它们可以证明内部结构、事件链和恢复语义，但不能自行证明：

- runtime episode 确实来自生产决策边界；
- execution/fill 确实来自独立外部事实；
- market path、交易日历、证券状态和规则绑定完整；
- evidence 未被同一操作者重写；
- admission 在当时已生效且后来是否被撤销。

Stage 4H 建立一个**独立、追加式、可签名、可撤销、Point-in-Time 可查询的 Outcome Admission Authority**。它不得修改 Stage 4G case 或 Stage 4F candidate record，而是发布独立 Admission Decision。

## 2. 前置条件

Stage 4H 不应在以下条件完成前进入正式实现：

1. Stage 4G Collection schema v3 的 R0 hardening 已完成独立审查并合并；
2. Stage 4G.1 能产生可审计 Runtime Decision Artifact；
3. episode fact ID、entry request time 和完整 path 不再完全依赖人工自报；
4. 至少一种 execution evidence type 有明确的 raw artifact 与 verifier；
5. 选择并固定签名验证后端；
6. Authority actor、权限、密钥轮换和撤销运维流程获批准。

未满足时允许实现纯合同/fixture，但状态必须保持：

```text
TRUSTED_OUTCOME_AUTHORITY_CONFIGURED = false
ADMITTED_OUTCOME_COUNT = 0
REAL_SCOREBOARD = INSUFFICIENT_REAL_EVIDENCE
```

## 3. 非目标

Stage 4H 不：

- 下单或控制 Broker；
- 修改 Stage 4G/4F immutable evidence；
- 根据候选对象中的 `verified=true` 自动批准；
- 因 evidence ID 是 SHA-256 就认定 evidence 真实；
- 因 Broker callback 来自同一机器就认定独立；
- 自动训练、晋级模型或调整策略权重；
- 直接计算并公开投资表现；
- 将撤销历史从账本删除。

## 4. Authority 边界与角色

建议角色：

```text
COLLECTOR
- 产生 Runtime/Market/Execution candidate artifacts
- 无 Admission 写权限

REQUESTER
- 组装 Admission Request
- 无批准权限

REVIEWER
- 审查证据与 blocker
- 可签署 review statement

APPROVER
- 根据策略签署 Admission Decision
- 不得是该样本的唯一 Collector/Requester

AUDITOR
- 只读验证全链、权限、签名和撤销状态

SYSTEM_MATERIALIZER
- 只消费已生效 Admission
- 无写入/批准权限
```

Live Outcome 首版建议至少双控制：Requester/Collector 与 Approver 分离。高风险市场或来源可配置双 Approver/quorum；任何 quorum 政策必须版本化并绑定 decision。

## 5. Authority Identity Registry

独立 Registry 记录：

```text
AuthorityPrincipal
- principal_id
- role set
- public_key_id
- algorithm
- public_key fingerprint
- valid_from
- valid_to
- revoked_at
- revocation_reason
- issuer / registry decision
- record_id / signature
```

要求：

- key validity、role validity 和 decision effective time 按 `as_of` 判断；
- key rotation 产生新记录，不覆盖旧 key；
- key compromise 通过撤销记录表达；
- 撤销是否追溯影响既有 decision 必须由版本化 policy 决定；
- private key 不进入仓库、数据库或日志；
- Authority Store 只保存 public material、fingerprint 和 opaque signer reference。

### 5.1 Genesis Root of Trust

Registry 不能通过“任意本地用户写入第一个 public key”自我建立可信性。正式 Authority 必须有显式 genesis ceremony：

```text
- 预先批准的 offline root public key fingerprint
- genesis policy ID / bytes
- initial principal/key/role set
- authority store stable ID
- creation time evidence
- quorum/signature requirement
- printed/offline recovery record
```

实现要求：

- root fingerprint 通过独立配置/部署清单固定，不能仅从 Authority DB 自身读取；
- genesis record 必须由预先信任的 root 或离线 quorum 签署；
- 未提供外部 trust anchor 时只能标记 `LOCAL_SELF_ASSERTED_ROOT`，不得声明独立可信 Authority；
- root rotation、recovery 与 compromise 需要单独、多签、append-only ceremony；
- backup restore 必须验证 stable store ID、genesis record 和全链，不得因为路径相同就接受新 Authority；
- 测试 fixture root 与正式 root 必须使用不同 domain/ID，fixture decision 永远不能进入 real scope。

## 6. 签名后端

Python 标准库不能提供符合本阶段要求的现代非对称签名工作流。不得用以下方式冒充 Authority signature：

```text
caller-provided SHA
plain HMAC shared by collector and approver
UUID/token
record_hash alone
verified=true
```

实施前必须选择一种明确后端，例如：

- 固定版本、审计来源的 Ed25519 实现；
- 操作系统/硬件 keystore 或外部 signing service；
- 具备离线 public-key verification 的企业 signer。

选择要求：

- 非对称签名；
- canonical bytes；
- algorithm/key ID 显式绑定；
- offline verifier；
- deterministic negative tests；
- key rotation/revocation；
- secret zeroization/最小暴露；
- 依赖许可证与供应链审查。

在签名后端未固定前，Authority 只能运行 `CONTRACT_ONLY`，不得产出 ADMITTED。

### 6.1 Signed Envelope 与 Domain Separation

签名不能只覆盖裸 `decision_hash`。必须签署 canonical envelope：

```text
signature_domain = "stock-tracker/outcome-admission/<environment>/<schema>"
authority_store_id
record_type
record_id
policy_id
principal_id / role
key_id / algorithm
created_at
nonce
payload_sha256
previous_authority_hash
```

要求：

- production、shadow、development、fixture 使用不同 domain；
- Admission、Revocation、Registry、Policy、Checkpoint 使用不同 record type/domain；
- verifier 重建 canonical bytes，不接受调用方直接传入“已计算 hash”；
- signature 必须绑定 Authority Store stable ID，防止从另一个 Store 复制；
- nonce/record ID 全局唯一并进入 append-only chain，防 replay；
- key/algorithm confusion、unknown fields、duplicate JSON keys、non-canonical bytes 和 signature malleability 均失败关闭；
- 原始签名字节、public key、algorithm parameters 和 verifier version 必须可审计。

## 7. Candidate Evidence Bundle

每个 Admission Request 必须绑定 exact candidate bundle：

```text
AdmissionCandidateBundle
- request_id
- Stage 4G collection schema
- collection database identity / audit ID
- case_id
- runtime_episode_fact_id
- runtime_episode_id
- decision_snapshot_id
- collection event hashes
- PATH point IDs / PATH event fact IDs / collection observed times
- exit intent decision snapshot ID
- exit request path-prefix count / prefix ID / schema version
- FINALIZATION_PREPARED event hash
- FINALIZED event hash
- Stage 4F ledger target identity
- outcome_id
- outcome signal ID
- record_hash / append_order
- ledger audit ID
- runtime decision artifact ID
- execution evidence manifest ID
- market path manifest ID
- calendar snapshot ID
- security status / universe identity IDs
- market/instrument rule snapshot ID
- cost schedule ID
- parser/schema/source identities
- evidence acquisition/known/usable timestamps
- requested admission scope
- candidate_bundle_id
```

Authority 必须从原始 stores 重新读取并重算 bundle，不能只相信 Requester 提交的 JSON。

## 8. Evidence Verification

### 8.1 Runtime episode

验证：

- Runtime Decision Artifact 存在且 hash/chain/audit 通过；
- artifact ID 等于 `runtime_episode_fact_id`；
- artifact 与 Stage 4G snapshot 完全一致；
- state/request/capture times timezone-aware 且 PIT 合法；
- artifact 不是 legacy/quarantined/synthetic，除非 admission scope 明确只允许 diagnostic。

### 8.2 Execution

按 evidence type 验证：

- PAPER：永不 admitted 为 real；
- MANUAL_ATTESTED：默认不足以单独 admitted；需要独立 corroboration policy；
- BROKER_CONFIRMED_READ_ONLY：验证 broker session、order/execution IDs、side/quantity/price/fees/timestamps、duplicate/gap/reconnect 和原始签名/传输证明；
- 多次 partial fill 必须完整聚合并能从 raw executions 重算；
- account identifier 只保存脱敏/opaque identity；
- execution source 与 Collector independence 必须有明确声明和 Authority evidence。

### 8.3 Market path

验证：

- entry 到 exit/horizon 的完整 session coverage；
- raw/bar artifact 全链与 parser/schema；
- Calendar、Security Status、Universe、Corporate Action 与 adjustment；
- price/volume/currency/unit；
- gap、停牌、无交易、休市、缺失数据、涨跌停与异常交易状态必须区分；不得伪造 OHLC 填补；
- `EXIT_REQUEST` prefix 必须由请求 event 前已经 durable append 的 exact PATH point/fact IDs 与 collection known time 重建，不能因较早 market timestamp 接受后来才 observed 的事实；
- target/stop/timeout reason 必须与请求前、horizon 内 first-touch 及版本化 terminal rule 一致；
- 同一 Stage 4G PATH point 双触发必须保持 blocker；只有更细粒度、可审计路径或 policy 明确允许且已冻结的 ambiguity evidence 才能解除。

### 8.4 Market rules与成本

验证：

- A 股 T+1、lot、price limit、停牌和费用；
- 港股 lot、延迟、价差、VCM 和费用；
- 美股 session、盘前盘后、费用和 corporate action；
- execution rule/cost schedule 在交易时点有效；
- 规则来源与 verified 状态符合 admission policy。

## 9. Admission 状态机

Authority 使用独立 append-only decision chain：

```text
REQUESTED
→ EVIDENCE_INCOMPLETE
→ READY_FOR_REVIEW
→ REVIEWED
→ ADMITTED | REJECTED
→ REVOKED | SUPERSEDED
```

每个状态变化是新记录，不修改旧记录。

### 9.1 `ADMITTED`

只表示该 candidate 在指定 scope、policy、effective interval 和 `as_of` 下通过可信准入。它不表示策略有效、盈利或可自动交易。

### 9.2 `REJECTED`

记录 blocker 和证据；后续新证据必须创建新 request/revision，不回写旧 decision。

### 9.3 `REVOKED`

保留原 Admission，但从 revocation policy 指定的生效时间起不再可用于 Scoreboard。原因包括 key compromise、source correction、broker reversal、path gap、身份错误、规则错误或审查撤销。

### 9.4 `SUPERSEDED`

用于新 decision 明确替代旧 decision；旧记录仍可按历史 `as_of` 查询。

## 10. Admission Decision

```text
OutcomeAdmissionDecision
- decision_id
- request_id / candidate_bundle_id
- outcome_id / record_hash
- state
- admission_scope
- policy_id / policy_version
- reviewer IDs
- approver IDs
- quorum result
- blocker codes
- evidence IDs
- known_at
- effective_from
- effective_to
- recorded_at
- previous_decision_hash
- authority_registry_snapshot_id
- signatures[]
- decision_hash
```

时间规则：

```text
all evidence known_at <= decision known_at
known_at <= effective_from
recorded_at >= known_at
as_of >= recorded_at for visibility
```

禁止回填一个早于 evidence 可知时间的 `known_at/effective_from`。

### 10.1 Trusted Time 与外部 Checkpoint

本地 wall clock、SQLite append order 和同一操作者的签名仍不能独立证明“某条 Admission 在过去某时已经存在”。若允许事后生成整条本地链并回填时间，PIT 可信性仍是自我声明。

正式 Authority 至少需要一种仓库外时间/透明度锚点：

- RFC 3161 或等价可信时间戳 token；
- 独立 signing service 返回的不可回填 receipt；
- 定期发布到只追加、公开可验证的 transparency/checkpoint channel；
- 离线第二设备按固定周期签署 checkpoint，并保存独立介质记录。

每个 checkpoint 绑定：

```text
authority_store_id
last_append_order
last_record_hash
registry_snapshot_id
policy_snapshot_id
checkpoint_created_at
external_time_evidence_id / receipt
```

规则：

- Admission 的最早可信存在时间不得早于覆盖它的第一个外部 checkpoint；
- 没有外部时间锚时必须显示 `LOCAL_TIME_NOT_INDEPENDENT`；
- 单机自签链可用于完整性和操作审计，但不能声称独立 PIT 证明；
- checkpoint 间隔、最大未锚定窗口和断档处理由版本化 policy 定义；
- 外部服务不可用时可继续收集 candidate，但 real Admission materialization 必须暂停或降级；
- Authority 不得把 Git commit time、文件 mtime 或普通 NTP 查询单独当作不可回填证明。

## 11. Policy

`OutcomeAdmissionPolicy` 必须版本化并至少定义：

- 允许的 market / strategy / evidence type；
- 最低 DataTrustTier；
- required evidence components；
- Calendar/Status/Universe/Corporate Action 要求；
- Broker/manual corroboration要求；
- source independence；
- reviewer/approver roles；
- quorum；
- key/algorithm requirements；
- revocation semantics；
- effective interval；
- synthetic/paper exclusion；
- unresolved blocker policy。

Policy 不能由单个请求携带任意字段自我选择；必须来自独立、受信任 Registry。

## 12. Authority Store

独立路径，例如：

```text
data/outcome-admission-authority.db
data/outcome-admission-records/
```

不得与 production DB、Runtime Artifact、Stage 4G Collection 或 Stage 4F Ledger 共用。

要求：

- immutable canonical decision records；
- append order/hash chain；
- atomic no-overwrite publication；
- stable authority store ID；
- signed checkpoints；
- audit snapshot；
- request/decision/revocation indexes；
- full audit before extension；
- path/inode/stable-store identity；
- backup/restore ceremony；
- corruption quarantine；
- no secret/private key storage。

## 13. PIT Admission View

Scoreboard 只能消费 Authority materialized view：

```text
admitted_outcomes(as_of, policy_id, cohort)
```

一个 Outcome 在 `as_of` 可见需同时满足：

- Stage 4F record 在 `as_of` 已摄取；
- Admission Decision 在 `as_of` 已记录且生效；
- signer/key/role 在相关时点有效；
- quorum 满足；
- 没有在 `as_of` 已生效的 revoke/supersede；
- exact cohort 与 Scoreboard policy 匹配。

后来的撤销不能改变早期历史视图，除非 revocation policy 明确规定追溯失效；追溯规则也必须在当时可审计。

## 14. Separation of Duties

最低策略：

- Requester 不能是唯一 Approver；
- Collector 不能单独批准自己采集的 live sample；
- signer key 不能同时属于自动采集服务和唯一批准者；
- policy admin 与 outcome approver 建议分离；
- emergency revocation 可单人触发，但恢复 Admission 需要正常 quorum；
- 所有权限变化 append-only 记录。

单用户本地部署若无法形成真正组织隔离，产品必须明确显示 `LOCAL_SINGLE_OPERATOR_LIMITATION`，不得声称 institutional independence。可以使用离线第二密钥或独立设备签署，但必须诚实标注实际治理水平。

## 15. 隐私与敏感数据

- 不保存券商密码、API secret、session token；
- 不保存完整账号，使用 opaque account identity；
- 原始 broker statements/附件保存在仓库外加密 evidence vault；
- Authority 只保存 hash、metadata、retention/classification 和取证位置；
- 日志和错误不得回显 secret/PII；
- export 默认脱敏；
- 删除请求不能破坏 append-only审计，应采用加密擦除/密钥销毁与 tombstone policy。

## 16. API 与 CLI

建议先 local/offline CLI：

```text
build-admission-request
verify-admission-request
review-admission-request
sign-admission-decision
append-admission-decision
revoke-admission
verify-authority-ledger
materialize-admitted-view --as-of ...
```

写操作必须：

- explicit actor；
- local authenticated session；
- nonce/replay protection；
- exact request/decision preview；
- signature validation；
- audit log；
- 不允许 browser 直接持有 private key。

## 17. 负向测试矩阵

至少覆盖：

- forged/corrupted signature；
- unknown、expired、revoked或 role-mismatched key；
- Collector 自我批准；
- quorum 不足；
- policy 不存在/未来生效/被撤销；
- evidence known_at 晚于 decision；
- retroactive backfill；
- Stage 4G case tamper；
- Stage 4F record/ledger replacement；
- runtime artifact mismatch；
- missing execution/path/calendar/rule evidence；
- Paper/Synthetic 请求 real scope；
- duplicate decision；
- conflicting Admission；
- revoke/supersede as-of semantics；
- key rotation；
- authority DB/file orphan、chain break和并发初始化；
- backup restore 到相同路径但不同 store identity；
- secret/PII leakage scan。

## 18. Scoreboard 解锁条件

Stage 4H 完成仍不自动解锁真实 Scoreboard。Stage 4I 需要独立、版本化 `StrategyScoreboardPolicy`，定义：

- exact strategy/version/market/horizon/model/evidence tier；
- minimum admitted sample count；
- recent-window weighting；
- bucket minimums；
- revocation handling；
- confidence intervals；
- insufficient-evidence UI；
- no automatic promotion/trading。

在 policy 和足够 admitted samples 前：

```text
metrics = null
state = INSUFFICIENT_REAL_EVIDENCE
investment_performance_claim = false
```

## 19. 实施顺序

### 4H-A — Authority/Policy/Principal Contracts

只做 canonical contracts、fixture keys、negative tests；不允许 real Admission。

### 4H-B — Independent Store and Signature Verification

实现 Registry、signed decision、revocation和审计。选择正式签名后端前只允许 fixture lane。

### 4H-C — Candidate Bundle Verifiers

接 Stage 4G.1、Stage 4G 与 Stage 4F exact identities；全部 fail-closed。

### 4H-D — Local Dual-Control Workflow

实现 Request/Review/Approve/Revoke，禁止 self-admission。

### 4H-E — Shadow Admission

跨多个交易日运行；只生成 shadow admitted view，不接 Scoreboard UI。

### 4H-F — Independent Review and Release Gate

完成密码学、PIT、权限、金融正确性、备份恢复和 secret/privacy review。

## 20. 最终门禁

```text
Stage 4G.1 operational artifacts accepted
signing backend pinned and reviewed
principal/policy registry tests
signature/key rotation/revocation tests
candidate bundle full reconstruction
PIT as-of and retroactive-backfill tests
separation-of-duties/quorum tests
market/execution/path/rule verification
concurrent append/audit/recovery tests
Stage 4F/4G regressions
full Runtime/Quant
production DB unchanged
no private keys/secrets in repo
exact Git Index scan
independent cryptographic and financial review
```

只有这些门禁通过后，Authority 才能从 `CONTRACT_ONLY` 进入 `SHADOW_ADMISSION`；真实产品 Scoreboard 仍需 Stage 4I 单独验收。
