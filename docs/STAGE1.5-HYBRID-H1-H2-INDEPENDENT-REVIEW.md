# Stage 1.5 Hybrid H1/H2 独立工程 Review

> Review 状态：`PASSED_WITH_OPERATIONAL_GATES_PENDING`
>
> 日期：2026-08-26
>
> Review 结论：`ENGINEERING_READY_FOR_MERGE`

## 1. Review 范围

本 Review 独立检查 H1/H2 的：

- Runtime Config 与 URL Builder；
- Browser Token 存储、Origin 隔离和重定向边界；
- API Major / Engine ID / Commit / Health 合同；
- CORS Allowlist、OPTIONS、Authorization 和 SSE；
- Runtime Health 的数据真实性、时区和隐私边界；
- Hard failure / STALE 的旧决策清理；
- Today、Portfolio、Quant 与生产数据库回归；
- Git 提交边界和本地生成物隔离。

不把本地双 Origin 测试视为真实 Tailscale 或 Cloudflare Pages operational 验收。

## 2. 对抗式 Findings 与处理

| 级别 | Finding | 处理结果 |
|---|---|---|
| CRITICAL | 远程 API Origin 曾允许明文 HTTP，可能把 Bearer 发往非 TLS Origin | 已修复：HTTP 仅允许 loopback，所有远程 Origin 必须 HTTPS；Python/Browser 双侧失败关闭 |
| CRITICAL | `secureFetchOptions(extra)` 曾允许调用者覆盖 `credentials`、`redirect`、`referrerPolicy` | 已修复：安全字段在 merge 后强制覆盖；真实浏览器负向测试验证不可降级 |
| CRITICAL | Build/Commit mismatch 初版仅告警，仍可保留握手和私有值 | 已修复：`BUILD_MISMATCH` 进入 hard failure、清 Token、阻止后续 API/SSE |
| CRITICAL | Runtime Health 初版字段校验不足，畸形 Provider 数量或状态可能继续握手 | 已修复：字段、枚举、时间、Provider 精确计数、状态一致性和非 UNKNOWN 时间均严格校验；invalid Health 清 Token |
| CRITICAL | Quote 对象残留 `data_status=LIVE` 时，时间推进后 Health 可能继续显示 LIVE | 已修复：每次 Health 按当前时钟重新计算 freshness；2 小时旧 LIVE fixture 返回 STALE |
| CRITICAL | naive A/HK/US 行情时间曾被按 UTC 解释，可能把市场本地旧行情误判为新鲜 | 已修复：source timestamp 按目标市场时区归一化，collection timestamp 按进程本地时区归一化；回归校验 UTC `data_as_of` |
| IMPORTANT | 旧全局 `sessionStorage` Token key 与 orphan scoped value 可能残留 | 已修复：启动删除 legacy key；Origin marker 改变时删除旧、新 scoped value |
| IMPORTANT | API/Engine/Invalid Health mismatch 清理不完整 | 已修复：均清理当前 Origin Token，清空当前私有决策内存，不发送后续 Authorization |
| IMPORTANT | Runtime Config 允许未知字段，容易把秘密或未审查开关误放入公开 JS | 已修复：Browser/Python 配置均使用 exact field allowlist；`privateAccess` 等未知字段进入 `RUNTIME_CONFIG_ERROR` |
| IMPORTANT | Runtime Health 与 CORS reachability probe 没有有界超时 | 已修复：Health 5 秒、probe 2.5 秒；不会无限挂起初始化或健康轮询 |
| IMPORTANT | SSE 401/403 可能高速重试，且在私有数据认证失败前启动 | 已修复：auth block 停止自动重试；首次私有数据成功后才建立 SSE |
| IMPORTANT | malformed Host 可能被误当 same-origin | 已修复：Host identity 严格解析；畸形 Host 测试确认不能绕过 CORS/认证边界 |
| IMPORTANT | Provider health 缺失、HALF_OPEN、未知状态可能未降级 | 已修复：count=0、HALF_OPEN、OPEN、未知状态均保守降级；Provider summary 保持内部一致 |
| NORMAL | `web/index.html` 曾重复加载 Runtime Config/Runtime 模块 | 已修复：只加载一次，避免双初始化和 session scope 副作用 |
| NORMAL | App 内曾存在重复 Runtime status/render 实现 | 已收敛为单一 Runtime snapshot 驱动路径 |

## 3. 安全结论

### 3.1 Token

通过：

- Token 只来自当前会话 `sessionStorage`；
- Key 绑定 normalized API Origin；
- Origin 改变、Build/API/Engine/Health 不兼容会清理；
- 不进入 Runtime Config、URL、DOM、日志或验收 JSON；
- Browser 请求不携带 Cookie，拒绝重定向，不发送 Referrer；
- 不允许调用者覆盖安全 Fetch options。

### 3.2 CORS

通过：

- exact Origin；
- wildcard、`null`、userinfo、path/query/fragment、远程 HTTP 均拒绝；
- CORS 在 auth 之前检查；
- OPTIONS 只允许冻结方法/Header；
- 预检不绕过实际 Bearer；
- SSE、成功响应和允许 Origin 下的错误响应包含 exact CORS；
- 同源 loopback 兼容；
- malformed Host 不构成 same-origin。

### 3.3 Runtime Health

通过：

- public metadata-only；
- 不访问 Provider 网络；
- 不泄露数据库路径、Token、Portfolio、成本、Watchlist、URL 或密钥；
- 数据状态按当前时钟和市场时区重新计算；
- Provider/Scheduler/Database 异常保守降级；
- Browser 对畸形合同失败关闭。

## 4. 产品与金融正确性

本切片没有修改：

- ActionState 映射；
- PositionSizer；
- TradePlan；
- Exit；
- Signal scoring / Risk gate；
- 概率、Big Trend、Strategy Scoreboard 或模型晋级。

STALE 和 hard failure 只收紧展示/请求边界：旧 `EXECUTABLE` 不再留在页面内存中，Portfolio 手工事实与新开仓决策仍保持不同语义。

Quant 回归通过，但所有 smoke/benchmark 仍明确为 synthetic fixture，不构成真实投资表现。Challenger 未晋级，原因仍为：

```text
ECE_REGRESSED
TIME_INSTABILITY
```

## 5. 验证结果

| 门禁 | 结果 |
|---|---:|
| H1/H2 Python 专项 | 14/14 |
| Browser 主跨域场景 | 28/28 |
| Config invalid | 3/3 |
| Runtime Health invalid | 3/3 |
| Build mismatch | 3/3 |
| STALE | 2/2 |
| Runtime unittest | 394 通过，1 跳过 |
| Quant | 560 通过，244 subtests |
| Mock Today | 17/17 |
| 真实 Today | 17/17 |
| Portfolio CRUD | 13/13 |
| `compileall` | 通过 |
| H1/H2 新增/相关 Python Ruff | 通过 |
| `pip check` | 通过 |
| Quant contract smoke | 通过，synthetic only |
| Synthetic benchmark | 通过，未晋级 |
| Production migration dry-run | 未修改数据库，4 pending |

生产数据库 SHA-256 前后一致：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

## 6. 已知非阻断项

1. 全仓库 `ruff check .` 仍包含本切片之前存在的旧模块 lint 债务，特别是 `handlers.py` 和历史 collector/serializer 文件；本 Review 只确认 H1/H2 新增/相关文件通过 targeted Ruff，不能声称全仓库 Ruff 通过。
2. `ruff format --check` 未作为通过证据。
3. Windows 命令行捕获 Node 中文输出时可能显示编码替换字符；Browser DOM、状态码和断言均通过，不影响运行合同。
4. 历史 tracked `.pyc` 仍会造成工作树 dirty；必须从本次 staged 清单排除。
5. CSP、静态站点发布签名、真实 Pages Origin、Tailscale Target、开机自启、休眠与崩溃恢复属于 H3/H4。

## 7. Operational Gates

以下仍为 `PENDING`，不得冒充已完成：

```text
REAL_TAILSCALE_SERVE = PENDING
TWO_DISTINCT_TAILNET_NODES = PENDING
CLOUDFLARE_PAGES_DEPLOYMENT = PENDING
GITHUB_PAGES_FALLBACK = PENDING
SERVICE_AUTOSTART_AND_SLEEP_RECOVERY = PENDING
```

## 8. 最终判定

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
SECURITY_REVIEW = PASSED
LOCAL_CROSS_ORIGIN_ACCEPTANCE = PASSED
REGRESSION_GATES = PASSED
ENGINEERING_READY_FOR_MERGE = TRUE
OPERATIONAL_REMOTE_DEPLOYMENT = PENDING
```

H1/H2 可以进入定向 Git commit 和 `main` 推送；下一代码切片为 H3/H4。
