# Stage 1.5 Hybrid H1/H2 实施交接

> 状态：`IMPLEMENTED_VERIFIED`
>
> 日期：2026-08-26
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`
>
> 设计：`docs/STAGE1.5-HYBRID-H1-H2-DESIGN-PLAN.md`

## 1. 交付结论

Hybrid H1/H2 已把现有本地同源应用扩展为“静态 Web 与本地私有 Engine 可分离部署”的完整工程边界，同时保留 `LOCAL_ONLY` / H0 同源恢复路径。

本切片已经实现并验证：

- 无密钥 Runtime Config；
- REST、SSE、Runtime Health 的统一 URL Builder；
- 只存于 `sessionStorage` 且绑定规范化 API Origin 的私有访问值；
- API Major、Engine ID、Frontend Build / Backend Commit 握手；
- exact CORS Allowlist 与严格 `OPTIONS`；
- metadata-only `GET /api/runtime/health`；
- Header-authenticated cross-origin fetch-stream SSE；
- Engine、Tunnel、CORS、Auth、Version、Build、Invalid Health、STALE 的显式状态；
- 硬故障清理当前决策内存，STALE 阻止当前执行型决策；
- 本地双 Origin、临时 SQLite、真实浏览器 CRUD/SSE 验收。

这不代表 Cloudflare Pages、GitHub Pages、真实 Tailscale Serve Target Lane 或两台 Tailnet 设备已经 operational 通过。

## 2. H1 前端实现

### 2.1 Runtime Config

新增：

```text
web/runtime-config.js
web/runtime-config.example.js
web/js/runtime.js
web/css/runtime.css
```

正式字段为：

```text
deploymentMode
apiBaseUrl
allowedApiOrigins
ssePath
frontendBuild
expectedApiMajor
expectedEngineId
allowApiOriginOverride
allowPrivateBrowserCache
healthPollMs
```

安全规则：

- 未知字段失败关闭，避免秘密或未审查开关被误放入公开配置；
- `apiBaseUrl` 为空时才回退页面同源；
- 显式 API Origin 必须存在于 `allowedApiOrigins`；
- HTTP 只允许 `localhost`、`127.0.0.0/8`、`::1`，远程 Origin 必须 HTTPS；
- Origin 拒绝 userinfo、非根 path、query、fragment、反斜杠、`null`、控制字符；
- URL Builder 只接受规范化 `/api/...` path，并拒绝 query、fragment、空白和 dot-segment escape；
- `allowApiOriginOverride=true` 与 `allowPrivateBrowserCache=true` 在当前正式合同中均失败关闭；
- Runtime Config、HTML、URL 和日志均不保存 Bearer、Portfolio 或 Provider 密钥。

### 2.2 私有访问值

私有访问值只保存在：

```text
stockTrackerPrivateAccess::<encodeURIComponent(normalized-api-origin)>
```

另有当前 Origin marker：

```text
stockTrackerPrivateAccessOrigin
```

行为：

- 旧全局 key `stockTrackerPrivateAccess` 启动时删除；
- API Origin 改变时删除旧 Origin 和新 Origin 的遗留 scoped value；
- Build/API/Engine/Invalid Health 不兼容时清除当前 scoped value；
- Token 不进入 `localStorage`、Runtime Config、URL、DOM 或验收输出；
- Token 要求 32—4096 个可见字符，无首尾空白或控制字符。

### 2.3 请求和状态机

`web/js/api.js` 与 `web/js/sse.js` 统一使用 Runtime：

```text
Runtime.apiUrl(path)
Runtime.sseUrl()
Runtime.secureFetchOptions(...)
Runtime.privateHeaders()
```

安全选项由 Runtime 最终覆盖，调用者不能降级：

```text
mode = cors
credentials = omit
redirect = error
referrerPolicy = no-referrer
cache = no-store
```

Runtime Health 请求有 5 秒超时；CORS 可达性探针有 2.5 秒超时。SSE 的 401/403 进入 auth block，不高速自动重试；重新提供会话访问值或显式 reconnect 后才恢复。

当前状态枚举：

```text
ONLINE
DEGRADED
STALE
ENGINE_OFFLINE
NETWORK_OFFLINE
AUTH_REQUIRED
AUTH_FAILED
CORS_BLOCKED
API_VERSION_MISMATCH
ENGINE_ID_MISMATCH
BUILD_MISMATCH
RUNTIME_HEALTH_INVALID
TUNNEL_UNAVAILABLE
RUNTIME_CONFIG_ERROR
```

`BUILD_MISMATCH`、`RUNTIME_HEALTH_INVALID`、API Major 与 Engine ID mismatch 均为 hard failure，不能继续读取私有 API。

## 3. H2 后端实现

### 3.1 Runtime 配置

`config/app.toml` 新增 `[runtime]`，由 `stock_tracker/core/config.py` 严格解析：

```toml
[runtime]
deployment_mode = "HYBRID_PRIVATE"
engine_id = "stock-tracker-local"
commit_id = "development"
api_major = 1
cors_allowed_origins = []
cors_max_age_sec = 600
```

配置拒绝：

- 未知字段；
- 非数组或超过 32 项的 Origin；
- wildcard、`null`、非法 URL；
- 远程 HTTP Origin；
- 非冻结 deployment mode；
- 非法 API Major、Max-Age、Engine ID 或 Commit ID。

### 3.2 exact CORS / OPTIONS

`stock_tracker/api/server.py` 在认证和业务路由之前校验 Origin：

- exact normalized Origin；
- 同源请求不依赖 Allowlist；
- 非 allowlist / `null` / malformed Origin 返回 403；
- malformed Host 不能伪造 same-origin；
- 不返回 `Access-Control-Allow-Origin: *`；
- 允许的方法固定为 `GET, POST, PUT, PATCH, DELETE, OPTIONS`；
- 允许的请求 Header 固定为 `Authorization, Content-Type, Accept`；
- `OPTIONS` 不进入业务 Handler、不写数据库、不访问 Provider；
- 允许 Origin 下的正常、认证失败、业务失败和 SSE 响应均携带 exact CORS Header；
- 预检成功不等于实际私有 API 已通过认证。

### 3.3 Runtime Health

新增：

```text
stock_tracker/api/runtime.py
GET /api/runtime/health
```

Health 只读取进程、Store、Repository 路径存在性、Scheduler 和 Provider circuit metadata，不调用上游 Provider。

返回：

```text
schema_version
status
engine_id
engine_version
commit_id
deployment_mode
started_at
last_heartbeat_at
last_collection_at
data_as_of
data_status
scheduler_state
provider_summary
database_state
sse_available
api_major
```

不会返回：

- 私有访问值或认证配置；
- 数据库绝对路径；
- Portfolio、持仓、成本、Watchlist；
- Provider URL、密钥或环境变量。

正确性加固：

- 无行情为 `DEGRADED / UNKNOWN`，不伪造 LIVE；
- Provider health 缺失、HALF_OPEN、OPEN 或未知状态均降级；
- Scheduler 未运行或数据库未就绪均降级；
- 旧行情即使对象中残留 `data_status=LIVE`，也按当前时钟重新计算；
- naive 行情时间按目标市场时区归一化，`received_at` 按进程本地时区归一化，避免把 A 股本地旧时间误当 UTC 新行情；
- STALE 数据返回 `status=STALE`，浏览器保留身份握手但禁止当前执行型决策。

## 4. 主要文件

```text
config/app.toml
stock_tracker/__main__.py
stock_tracker/api/handlers.py
stock_tracker/api/runtime.py
stock_tracker/api/server.py
stock_tracker/core/config.py
stock_tracker/core/network.py

web/index.html
web/runtime-config.js
web/runtime-config.example.js
web/css/runtime.css
web/js/runtime.js
web/js/api.js
web/js/sse.js
web/js/app.js

scripts/run_hybrid_h1_h2_integration.py
tests/test_hybrid_h1_h2.py
tests/test_private_api_http.py
qa/ui/hybrid_runtime_qa.cjs
qa/ui/hybrid_runtime_config_error_qa.cjs
qa/ui/hybrid_runtime_invalid_health_qa.cjs
qa/ui/hybrid_runtime_build_mismatch_qa.cjs
qa/ui/hybrid_runtime_stale_qa.cjs
```

Today QA fixture 也补充 Runtime Health mock，以保持 H1/H2 握手后的原产品合同回归。

## 5. 验证证据

最新验证：

| 门禁 | 结果 |
|---|---:|
| H1/H2 Python 专项 | 14/14 通过 |
| H1/H2 浏览器主场景 | 28/28 通过 |
| Config / Invalid Health / Build / STALE 负向浏览器场景 | 11/11 通过 |
| 运行产品全量 unittest | 394 通过，1 跳过 |
| Quant | 560 通过，244 subtests 通过 |
| Mock Today | 17/17 通过 |
| 真实 API/Web Today | 17/17 通过 |
| Portfolio CRUD | 13/13 通过 |
| 双 Origin 390px / 1280px | 无横向溢出 |
| Python compileall | 通过 |
| H1/H2 新增/相关 Python 文件 Ruff | 通过 |
| `pip check` | 无损坏依赖 |
| Quant contract smoke | `passed=true`，synthetic only |
| Synthetic benchmark | 通过；Challenger 未晋级 |
| Production migration dry-run | `database_modified=false`，4 pending |

生产数据库验证前后 SHA-256 相同：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

全仓库 `ruff check .` 仍会命中 H1/H2 之前已经存在的旧模块 lint 债务；本切片没有把该结果描述为通过，也没有为清零历史债务而扩大修改范围。`ruff format --check` 未列为通过项。

Quant smoke 与 benchmark 均是 synthetic fixture，不构成真实投资表现。Challenger 继续因 `ECE_REGRESSED`、`TIME_INSTABILITY` 被阻止晋级。

## 6. 部署使用

### 本地同源

默认 `web/runtime-config.js`：

```javascript
apiBaseUrl: ''
allowedApiOrigins: []
frontendBuild: 'development'
```

继续由 Python Backend 同源托管 Web 与 API。

### H4 静态站点准备

实际 Pages 部署时：

1. 复制 `web/runtime-config.example.js` 为发布产物 `runtime-config.js`；
2. 设置 exact HTTPS Tailscale Serve API Origin；
3. 将同一静态站点 HTTPS Origin 写入 Backend `cors_allowed_origins`；
4. 把 `frontendBuild` 与 Backend `commit_id` 设为同一受控发布标识；
5. 不在配置中加入 Token 或账户事实；
6. 先通过 H3/H4 真实网络、重启、休眠、断线与两设备验收，再声明 operational 上线。

## 7. 未完成项

```text
H0 real Tailscale Serve + two distinct Tailnet devices = PENDING
H3 Target Lane / service startup / sleep / crash recovery = NOT_IMPLEMENTED
H4 Cloudflare Pages / GitHub Pages actual deployment = NOT_IMPLEMENTED
H5 public access = NOT_IMPLEMENTED
```

下一代码切片是 H3/H4。本切片没有修改 Quant、概率、Big Trend、策略战绩、仓位或交易计划语义。
