# Stage 1.5 Hybrid H0 实施交接

> 日期：2026-08-24
>
> 状态：`ENGINEERING_READY_FOR_MERGE`；`OPERATIONAL_DEVICE_ACCEPTANCE_PENDING`
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`
>
> 设计：`docs/STAGE1.5-HYBRID-H0-DESIGN-PLAN.md`

## 1. 交付结论

Hybrid H0 已完成工程实现和本地远程式验收：

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
LOCAL_REMOTE_STYLE_ACCEPTANCE = PASSED
REAL_TAILSCALE_SERVE = PENDING
TWO_DISTINCT_TAILNET_NODES = PENDING
```

当前执行宿主没有 Tailscale CLI，因此没有、也不得声称已经完成真实 Serve 或两台物理/Tailnet 设备验收。仓库已提供可在目标设备上执行的安全 `server/client` 工具。

## 2. 实际实现

### 2.1 Loopback 默认与公网显式确认

- `ServerConfig` 和 `config/app.toml` 默认绑定 `127.0.0.1`；
- `scripts/start.py` 始终显式传入 `--host 127.0.0.1`；
- `stock_tracker.__main__.build_context` 在构造 Repository、Scheduler 和 HTTP Server 前拒绝未确认的非 loopback；
- `APIServer` 自身再次执行同一安全检查，避免直接构造绕过 CLI；
- 非 loopback 必须同时使用 `--host <host>` 和 `--allow-non-loopback`；
- Docker/Procfile 仅以该双重确认进入 `PURE_CLOUD_EXPERIMENTAL`；
- `.dockerignore` 排除 `/data/`、`/build/`、归档、数据库、日志、缓存、截图和 Agent 工作文件。

### 2.2 私有访问合同复用

新增 `stock_tracker/core/security.py`，由 HTTP Server 与 H0 运维工具共同使用：

```text
STOCK_TRACKER_PRIVATE_ACCESS
minimum length = 32 visible characters
no surrounding/control whitespace
source = process environment only
```

访问值不接受命令行参数，不进入 Runtime Config、Git、URL、日志或验收 JSON。

### 2.3 Tailscale Serve Adapter

新增：

- `stock_tracker/deployment/hybrid_h0.py`；
- `scripts/hybrid_h0.py`；
- `scripts/hybrid_h0.bat`。

操作：

```bash
python scripts/hybrid_h0.py preflight
python scripts/hybrid_h0.py enable
python scripts/hybrid_h0.py status
python scripts/hybrid_h0.py disable
```

安全边界：

- target 只能是 `http://127.0.0.1:<port>`；
- 启用命令固定为 `tailscale serve --bg http://127.0.0.1:<port>`；
- 不提供 Funnel 或任意 URL 参数；
- preflight 验证 Tailscale Running、DNS 名、稳定节点 ID、Engine 可达性、无 Token 拒绝和正确 Token 放行；
- 启用前结构化读取 `tailscale serve status --json`；只有单一 HTTPS listener、单一 `/` Proxy、Funnel 关闭、无 Services/Foreground/额外挂载或未知非空 section 时才认定为 H0 独占配置；
- 相同且精确匹配的 H0 target 可幂等启用；
- disable 只在上述完整 ownership 合同匹配时执行 `tailscale serve off`；
- 永不执行可能清除其他服务的 `tailscale serve reset`。

### 2.4 临时数据库验收 Harness

新增：

- `stock_tracker/deployment/h0_acceptance.py`；
- `scripts/run_hybrid_h0_acceptance.py`。

本地只读生产保护验收：

```bash
python scripts/run_hybrid_h0_acceptance.py local
```

它创建临时目录、复制静态 Web、创建临时 SQLite，并验证：

1. 一次性 fixture marker；
2. 同源静态页面；
3. `/api/provider_health`；
4. 未携带 Bearer 的私有 API 返回 `401 PRIVATE_API_AUTH_REQUIRED`；
5. 正确 Bearer 返回 Portfolio schema；
6. fetch-stream SSE 返回 `: connected`；
7. Portfolio Profile PUT；
8. Position POST；
9. Position PATCH；
10. Position DELETE；
11. 最终 Position 列表为空；
12. 生产数据库前后 SHA-256 相等。

任何 Portfolio 写入前必须同时满足：

```text
marker schema exact match
fixture_id exact match
fixture_only is True
allow_portfolio_writes is True
production_database is False
```

### 2.5 真实两设备验收入口

服务端（必须已安装、登录 Tailscale，并在当前进程环境设置强 Bearer）：

```bash
python scripts/run_hybrid_h0_acceptance.py server --enable-serve
```

第二台独立 Tailnet 设备：

```bash
python scripts/run_hybrid_h0_acceptance.py client \
  --base-url https://<server>.ts.net \
  --fixture-id <server-output-fixture-id>
```

两设备证明不再只比较 hostname。Server marker 与 Client 都从各自 `tailscale status --json` 取得稳定节点 ID；节点 ID 缺失或相同即在任何 CRUD 写入前失败关闭。

## 3. 新增/修改代码

### 新增

```text
.dockerignore
stock_tracker/core/network.py
stock_tracker/core/security.py
stock_tracker/deployment/__init__.py
stock_tracker/deployment/hybrid_h0.py
stock_tracker/deployment/h0_acceptance.py
scripts/hybrid_h0.py
scripts/hybrid_h0.bat
scripts/run_hybrid_h0_acceptance.py
tests/test_hybrid_h0.py
```

### 修改

```text
config/app.toml
stock_tracker/core/config.py
stock_tracker/cli.py
stock_tracker/__main__.py
stock_tracker/api/server.py
scripts/start.py
Dockerfile
Procfile
render.yaml
```

对应 PRD、Architecture、Gap Matrix、Overview、Handoff 与 H0 文档也已同步。

## 4. 当前针对性验证

已运行：

```text
python -m compileall -q stock_tracker scripts
python -m unittest tests.test_hybrid_h0 -v
python -m unittest tests.test_config_contract tests.test_private_api_access tests.test_private_api_http tests.test_server_methods -v
python scripts/run_hybrid_h0_acceptance.py local
python -m ruff check <H0 changed/new Python files>
```

结果：

```text
H0 focused tests = 16 passed
related existing tests = 22 passed
local remote-style acceptance = passed
ruff check = passed
compileall focused = passed
production DB hash equal = true
```

生产数据库 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

`ruff format --check` 曾被 CodexPro 的高风险命令守卫拦截，未执行，不能写成通过；`ruff check` 已实际通过。完整发布门禁和独立对抗式 Review 已完成，详见 `docs/STAGE1.5-HYBRID-H0-INDEPENDENT-REVIEW.md`：Runtime 380 通过、1 跳过；Quant 560 通过；Mock Today 17/17；真实 API/Web Today 17/17；Portfolio CRUD 13/13；migration dry-run、pip check、Quant smoke、synthetic benchmark 与 `git diff --check` 均通过。

## 5. 已知限制

- 当前宿主没有 Tailscale CLI，无法运行真实 `preflight/enable/status/disable`；
- 当前没有第二台可由本会话控制的 Tailnet 设备；
- H0 保持前端与 API 同源，不实现 Runtime Config、CORS、`OPTIONS` 或 `/api/runtime/health`；
- H0 不实现 Windows Task Scheduler/Service、休眠防护和崩溃恢复，属于 H3；
- H0 不实现 Cloudflare Pages/GitHub Pages，属于 H4；
- 未获取真实行情，也未改变 Quant、信号、模型、概率或交易语义。

## 6. Git 交付与接续顺序

- H0 实现提交：`cf5b5f8eeae93c4147d6b607b30c3c569247a2b1`（`feat: implement hybrid H0 private bootstrap`）；
- 已推送 `origin/main`，并验证 local `HEAD`、`origin/main`、远端 `refs/heads/main` 三者 SHA 完全一致；
- 并行 UI/build/ZIP/cache/data/screenshot 工作未进入该提交。

接续顺序：

1. 在目标宿主和第二台 Tailnet 设备补齐 operational 验收；
2. 进入 Hybrid H1/H2。
