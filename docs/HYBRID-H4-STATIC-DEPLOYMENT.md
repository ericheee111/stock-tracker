# Hybrid H4 静态部署操作合同

> 状态：`ENGINEERING_READY / REAL_DEPLOYMENT_PENDING`
>
> 日期：2026-08-26

## 1. 不可变边界

静态构建只接受公开元数据：

```text
Web HTTPS Origin
API HTTPS Origin
Engine ID
API Major
Build/Commit ID
Health poll interval
```

禁止向静态构建环境提供：

```text
STOCK_TRACKER_PRIVATE_ACCESS
STOCK_TRACKER_NEW_PRIVATE_ACCESS
账户/持仓/成本
Provider 密钥
数据库或日志
```

构建器发现私有访问环境变量时失败关闭。

## 2. Cloudflare Pages（首选）

在 Cloudflare Pages 的 Git 集成中配置：

```text
Build command:       python scripts/build_hybrid_h4_ci.py
Build output folder: build/hybrid-h4-static
```

公开环境变量：

```text
STOCK_TRACKER_WEB_ORIGIN=https://<project>.pages.dev
STOCK_TRACKER_API_ORIGIN=https://<engine>.<tailnet>.ts.net
STOCK_TRACKER_ENGINE_ID=stock-tracker-local
STOCK_TRACKER_EXPECTED_API_MAJOR=1
STOCK_TRACKER_STATIC_HOST=cloudflare
```

`CF_PAGES_COMMIT_SHA` 自动成为前端 Build ID；Backend `[runtime].commit_id` 必须同步为同一值，否则 Browser 进入 `BUILD_MISMATCH` 并阻止私有请求。

Backend 还必须配置：

```toml
[runtime]
cors_allowed_origins = ["https://<project>.pages.dev"]
```

Preview 部署会产生不同 Origin。默认不要把通配 preview Origin 加入 CORS；只有明确列出并审查的 preview Origin 才可连接私有 Engine。

Cloudflare 构建包含 `_headers`，用于 CSP、Referrer、nosniff、frame deny、Permissions Policy 和 no-store Runtime Config。

## 3. GitHub Pages（备选）

仓库提供手动工作流：

```text
.github/workflows/deploy-hybrid-h4-github-pages.yml
```

手动输入：

```text
web_origin
api_origin
engine_id
expected_api_major
```

工作流构建 `host=github` 的 no-secret Artifact 并部署 Pages。GitHub Pages 备选可证明 HTML meta CSP/referrer 与 Artifact 完整性，但本仓库构建不能为 GitHub Pages 注入任意响应 Header；manifest 因此明确：

```text
response_headers_supported = false
```

不得把它描述成和 Cloudflare `_headers` 完全等价。

## 4. 本地构建与验证

Cloudflare 形态：

```bash
python scripts/build_hybrid_h4.py build \
  --web-origin https://example.pages.dev \
  --api-origin https://engine.example.ts.net \
  --engine-id stock-tracker-local \
  --build-id <backend-commit-id> \
  --host cloudflare

python scripts/build_hybrid_h4.py verify
```

GitHub Pages 形态把 `--host` 改为 `github`。

本地双 Origin + 临时 SQLite + Browser 验收：

```bash
python scripts/run_hybrid_h4_acceptance.py
```

该命令不部署远端，也不修改生产数据库。

## 5. 上线前验收

实际声明上线前必须同时满足：

```text
静态 URL 可从目标设备加载
Runtime Config API Origin 精确
Backend CORS 只含实际静态 Origin
Engine/API Major/Build handshake 通过
Bearer 不在 HTML/JS/URL/日志
REST/SSE/Portfolio CRUD 从第二设备通过
Engine 关闭后页面显示 ENGINE_OFFLINE
Token 轮换后旧值失败
设备撤销/ACL 生效
主机重启、休眠和网络恢复演练通过
```

在完成这些操作前，项目状态只能是：

```text
H4_ENGINEERING_READY
H4_REAL_DEPLOYMENT_PENDING
```
