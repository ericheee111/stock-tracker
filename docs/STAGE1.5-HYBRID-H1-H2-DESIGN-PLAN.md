# Stage 1.5 Hybrid H1/H2 设计与执行计划

> 状态：`IMPLEMENTED_REVIEWED`
>
> 日期：2026-08-26
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`

## 1. 目标与边界

本阶段把现有本地同源 Web/API 扩展为可安全承载“云端静态 Web + 本地私有 API”的工程合同，同时保持本地同源模式不退化。

```text
H1: Runtime Config → URL Builder → Origin-scoped session access → API/Engine/Commit handshake → runtime states
H2: exact CORS → OPTIONS → Authorization cross-origin → /api/runtime/health → SSE CORS → security regression
```

不实现 H3 开机自启/休眠/真实 Tailscale Target Lane、H4 Pages 实际部署/CSP、H5 公开 Tunnel，也不修改 Quant、策略、评分、概率、Big Trend 或交易计划语义。本地跨端口测试不得冒充真实 Tailnet 两设备验收。

## 2. H1 前端合同

### 2.1 无密钥 Runtime Config

`web/runtime-config.js` 仅包含非密钥元数据：

```javascript
window.STOCK_TRACKER_RUNTIME = Object.freeze({
  deploymentMode: "HYBRID_PRIVATE",
  apiBaseUrl: "",
  allowedApiOrigins: [],
  ssePath: "/api/stream",
  frontendBuild: "development",
  expectedApiMajor: 1,
  expectedEngineId: "stock-tracker-local",
  allowApiOriginOverride: false,
  allowPrivateBrowserCache: false,
  healthPollMs: 15000
});
```

规则：

1. 私有访问值不得进入 Runtime Config、HTML、Bundle、URL 或日志；
2. `apiBaseUrl` 为空时才回退页面同源；
3. 显式 API Origin 必须属于 `allowedApiOrigins`；
4. Origin 必须是规范化的 HTTP(S) Origin，拒绝 userinfo、非根 path、query、fragment、`null` 和首尾空白；HTTP 只允许 loopback，任何远程 Origin 必须使用 HTTPS；
5. 生产默认不提供任意 Origin Override；
6. 配置错误进入 `RUNTIME_CONFIG_ERROR`，不静默改连其他 Backend。

### 2.2 URL Builder、会话访问与握手

- REST、SSE、Health 只能通过一个 Runtime URL Builder；
- Builder 只接受 `/api/...` path，拒绝 query、fragment、空白、反斜杠及 URL 规范化后的 dot-segment 越界；
- 会话访问值只使用 `sessionStorage`，Key 绑定规范化 API Origin；
- API Origin 改变时删除旧 Origin 对应值并要求重新输入；
- 请求使用 `credentials: "omit"`、`redirect: "error"`、`referrerPolicy: "no-referrer"`，防止 Cookie 混入或私有 Header 跟随重定向；
- 首次私有数据请求前校验 API Major 与 Engine ID；
- `frontendBuild` 非 development 时校验 Backend Commit；
- Major/Engine/Build mismatch 均为硬阻断，清除当前 Origin 的会话访问值，并禁止后续私有请求；
- SSE 401/403 停止自动高速重试，更新访问值或显式 reconnect 后恢复。

## 3. H2 后端合同

### 3.1 Runtime 配置

`config/app.toml` 新增：

```toml
[runtime]
deployment_mode = "HYBRID_PRIVATE"
engine_id = "stock-tracker-local"
commit_id = "development"
api_major = 1
cors_allowed_origins = []
cors_max_age_sec = 600
```

模式名、Engine ID、Commit、API Major、Max-Age 和 Origin 数组必须严格解析。Origin 加载时规范化、去重；默认空 Allowlist 不影响同源，但所有真实跨域请求失败关闭。

### 3.2 精确 CORS

```text
Access-Control-Allow-Origin: 精确请求 Origin
Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
Access-Control-Allow-Headers: Authorization, Content-Type, Accept
Access-Control-Max-Age: 本地有界配置
Vary: Origin
```

- 不返回 wildcard；
- `Origin: null`、非法 Origin、非 allowlist Origin 失败关闭；
- 预检方法/Header 必须属于固定集合，且不绕过实际认证；
- 同源请求不依赖 Allowlist；
- CORS 拒绝在认证之前执行，避免向未知 Origin 暴露认证配置状态；
- 允许 Origin 下的 401/403/500 仍携带精确 CORS 头，让浏览器区分认证和网络错误。

### 3.3 Runtime Health

新增公开元数据端点：

```text
GET /api/runtime/health
```

至少返回：

```text
schema_version, status, engine_id, engine_version, commit_id,
deployment_mode, started_at, last_heartbeat_at, last_collection_at,
data_as_of, data_status, scheduler_state, provider_summary,
database_state, sse_available, api_major
```

Health 不包含会话访问值、数据库路径、账户、持仓、成本、私有 Watchlist 或完整 Provider URL；不访问上游；无行情时返回 `null` 与 `UNKNOWN`，不伪造 LIVE。浏览器严格校验字段类型、枚举、时间戳、Provider 计数和状态一致性；异常合同进入 `RUNTIME_HEALTH_INVALID` 硬阻断并清除当前 Origin 的会话访问值。

## 4. 前端状态机

```text
ONLINE, DEGRADED, STALE, ENGINE_OFFLINE, NETWORK_OFFLINE,
AUTH_REQUIRED, AUTH_FAILED, CORS_BLOCKED, API_VERSION_MISMATCH,
ENGINE_ID_MISMATCH, BUILD_MISMATCH, TUNNEL_UNAVAILABLE,
RUNTIME_CONFIG_ERROR, RUNTIME_HEALTH_INVALID
```

判定优先级为：配置错误 / Health 合同无效 → 浏览器离线 → API/Engine/Build mismatch → Health 不可达 → CORS/Tunnel → Stale → Auth → Degraded/Online。

Engine/CORS/Network/Tunnel/Version/Engine ID 硬故障时清除页面内存中的私有 Brief、Portfolio、Positions、Watchlist、Radar 与 Config，不继续把旧 `EXECUTABLE` 展示为当前动作；恢复后重新握手和拉取。页面显示 API Host、Engine ID、API Major、Backend Commit、Frontend Build 和数据时间。SSE 在线不等同于 Engine 在线，Health 为主判据。

## 5. 实施文件

计划新增：

```text
web/runtime-config.js
web/js/runtime.js
stock_tracker/api/runtime.py
tests/test_hybrid_h1_h2.py
qa/ui/hybrid_runtime_qa.cjs
qa/ui/hybrid_runtime_config_error_qa.cjs
qa/ui/hybrid_runtime_invalid_health_qa.cjs
qa/ui/hybrid_runtime_build_mismatch_qa.cjs
qa/ui/hybrid_runtime_stale_qa.cjs
scripts/run_hybrid_h1_h2_integration.py
docs/STAGE1.5-HYBRID-H1-H2-IMPLEMENTATION-HANDOFF.md
docs/STAGE1.5-HYBRID-H1-H2-INDEPENDENT-REVIEW.md
```

并修改配置、Server、Handlers、启动装配、前端 API/SSE/App/样式，以及 PRD、Architecture、Gap Matrix、Handoff、AGENTS、Overview 和 `CHATGPT_HANDOFF.md`。

## 6. 验收矩阵

后端必须覆盖：严格 Runtime 配置、Health 必需字段且无私有字段、同源兼容、allowlist 跨域 GET/CRUD/SSE、允许 Origin 下可读 401、精确 Bearer 后 CRUD、非 allowlist/`null`/非法 Origin 拒绝、OPTIONS 固定方法/Header、无 wildcard CORS。

前端必须覆盖：统一 URL Builder、无硬编码 fetch 旁路、Origin-scoped session key、Origin 变化清除、redirect 拒绝、Major/Engine mismatch 阻止私有请求、Auth/CORS/Engine/Network/Tunnel/Build/Stale 可见区分、390/768/1280 无横向溢出、Today 与 Portfolio 合同不回归。

发布门禁：focused H1/H2、全量 runtime、全量 Quant、Mock Today、真实同源 Today/Portfolio、跨域浏览器集成、compileall、ruff、pip check、Quant smoke、synthetic benchmark、migration dry-run、diff check、独立对抗式 Review 和 staged-tree 验证。

## 7. Git 边界

不提交 `data/`、数据库、日志、Build、ZIP、QA 生成截图、私有访问值或 tracked Python cache 的本地变动。全部门禁和 Review 通过后才提交；推送后验证 local `HEAD`、`origin/main` 与远端 `refs/heads/main` SHA 一致。

## 8. 实施结果

H1/H2 已按本计划完成工程实现和本地跨 Origin 浏览器验收。当前验证覆盖 exact CORS/OPTIONS、Bearer CRUD、fetch-stream SSE、Origin-scoped session access、API Major/Engine/Build hard block、非法 Runtime Health hard block、动态 STALE、无 Token 不启动 SSE，以及 390/1280 无横向溢出。真实 Tailscale Serve、两台独立 Tailnet 节点、H3 运行恢复和 H4 Pages 部署仍为独立 operational 门禁。
