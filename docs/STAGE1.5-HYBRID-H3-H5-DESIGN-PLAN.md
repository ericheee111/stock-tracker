# Stage 1.5 Hybrid H3–H5 设计与执行计划

> 状态：`IMPLEMENTED_REVIEWED`
>
> 日期：2026-08-26
>
> 前置阶段：Hybrid H0、H1/H2 已完成工程实现并推送
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`

## 1. 阶段目标

本轮连续完成三个相邻部署切片的工程边界：

```text
H3：API Target Lane + 远程写审计 + Windows 恢复/休眠保护 + 安全迁移/回滚
H4：无密钥静态发布构建 + exact CSP/connect-src + 本地真实浏览器上线/离线验收
H5：可信 Tailnet 优先 + 公开入口失败关闭门禁（不提供公开 enable 动作）
```

阶段不修改 Quant、ActionState、PositionSizer、TradePlan、Exit、概率、Big Trend、Strategy Scoreboard 或模型晋级语义。

## 2. H3：Serve API Target Lane 与运行加固

### 2.1 双 Loopback Listener

```text
127.0.0.1:8080  本地整站 / LOCAL_ONLY 与 H0 恢复入口
127.0.0.1:8081  API-only Target；只允许 /api/...，静态路径返回 404
```

两个 Listener 共享同一 AppContext、Store、Repository、Scheduler 和 SSE Hub，不复制数据库，也不开放 LAN/Public bind。API Target 与主端口必须不同；任何非 loopback 绑定继续要求既有双重显式确认。

### 2.2 Tailscale Serve 迁移

H3 工具只接受以下三种安全初态：

```text
EMPTY
exact H0 whole-site target: http://127.0.0.1:8080
exact H3 API target:        http://127.0.0.1:8081
```

存在 Funnel、额外挂载、未知 Section、不同 Proxy、Services、Foreground 或其他 Listener 时失败关闭。迁移过程：

```text
API Target preflight
→ 读取结构化 Serve status
→ exact H0 才允许 serve off
→ 再启用 exact H3 target
→ 重新读取并精确验证
→ 失败时尝试恢复 exact H0，并验证回滚
```

禁止使用 `tailscale serve reset`。

### 2.3 远程写审计

所有 remote-style `POST/PUT/PATCH/DELETE` 在进入业务写操作前必须成功写入 append-only JSONL 元数据审计；审计不可写时返回 `REMOTE_AUDIT_UNAVAILABLE`，不得修改 Portfolio/Watchlist/Event 等事实。

审计只记录：

```text
request_id
时间
HTTP 方法
模板化 route（动态 ID 被替换）
客户端边界类别
Origin
认证结果/最终结果
HTTP 状态
```

不得记录 Token、请求 Body、账户、持仓、成本、Symbol、IP、数据库路径或 Provider 密钥。日志有大小上限和轮换数量。

### 2.4 Windows 恢复与休眠

- 生成 Task Scheduler XML，默认只生成计划，不修改主机；
- `--apply` 与单独 host-change acknowledgement 同时存在才可安装/删除；
- Task 直接监督 `python -m stock_tracker --host 127.0.0.1`，包含失败重启、登录后启动、网络恢复后补启动和 WakeToRun；
- Token 不写入 XML或命令行，必须由受控进程环境提供；
- 交易时段 Power Guard 代码存在但默认关闭，避免未经用户选择让个人电脑跨夜保持唤醒；
- 显式启用时只在已启用市场的交易窗口请求 Windows `ES_SYSTEM_REQUIRED`，退出交易窗口或关闭 Engine 即释放；
- US market 在缺少 IANA tzdata 时使用标准库 DST fallback，避免固定 UTC offset 漂移一小时。

### 2.5 Token 轮换

轮换工具只从：

```text
STOCK_TRACKER_PRIVATE_ACCESS
STOCK_TRACKER_NEW_PRIVATE_ACCESS
```

读取旧/新值，验证强度和不同性，但输出中不包含任何值。实际切换通过受监督 Engine 环境 + 重启完成；旧值必须在新值验收后失效。

## 3. H4：云端静态网页工程发布链

### 3.1 确定性构建

新增构建器：

```text
web/
→ 过滤本地/私有/解释器生成物
→ 写入无密钥 runtime-config.js
→ 注入 CSP/referrer meta
→ 生成 Cloudflare _headers 或 GitHub fallback
→ 生成 404.html/.nojekyll
→ 内容哈希 manifest
→ no-secret 与完整性复验
→ 原子替换 build 输出
```

禁止进入构建：

```text
.env
数据库/WAL/SHM
日志/PID
ZIP
.pyc/.pyo/__pycache__
runtime-config.example.js
Symlink
实际会话 Token
```

### 3.2 安全 Header/CSP

Cloudflare Pages 构建生成：

```text
Content-Security-Policy
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Permissions-Policy
Cross-Origin-Opener-Policy
```

`connect-src` 只允许 `'self'` 与构建时固定的单一 API Origin，禁止 `*`。`runtime-config.js` 与 `index.html` 使用 `no-store`。GitHub Pages 备选保留 CSP/referrer meta 和 `.nojekyll`，并在 manifest 中诚实标记“不能由仓库构建证明响应 Header 已设置”，不得冒充与 Cloudflare `_headers` 等价。

### 3.3 本地真实浏览器验收

用临时目录、临时 SQLite 和两个不同 loopback Origin 验证：

```text
静态响应 Header/CSP
Runtime Config / Engine / API Major / Build 握手
Bearer Portfolio CRUD
fetch-stream SSE
远程写审计无秘密
390/1280 无横向溢出
Engine offline 时静态 Shell 仍加载
Offline 时不保留 EXECUTABLE 动作
生产数据库 SHA 不变
```

本地验收只能证明静态发布包和跨 Origin 合同，不等于 Cloudflare Pages 或 GitHub Pages 已实际上线。

## 4. H5：公开访问失败关闭合同

H5 工具只有 `preflight`，没有 Funnel/Tunnel enable 动作。

优先顺序：

```text
TRUSTED_TAILNET
→ 必要时另行设计 TAILSCALE_FUNNEL 小范围实验
→ 必要时自有域名 + CLOUDFLARE_TUNNEL
```

Trusted Tailnet 只需沿用 H3 Bearer、API Target 和审计边界。任何公开入口还必须满足：

```text
HYBRID_PUBLIC_AUTH 显式模式
exact HTTPS CORS
强 Bearer
远程写审计
独立公开安全 Review ID
显式 public-exposure acknowledgement
公开速率限制
```

当前仓库尚未实现公开速率限制，也不暴露公开 enable 命令，因此 Funnel/Cloudflare Tunnel preflight 必须返回 blocked。这是安全设计，不是待“绕过”的失败。

## 5. Python Bytecode 清理

`stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc` 是 CPython 3.14 根据 `__init__.py` 生成的 timestamp-based 字节码缓存：

- 不是真实源码；
- 可由当前解释器重新生成；
- 与 Python minor/version、编译选项和源文件时间相关；
- 不构成 Quant 证据或可移植发布物；
- 已被 `__pycache__/` 和 `*.pyc` ignore 规则覆盖。

历史误入 Git 的 5 个 `.pyc` 应仅从 Git index 移除，保留本地缓存不作交付。新增 source-distribution 回归门禁，未来任何 `.pyc/.pyo/__pycache__` 被跟踪都失败。

## 6. 验收与 Review 门禁

```text
H3/H4/H5 专项单测
H4 真实浏览器在线/离线验收
H0、H1/H2 回归
Runtime 全量 unittest
Quant 全量 pytest
Source distribution / no tracked bytecode
Mock Today
真实 Today + Portfolio
compileall
Targeted Ruff
pip check
Quant contract smoke
Synthetic fixture benchmark
Production migration dry-run + SHA
staged-tree 独立验证
Independent Review
Git push + 三方 SHA 一致
```

全仓库既有 Ruff 债务不能伪装为本阶段回归；本阶段新增和直接修改的 Python 表面必须通过 targeted Ruff。

## 7. Operational 边界

当前环境没有 Tailscale CLI、已登录的 Tailnet 第二设备、Wrangler 或已认证 GitHub Pages/Cloudflare 账号。因此本轮可以完成并合并 H3/H4/H5 的工程实现与本地验收，但以下状态必须继续为 `PENDING`：

```text
REAL_TAILSCALE_SERVE_API_TARGET
TWO_DISTINCT_TAILNET_NODES
WINDOWS_TASK_ACTUAL_INSTALL
SLEEP/CRASH/NETWORK_REAL_HOST_DRILL
CLOUDFLARE_PAGES_ACTUAL_DEPLOYMENT
GITHUB_PAGES_ACTUAL_DEPLOYMENT
ANY_PUBLIC_FUNNEL_OR_TUNNEL
```

## 8. 实施结果

H3–H5 已按本计划完成仓库侧工程实现和本地验收：

```text
H3 API-only Target / audit / migration rollback / host plans = VERIFIED
H4 deterministic static build / browser online-offline acceptance = VERIFIED
H5 trusted-tailnet preflight / public fail-closed gate = VERIFIED
tracked Python bytecode removal = VERIFIED
engineering review = PASSED
```

最新工作树证据为 H0–H5 部署专项 62/62、运行产品 426/1、Quant 561 + 244 subtests、H4 浏览器 15/15、H1/H2 浏览器 28/28 + 11/11、H0 本地验收 12/12、Today 17/17、Portfolio 13/13。真实 Tailscale、两设备、Windows Task/休眠恢复、Pages 实际部署和任何公开入口继续保持 `PENDING`。
