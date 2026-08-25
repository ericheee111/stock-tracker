# Stage 1.5 Hybrid H0 独立对抗式 Review

> Review 日期：2026-08-24
>
> Review 范围：Hybrid H0 设计、默认监听、私有认证、Tailscale Serve Adapter、临时验收 Harness、部署文件、测试、PRD/架构状态与发布门禁
>
> 主规格：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`
>
> 设计：`docs/STAGE1.5-HYBRID-H0-DESIGN-PLAN.md`
>
> 实施交接：`docs/STAGE1.5-HYBRID-H0-IMPLEMENTATION-HANDOFF.md`

## 1. 最终判定

```text
ENGINEERING_READY_FOR_MERGE
OPERATIONAL_DEVICE_ACCEPTANCE_PENDING
```

工程实现、失败关闭、安全边界、本地远程式 REST/SSE/Portfolio CRUD 验收和完整仓库门禁均已达到可合入标准。

真实 Tailscale Serve 与两台不同 Tailnet 节点的 operational 验收仍为 `PENDING`，原因是当前执行宿主没有 Tailscale CLI，且本会话没有第二台可控 Tailnet 设备。该限制不构成工程合并阻断，但严格阻止“真实两设备已经通过”的声明。

## 2. Review 方法

本 Review 不信任实现阶段的文字结论，而是重新检查：

- 当前工作树与并行修改边界；
- 配置、CLI、`build_context`、`APIServer` 四层绑定路径；
- 私有 Bearer 的来源、强度、传播与日志边界；
- Tailscale CLI 命令、Serve JSON、Funnel、额外挂载、Services、Foreground 与停用语义；
- 临时验收 marker、fixture identity、数据库路径、写入前置条件和失败清理；
- SSE、REST 与 Portfolio CRUD 的真实 HTTP 行为；
- 两设备证据强度；
- Docker 构建上下文；
- 生产数据库 SHA-256；
- runtime、quant、QA、integration、migration dry-run、ruff 与 diff 门禁；
- 未纳入任务的并行 UI、构建包、截图、缓存和运行数据。

## 3. 发现与修复

### Finding H0-R1：`APIServer` 可被直接构造绕过 CLI 绑定门

严重度：`IMPORTANT`

初始实现只在 CLI/`build_context` 层拒绝未确认的非 loopback。仓库内或未来调用方可直接执行：

```python
APIServer("0.0.0.0", port, ctx, logger)
```

从而绕过 H0 的核心网络边界。

修复：

- `APIServer.__init__` 自身调用 `require_safe_bind()`；
- 新增 keyword-only `allow_non_loopback=False`；
- `build_context` 只有在 CLI 显式 `--allow-non-loopback` 时才向下传递；
- 新增直接构造 `0.0.0.0` 必须抛出 `UnsafeBindError` 的回归测试。

状态：`FIXED_AND_TESTED`

### Finding H0-R2：仅比较 hostname 不能证明两台不同 Tailnet 设备

严重度：`IMPORTANT`

hostname 可重复、可修改，也不能证明客户端实际登录了不同的 Tailscale 节点。若只比较 hostname，单机伪装或误配置可能被写成“两设备验收”。

修复：

- Server 与 Client 分别调用本机 `tailscale status --json`；
- 使用 `Self.StableID`，缺失时仅兼容 `Self.ID`；
- Server 将节点 ID 写入一次性临时 fixture marker；
- Client 将本机节点 ID交给验收器；
- 节点 ID 缺失或相同，在任何 Portfolio 写入前失败关闭；
- hostname 仅保留为辅助审计字段；
- 新增“不同稳定节点 ID 才可通过”和“相同节点 ID 禁止写入”的测试。

状态：`FIXED_AND_TESTED`

### Finding H0-R3：相同 Proxy target 可能与 Funnel、额外挂载或其他 Serve 配置共存

严重度：`CRITICAL`

初始 ownership 判定主要搜索 backend target 字符串。如果同一 Serve 配置同时存在：

- `AllowFunnel = true`，或使用字符串 `"false"` 等非布尔伪关闭值；
- 额外 mount；
- Tailscale Services；
- Foreground 配置；
- 额外 handler 字段；
- 多个 TCP listener；

则实现可能错误地把配置认作“由 H0 独占”，随后幂等启用或执行 `serve off`，造成意外公开暴露或干扰其他服务。

修复：

`_serve_config_summary()` 改为结构化精确 ownership 检查，只有以下全部满足才认为是 H0 配置：

```text
payload 是 object
AllowFunnel 缺失/为空，或所有叶子值都严格为 boolean false
Services 为空
Foreground 为空
恰好一个 Web handler
mount 恰好为 /
handler 只含 Proxy
Proxy 恰好为 http://127.0.0.1:<port>
恰好一个 TCP listener
listener 的 HTTPS 恰好为 true
没有其他启用的 TCP mode
没有未知的非空顶层 section
```

发现任何冲突时：

- `enable` 不覆盖；
- `disable` 不执行 `serve off`；
- 永不执行 `serve reset`。

新增 Funnel 已启用、字符串 `"false"` 伪关闭和额外挂载的负向测试，确认不会调用 `serve off`。

状态：`FIXED_AND_TESTED`

### Finding H0-R4：Docker 构建上下文可能携带本地私有/运行产物

严重度：`IMPORTANT`

Dockerfile 使用 `COPY . /app`。仅依赖 `.gitignore` 不能保证 Docker 构建上下文排除：

- `data/stock_tracker.db`；
- WAL/SHM/log；
- `build/`；
- ZIP/TAR；
- Agent 文件；
- cache；
- QA 截图。

修复：新增 `.dockerignore`，显式排除上述路径和类型，并增加回归测试。

状态：`FIXED_AND_TESTED`

### Finding H0-R5：弱或不一致的 Token 验证可能导致 Server 与运维工具漂移

严重度：`IMPORTANT`

HTTP Server 与 H0 CLI 若分别实现 Token 校验，长度、控制字符或首尾空白规则可能漂移。

修复：

- 新增 `stock_tracker/core/security.py`；
- Server 与 H0 Adapter 共同使用 `private_access_value_valid()`；
- Token 只从 `STOCK_TRACKER_PRIVATE_ACCESS` 进程环境读取；
- CLI 无 Token 参数；
- JSON evidence 明确 `contains_private_access=false`；
- 测试验证短值和首尾空白失败关闭。

状态：`FIXED_AND_TESTED`

### Finding H0-R6：Portfolio 验收不能触碰生产数据库

严重度：`CRITICAL`

两设备验收要求真实 PUT/POST/PATCH/DELETE，但不能把这些写入用户的生产账户与持仓数据库。

修复：

- `TemporaryH0Fixture` 始终创建临时 SQLite；
- 静态 Web 复制到临时目录；
- 写入前严格验证 marker schema、随机 32 hex fixture ID、`fixture_only is True`、`allow_portfolio_writes is True`、`production_database is False`；
- marker 不匹配、节点不满足或连接失败均在写入前退出；
- 创建的 Position 在成功或失败路径尽力清理；
- `local` 模式对生产数据库仅做二进制 SHA-256，不以 SQLite 打开；
- 完整验收前后生产数据库 SHA 一致。

状态：`FIXED_AND_TESTED`

### Finding H0-R7：正式客户端可把 Bearer 发送到任意用户输入 Origin

严重度：`CRITICAL`

初始 `client --base-url` 接受任意 HTTP/HTTPS Origin。即使两设备节点 ID 校验正确，用户输入错误、DNS 欺骗或复制了非 Tailscale 地址时，客户端仍可能先把私有 Bearer 发往不受信任的服务器。

修复：

- 正式两设备 `client` 在读取并发送 Bearer 前调用 `validate_tailnet_serve_origin()`；
- 只接受 `https://*.ts.net`；
- 只接受默认 HTTPS 443；
- 禁止 userinfo、path、query、fragment 和首尾空白；
- Server 输出的 DNS Origin 也经过同一规范化校验；
- `--allow-same-device` 仅保留本地诊断用途，不能形成 operational 通过结论；
- 新增 HTTP、非 `ts.net`、非 443、带路径和带 userinfo 的负向测试。

状态：`FIXED_AND_TESTED`

## 4. 最终实现边界

### 已完成

- 默认 `127.0.0.1`；
- 非 loopback 双重显式确认；
- `APIServer` 最终边界失败关闭；
- Tailscale CLI 解析与 Running/DNS/稳定节点 ID 检查；
- `tailscale serve --bg http://127.0.0.1:<port>`；
- 精确 Serve ownership 与冲突保护；
- Bearer 纵深防御；
- 同源静态 Web、REST、fetch-stream SSE；
- 临时 Portfolio Profile/Position CRUD；
- 生产数据库哈希保护；
- Docker 构建上下文保护；
- 设计、实施、Gap Matrix、PRD、Overview、Architecture 与 Handoff 同步。

### 未完成且不得误报

- 当前机器上的真实 Tailscale 安装/登录；
- 真实 Serve HTTPS；
- 两台不同 Tailnet 节点 operational 验收；
- H1 Runtime Config/API Base；
- H2 CORS/`OPTIONS`/`/api/runtime/health`；
- H3 开机自启、休眠、崩溃恢复；
- H4 Cloudflare Pages/GitHub Pages；
- H5 Funnel/Cloudflare Tunnel 公开访问。

## 5. 新鲜发布门禁

### 编译与静态检查

```text
python -m compileall -q stock_tracker tests tests_quant scripts
PASS

python -m ruff check <全部 H0 新增/修改 Python 文件>
PASS

git diff --check
PASS（仅 Windows LF→CRLF 提示）
```

`ruff format --check` 被 CodexPro 高风险命令守卫拦截，未执行，因此不列为通过项。

### Runtime

```text
python -m unittest discover -s tests -p "test_*.py"
380 passed, 1 skipped
```

最终一次运行无测试失败。此前一次完整运行出现过 `tempfile` 对 HTTP 404 对象的 `ResourceWarning`，最终重跑未复现；不把仓库描述为“永久 warning-free”。

### Quant

```text
python -m unittest discover -s tests_quant -p "test_*.py"
560 passed
```

迁移负向路径仍输出既有 SQLite `ResourceWarning`，退出码为 0。H0 未修改 Quant 代码。

### Dependency、Smoke 与 Benchmark

```text
python -m pip check
No broken requirements found.

python scripts/run_quant_contract_smoke.py
passed=true
synthetic_fixture_only=true
production_database_modified=false

python scripts/run_quant_fixture_benchmark.py
PASS
synthetic_fixture_only=true
promoted=false
reasons=ECE_REGRESSED,TIME_INSTABILITY
LightGBM unavailable
```

这些结果只证明工程和合成合同，不是投资表现证据。

### Migration

```text
python scripts/quant_migrate.py --database data/stock_tracker.db
mode=DRY_RUN
pending_count=4
database_modified=false
```

### 前端与真实 API/Web

```text
npm run today:qa
17/17 passed

python scripts/run_stage1_today_integration.py
Today 17/17 passed
Portfolio CRUD 13/13 passed
```

### H0 专项

```text
python -m unittest tests.test_hybrid_h0 -v
16/16 passed

python scripts/run_hybrid_h0_acceptance.py local
12/12 checks passed
operational_device_acceptance=PENDING
production_database_modified=false
```

### 生产数据库

验收前后 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

结论：`UNCHANGED`

## 6. 并行工作隔离

以下工作不属于 H0，不得进入本次 commit：

```text
web/css/*.css
web/index.html
web/js/app.js
build/**
stock-tracker-web.zip
qa/shots/**
stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc
```

它们未被本 Review 作为 H0 交付内容审阅或认可。

## 7. 发布建议

允许：

- 将 PRD v1.1 Hybrid 架构冻结与 H0 工程实现作为一个一致提交栈合入；
- 推送 `main`；
- 将下一代码切片切换为 H1/H2；
- 在真实宿主安装、登录 Tailscale 后补做 operational acceptance。

禁止：

- 声称 Tailscale Serve 已在当前宿主真实启用；
- 声称两设备已经通过；
- 将 H0 本地模拟证据写成生产网络证据；
- 将 synthetic Quant 结果写成真实策略表现；
- 混入并行 UI/build/ZIP/cache/data/screenshot 修改。
