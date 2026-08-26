# Stage 1.5 Hybrid H3–H5 实施交接

> 状态：`IMPLEMENTED_VERIFIED / OPERATIONAL_GATES_PENDING`
>
> 日期：2026-08-26
>
> 设计：`docs/STAGE1.5-HYBRID-H3-H5-DESIGN-PLAN.md`

## 1. 交付范围

本切片完成 H3/H4/H5 的仓库侧工程实现：

```text
H3  API-only loopback Target、Tailscale exact migration/rollback、远程写审计、
    Windows Task 计划、可选交易窗口 Power Guard、Token 轮换计划

H4  no-secret 静态构建、跨平台确定性 Hash、Cloudflare _headers、
    GitHub Pages 备选工作流、临时 SQLite 双 Origin Browser 验收

H5  Trusted Tailnet preflight、Funnel/Cloudflare Tunnel fail-closed public gate
```

真实 Tailscale、Windows Task、睡眠恢复、Cloudflare Pages、GitHub Pages 和任何公开入口没有在当前宿主实际执行。

## 2. H3 实现

### 2.1 API-only Target

配置：

```toml
api_target_enabled = true
api_target_port = 8081
```

运行时：

```text
127.0.0.1:8080  本地整站/H0 recovery
127.0.0.1:8081  API-only H3 target
```

API Target 不托管静态页面，避免 Pages 前端跨域连接时把本地整站意外暴露为第二份 UI。

### 2.2 远程写审计

新增 metadata-only rotating JSONL audit。remote-style 写请求在业务 Handler 前必须成功追加授权记录；写入失败返回 503，并阻止 Repository 修改。最终响应追加结果记录。

审计字段固定，不接受任意 payload 字段。动态 Position/Signal/Symbol 路径被模板化，不保存 Token、Body、账户、持仓、成本、Symbol、IP、数据库路径或 Provider URL。

### 2.3 Target 迁移/回滚

新增：

```text
stock_tracker/deployment/hybrid_h3.py
scripts/hybrid_h3.py
scripts/hybrid_h3.bat
```

工具只操作 exact H0/H3 ownership，不执行 `serve reset`。H0→H3 与 H3→H0 都具备迁移后精确复验和失败时恢复原 Target 的事务式逻辑。

### 2.4 Windows 恢复与 Power Guard

Task Scheduler XML：

- no-secret；
- 失败重启；
- 登录启动；
- StartWhenAvailable；
- WakeToRun；
- 直接监督 Python 进程；
- 默认 dry-run；
- 安装/删除需要 `--apply` 和 host-change acknowledgement。

Power Guard 默认关闭。显式启用后只在配置的市场交易窗口请求系统保持运行，停止/非交易窗口释放。US/Eastern 在无 IANA tzdata 时使用标准库 DST fallback。

### 2.5 Token 轮换

只验证 current/replacement 环境值的强度和不同性，不输出值、不自动修改环境。切换必须通过受监督进程环境和重启完成。

## 3. H4 实现

新增：

```text
stock_tracker/deployment/hybrid_h4.py
scripts/build_hybrid_h4.py
scripts/build_hybrid_h4.bat
scripts/build_hybrid_h4_ci.py
scripts/run_hybrid_h4_acceptance.py
qa/ui/hybrid_h4_qa.cjs
.github/workflows/deploy-hybrid-h4-github-pages.yml
```

静态构建：

- 只复制 `web/` 公共资产；
- UTF-8/LF 文本规范化；
- exact Runtime Config；
- exact CSP `connect-src`；
- Cloudflare `_headers`；
- GitHub `.nojekyll`/404 fallback；
- 内容哈希 manifest；
- symlink/cache/database/log/archive/private environment 拒绝；
- 临时目录构建和验证；
- 激活失败恢复上一 verified build。

Cloudflare Pages 使用 `scripts/build_hybrid_h4_ci.py` 和公开环境变量。GitHub Pages 通过手动 workflow 构建；由于 GitHub Pages 不能由本构建注入任意响应 Header，manifest 明确 `response_headers_supported=false`，只声明 meta CSP/referrer fallback。

本地 H4 验收使用两个不同 loopback Origin、API-only Backend、随机会话 Token、临时 SQLite 和真实 Playwright，覆盖在线 CRUD/SSE 和 Engine Offline 静态 Shell。

## 4. H5 实现

新增：

```text
stock_tracker/deployment/hybrid_h5.py
scripts/hybrid_h5.py
docs/HYBRID-H5-PUBLIC-ACCESS-GATE.md
```

Trusted Tailnet 是唯一可通过的共享 preflight。公开 Funnel/Tunnel 仍因没有公开速率限制和没有公开 enable action 被强制阻断。工具不会修改 Tailscale、DNS、Tunnel 或 Firewall。

## 5. `.pyc` 处理

历史 tracked CPython 缓存不属于源码。计划从 Git index 移除：

```text
stock_tracker/quant/data/__pycache__/__init__.cpython-313.pyc
stock_tracker/quant/data/__pycache__/__init__.cpython-314.pyc
stock_tracker/quant/data/__pycache__/bar_artifact.cpython-314.pyc
stock_tracker/quant/data/__pycache__/manifest.cpython-313.pyc
stock_tracker/quant/data/__pycache__/manifest.cpython-314.pyc
```

本地文件可由解释器保留/重建；提交中只体现“不再跟踪”。新增 source-distribution 测试阻止未来 tracked `.pyc/.pyo/__pycache__`。

## 6. 使用入口

```bash
# H3
python scripts/hybrid_h3.py status
python scripts/hybrid_h3.py migrate-target
python scripts/hybrid_h3.py rollback-target
python scripts/hybrid_h3.py token-rotation-plan
python scripts/hybrid_h3.py task-plan

# H4
python scripts/build_hybrid_h4.py build --web-origin ... --api-origin ... --engine-id ... --build-id ...
python scripts/build_hybrid_h4.py verify
python scripts/run_hybrid_h4_acceptance.py

# H5
python scripts/hybrid_h5.py --mode TRUSTED_TAILNET
python scripts/hybrid_h5.py --mode TAILSCALE_FUNNEL
```

## 7. 最终验证结果

| 门禁 | 结果 |
|---|---:|
| H0–H5 部署专项 unittest | 62/62 通过 |
| H3/H4/H5 新增专项 | 26/26 通过 |
| 运行产品全量 unittest | 426 通过，1 跳过 |
| Quant 全量 | 561 通过，244 subtests 通过 |
| H4 生成静态站点真实浏览器在线/离线 | 15/15 通过 |
| H1/H2 跨 Origin 浏览器主场景 | 28/28 通过 |
| H1/H2 Config/Health/Build/STALE 负向场景 | 11/11 通过 |
| H0 本地远程式验收 | 12/12 通过 |
| Mock Today | 17/17 通过 |
| 真实 API/Web Today | 17/17 通过 |
| Portfolio CRUD | 13/13 通过 |
| Source distribution + no tracked bytecode | 3 通过，45 subtests 通过 |
| `compileall` | 通过 |
| 新增/直接修改 Python targeted Ruff | 通过 |
| Node 语法检查 | 通过 |
| `pip check` | 无损坏依赖 |
| Quant contract smoke | `passed=true`，synthetic only |
| Synthetic fixture benchmark | 通过；Challenger 未晋级 |
| Production migration dry-run | `database_modified=false`，4 pending |

生产数据库验证前后 SHA-256 相同：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

H4 构建重复执行得到相同 manifest SHA；浏览器验收只使用临时目录、临时 SQLite 和随机会话访问值。Quant smoke/benchmark 明确为 synthetic fixture，不构成真实投资表现；Challenger 继续因 `ECE_REGRESSED` 和 `TIME_INSTABILITY` 被阻止晋级。

全仓库仍存在本阶段之前的历史 Ruff 债务，因此只声明新增和直接修改表面的 targeted Ruff 通过；`ruff format --check` 不列为通过项。

## 8. 未完成的 operational 门禁

```text
REAL_TAILSCALE_SERVE_API_TARGET
TWO_DISTINCT_TAILNET_NODES
WINDOWS_TASK_ACTUAL_INSTALL
REAL_REBOOT/SLEEP/NETWORK_RECOVERY
CLOUDFLARE_PAGES_ACTUAL_DEPLOYMENT
GITHUB_PAGES_ACTUAL_DEPLOYMENT
PUBLIC_RATE_LIMIT
ANY_PUBLIC_FUNNEL_OR_TUNNEL
```

这些门禁需要外部账号、CLI、真实主机和第二设备；本地测试不替代。
