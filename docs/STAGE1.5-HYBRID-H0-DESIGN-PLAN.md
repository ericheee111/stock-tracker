# Stage 1.5 Hybrid H0 设计与实施计划

> 日期：2026-08-24
>
> 状态：Design Freeze
>
> 目标阶段：Hybrid H0 — 私有同源 Bootstrap
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`

## 1. 目标

H0 在不引入跨域、云端业务存储或 Quant 逻辑改动的前提下，将当前本地同源应用升级为可通过 Tailnet 私有访问的 Bootstrap Lane：

```text
Tailnet 浏览器
    |
    | HTTPS（Tailscale Serve）
    v
Tailscale daemon
    |
    | http://127.0.0.1:8080
    v
Stock Tracker Local Engine
    +-- 静态 Web
    +-- REST
    +-- fetch-stream SSE
    +-- Portfolio CRUD
```

完成 H0 不代表 Cloudflare Pages/GitHub Pages 静态前端、跨域 CORS、Runtime Config 或 `/api/runtime/health` 已完成；这些属于 H1/H2/H4。

## 2. 当前基线

当前仓库已经具备：

- Python Backend 同时托管 `web/` 和 `/api/...`；
- 私有 API 的 loopback 免认证与强 Bearer Token 失败关闭边界；
- fetch-stream SSE，可携带 Authorization Header；
- Portfolio Profile/Position REST CRUD；
- 使用临时 SQLite 的真实 API/Web 集成测试。

H0 阻断项：

1. `config/app.toml`、`ServerConfig` 和缺省加载仍绑定 `0.0.0.0`；
2. 本地 `scripts/start.py` 没有强制 loopback；
3. Render/Docker 等纯云实验未与本地安全默认明确分离；
4. 没有受控的 Tailscale Serve preflight/enable/status/disable 工具；
5. 没有不会修改生产数据库的远程 REST/SSE/Portfolio CRUD 验收夹具；
6. 当前执行主机未安装 Tailscale CLI，因此本会话不能伪造真实 Tailnet 或两台物理设备证据。

## 3. 安全决策

### 3.1 本地默认失败关闭

- `ServerConfig.host`、缺省配置和 `config/app.toml` 统一改为 `127.0.0.1`；
- 本地一键启动脚本显式传入 `--host 127.0.0.1`，即使配置被误改也不自动监听 LAN/公网；
- 非 loopback 绑定必须同时显式传入目标 Host 和危险操作确认参数；
- Docker/Procfile/Render 只作为 `PURE_CLOUD_EXPERIMENTAL`，必须显式声明 `0.0.0.0` 与确认参数。

### 3.2 Serve 目标固定

H0 只允许：

```text
http://127.0.0.1:<port>
```

不得将 Serve 指向：

- `0.0.0.0`；
- LAN IP；
- `free-stockdb` 端口；
- SQLite、文件目录或任意用户提供 URL；
- Tailscale Funnel。

Tailscale 当前 CLI 的受支持反向代理形式为 `tailscale serve --bg http://127.0.0.1:<port>`；`--bg` 使配置在 daemon/设备重启后恢复。H0 工具不得执行 `serve reset`，避免清除不属于本项目的配置。

### 3.3 私有认证

- 远程访问继续要求 `STOCK_TRACKER_PRIVATE_ACCESS`；
- 访问值只能从进程环境读取，不接受命令行参数，不写入配置、Git、URL、日志或验收 JSON；
- H0 preflight 必须模拟反向代理请求，证明：
  - 无 Authorization 时为 `401 PRIVATE_API_AUTH_REQUIRED`；
  - 正确 Bearer 时私有 API 返回 200；
  - 未配置或弱访问值时拒绝启用 Serve；
- H0 不信任 Forwarded/Tailscale 身份 Header 来绕过应用层 Bearer。

## 4. 实施切片

### H0-A：Loopback 与非 loopback 显式确认

修改：

- `config/app.toml`；
- `stock_tracker/core/config.py`；
- `stock_tracker/cli.py`；
- `stock_tracker/__main__.py`；
- `scripts/start.py`；
- `Dockerfile`、`Procfile`、`render.yaml`；
- 对应单元测试和文档。

合同：

```text
local default -> 127.0.0.1
non-loopback -> --host <host> + --allow-non-loopback
cloud experiment -> explicit 0.0.0.0 + explicit acknowledgement
```

### H0-B：Tailscale Serve 运维工具

新增标准库实现：

- `stock_tracker/deployment/hybrid_h0.py`；
- `stock_tracker/deployment/__init__.py`；
- `scripts/hybrid_h0.py`；
- `scripts/hybrid_h0.bat`。

命令：

```text
preflight  检查 token、loopback Engine、远程式认证和 Tailscale 状态
enable     仅在 preflight 全通过后执行 Serve --bg
status     输出 Tailscale/Serve 状态，不输出 token
disable    关闭本项目根 Serve，不执行全局 reset
```

### H0-C：无生产数据写入的验收工具

新增：

- `scripts/run_hybrid_h0_acceptance.py`；
- 自动化测试。

提供三种路径：

1. `local`：临时 SQLite + 反向代理 Host 模拟，自动验证静态页、REST、SSE、未认证拒绝、认证通过和 Position POST/PATCH/DELETE；
2. `server`：在服务端启动临时验收 Engine，可选启用真实 Tailscale Serve；
3. `client`：从第二台 Tailnet 设备访问 HTTPS Serve URL，校验服务端验收标识、设备身份不同、REST、SSE 和 Portfolio CRUD。

写操作只允许在带一次性 fixture marker 的临时验收 Engine 上执行。客户端在 marker、fixture ID 或不同设备检查失败时禁止 POST/PATCH/DELETE。

### H0-D：文档、Review 与发布

- 更新 PRD、Gap Matrix、Overview、Handoff 和主部署规格的精确状态；
- 新增独立 Review 报告；
- 运行完整 runtime/quant/QA 门禁；
- 验证生产数据库 SHA-256 不变；
- 只提交 H0 与上一轮尚未提交的部署主规格文件；
- 排除并行 UI、`build/`、ZIP、缓存、数据库、日志和截图；
- 推送 `origin/main` 并验证远端 SHA。

## 5. 验收矩阵

| 验收项 | 自动化本机 | 真实 Serve 单机 | 两台 Tailnet 设备 |
|---|---:|---:|---:|
| 默认绑定 `127.0.0.1` | 必须 | 必须 | 必须 |
| 非 loopback 无确认失败 | 必须 | 必须 | 必须 |
| Serve target 固定 loopback | 必须 | 必须 | 必须 |
| 私有 API 无 Token 拒绝 | 必须 | 必须 | 必须 |
| 私有 API 正确 Token 通过 | 必须 | 必须 | 必须 |
| 静态页面可访问 | 必须 | 必须 | 必须 |
| REST | 必须 | 必须 | 必须 |
| fetch-stream SSE | 必须 | 必须 | 必须 |
| 临时 Portfolio CRUD | 必须 | 必须 | 必须 |
| 服务端/客户端设备不同 | 不适用 | 不适用 | 必须 |
| 生产 SQLite 不变 | 必须 | 必须 | 必须 |

## 6. Review 判定

Review 输出两个独立结论：

```text
ENGINEERING_VERDICT
OPERATIONAL_DEVICE_ACCEPTANCE
```

允许在以下状态推送工程实现：

```text
ENGINEERING_READY_FOR_MERGE
OPERATIONAL_DEVICE_ACCEPTANCE_PENDING
```

但不得将其描述为“两设备正式验收已完成”。只有在安装并登录 Tailscale 的服务端运行 `server --enable-serve`，并在另一台独立 Tailnet 设备运行默认要求节点不同的 `client`，由双方 `tailscale status --json` 提供不同稳定节点 ID，且保存无密钥证据后，才可将后者更新为 `PASSED`。

## 7. 非目标

H0 不实现：

- Cloudflare Pages/GitHub Pages；
- Runtime Config/API Base 解耦；
- CORS/`OPTIONS`；
- `/api/runtime/health`；
- Tailscale Funnel 或公开互联网访问；
- 代理身份 Header 免 Bearer；
- 开机自启和休眠治理的完整 H3 验收；
- 生产持仓写入；
- Quant、模型、信号、概率或交易逻辑改动。

## 8. 实施结果（2026-08-24）

```text
H0-A_LOOPBACK_AND_PUBLIC_BIND_GUARD = PASSED
H0-B_TAILSCALE_SERVE_OPERATOR = PASSED_BY_UNIT_AND_FAKE_CLI
H0-C_LOCAL_REMOTE_STYLE_ACCEPTANCE = PASSED
H0-C_REAL_SERVE = PENDING_TAILSCALE_INSTALL_AND_LOGIN
H0-C_TWO_DISTINCT_TAILNET_NODES = PENDING
H0-D_FULL_REVIEW = PASSED_ENGINEERING_READY_FOR_MERGE
H0-D_GIT_RELEASE = PENDING
```

已实现：

- 配置、CLI、`build_context` 与 `APIServer` 四层 loopback 失败关闭；
- 非 loopback 绑定只有同时提供目标 Host 与 `--allow-non-loopback` 才可启动；
- Docker/Procfile 将公网监听明确限制为 `PURE_CLOUD_EXPERIMENTAL`，`.dockerignore` 排除本地私有/运行产物；
- Tailscale Serve 目标固定 loopback，Token 只来自进程环境；
- 启用和停用均先检查现有 Serve JSON，遇到非 H0 target 时拒绝覆盖或关闭；
- 两设备证据升级为比较双方 Tailscale 稳定节点 ID，而不是只比较 hostname；
- 临时数据库 marker 在任何 Portfolio 写入前完成严格验证；
- 本地远程式静态页、REST、Bearer、SSE、Profile PUT 与 Position POST/PATCH/DELETE 全部通过；
- 生产 `data/stock_tracker.db` 在验收前后 SHA-256 保持一致。

当前执行宿主没有 Tailscale CLI，因此真实 Serve 与两节点验收必须保持 `PENDING`。这不阻止工程实现进入 Review 和 Git，但阻止任何“真实两设备已经通过”的声明。
