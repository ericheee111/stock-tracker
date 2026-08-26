# Stage 1.5 Hybrid H3–H5 独立工程 Review

> Review 状态：`PASSED_WITH_OPERATIONAL_GATES_PENDING`
>
> 日期：2026-08-26
>
> 最终判定：`ENGINEERING_READY_FOR_MERGE / OPERATIONAL_GATES_PENDING`

## 1. Review 范围

- API-only Target 与双 Listener 隔离；
- Tailscale Serve exact ownership、迁移与双向失败回滚；
- 远程写审计的前置失败关闭、隐私字段和轮换；
- Windows Task/Power Guard 的主机影响、默认关闭和显式确认；
- Token 轮换不泄露；
- 静态构建的 no-secret、CSP、symlink/path、Hash 和原子激活；
- Cloudflare/GitHub Pages 发布合同的诚实边界；
- H5 public gate 是否存在绕过；
- CPython bytecode 是否仍被 Git 跟踪；
- H0/H1/H2、Today、Portfolio、Quant 和生产数据库回归。

## 2. 已识别并修复的 Findings

| 级别 | Finding | 处理 |
|---|---|---|
| CRITICAL | H0→H3 preflight 若直接以 API target 检查，会把 exact H0 初态误判为冲突 | 先读取并精确分类 ownership，再按当前 exact Target 运行 preflight |
| CRITICAL | 仅复用 H0 探针不能证明 8081 是 API-only Listener，可能把整站 Listener 迁入 Target Lane | 新增 H3 直连 preflight：根路径必须 404、Runtime Health 必须为 `HYBRID_PRIVATE` metadata 合同，且两类响应都必须带 Request ID；测试确认整站 Listener 被拒绝 |
| CRITICAL | H3→H0 失败可能把 API Target 留空 | 增加反向事务回滚；H0 恢复失败时验证恢复原 H3 API Target |
| CRITICAL | 远程写在审计不可写时仍可能进入 Repository | 审计授权记录成为业务写前置；失败返回 503，测试验证 Profile 未落库 |
| CRITICAL | 审计接口若接受任意 dict，调用方可能误写 Token/Body | Logger 按固定字段重建记录，未知 kwargs 丢弃，动态 ID 模板化 |
| CRITICAL | 固定 US UTC offset 会在 DST 期间漂移一小时，影响 STALE 与 Power Guard | 首选 ZoneInfo；缺 tzdata 时实现 US/Eastern 标准库 DST fallback |
| CRITICAL | 静态构建安全 Fetch/CORS 正确，但 CSP 若使用 wildcard 仍可外连 | `connect-src` 固定为 self + 单一规范化 API Origin；wildcard 验证失败 |
| CRITICAL | 静态包替换时先删除旧包，激活失败会失去可部署版本 | 同盘临时构建、验证、备份旧目录、激活失败恢复上一 verified build |
| IMPORTANT | Windows Task/休眠设置会改变个人主机行为 | Task 默认 dry-run，安装/删除双重确认；Power Guard 默认关闭 |
| IMPORTANT | Task XML/CLI 可能固化 Bearer | Token 只来自进程环境，XML/命令/JSON 输出均不包含值 |
| IMPORTANT | `runtime-config.example.js`、数据库、日志、symlink 或 `.pyc` 可能进入静态包 | 复制和验证双阶段拒绝，Hash manifest 校验精确文件集合 |
| IMPORTANT | Windows 与 Linux line ending 会让同一 Commit 的 manifest 不同 | 公共文本统一 UTF-8 + LF 后再 Hash |
| IMPORTANT | GitHub Pages 不能由静态仓库任意设置响应 Header | manifest 明确 `response_headers_supported=false`，只声明 meta fallback |
| IMPORTANT | H5 公开入口可能被“preflight pass”误用为批准 | 公开模式始终因 rate limit/enable action 缺失而 blocked，CLI 无 enable |
| IMPORTANT | tracked `.pyc` 容易被误当 Quant 交付证据 | 移除 5 个历史缓存的 Git tracking，新增 source-distribution 门禁 |

## 3. 安全结论

### 3.1 API Target

目标只能是 loopback API-only Listener。静态路径不提供内容；H0 本地整站继续作为恢复路径。Target 迁移不使用 `serve reset`，不接受 Funnel/额外挂载/未知配置。

### 3.2 Remote Audit

remote-style 写请求没有可用审计时失败关闭。业务写入前的 `AUTHORIZED` 记录是强制门禁；最终 `SUCCEEDED/REJECTED` 记录为 best effort，因此审计设备在业务写完成后瞬时故障不会回滚已提交的 Repository 事务。记录不包含 Token、Body、Portfolio、Symbol、IP、DB 路径或 Provider 密钥。Request ID 响应头用于本地关联。

### 3.3 Static Build

Static Artifact 不包含秘密、数据库、日志、缓存、归档、symlink 或构建示例；Runtime Config 只有公开元数据。Cloudflare CSP/Headers 和 GitHub fallback 声明分开，未伪造平台能力。

### 3.4 Public Access

Trusted Tailnet 保持默认。Funnel/Cloudflare Tunnel 仍无仓库 enable 动作，未实现 public rate limit，因此不能通过 H5。

## 4. 产品与金融正确性

本切片只改变部署、网络、审计和运行恢复边界，没有修改金融决策公式、ActionState、仓位、Exit、概率或模型晋级。STALE/Offline 继续阻止执行型动作，不把静态页面可加载误认为 Engine 在线。

## 5. 门禁结果

| 门禁 | 结果 |
|---|---:|
| H0–H5 部署专项 unittest | 62/62 通过 |
| H3/H4/H5 新增专项 | 26/26 通过 |
| 运行产品全量 unittest | 426 通过，1 跳过 |
| Quant 全量 | 561 通过，244 subtests 通过 |
| H4 生成站点真实浏览器验收 | 15/15 通过 |
| H1/H2 浏览器主场景 + 负向场景 | 28/28 + 11/11 通过 |
| H0 本地远程式验收 | 12/12 通过 |
| Mock Today / 真实 Today / Portfolio CRUD | 17/17、17/17、13/13 通过 |
| Source distribution / no tracked bytecode | 3 通过，45 subtests 通过 |
| `compileall` / targeted Ruff / Node syntax / `pip check` | 通过 |
| Quant contract smoke / synthetic benchmark | 通过，均为 synthetic only |
| Production migration dry-run | 未修改数据库，4 pending |

生产数据库验证前后 SHA-256：

```text
1cde40aa66846630d89b10d080a8837d204266c5ce32001a45d3b0c0c06197b1
```

Challenger 继续因 `ECE_REGRESSED`、`TIME_INSTABILITY` 被阻止晋级。全仓库旧 Ruff 债务和 `ruff format --check` 未被虚构成通过项；本 Review 只确认新增与直接修改 Python 表面的 targeted Ruff。

## 6. Operational Pending

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

## 7. 最终判定

```text
ENGINEERING_IMPLEMENTATION = COMPLETE
SECURITY_REVIEW = PASSED
LOCAL_TARGET_LANE_ACCEPTANCE = PASSED
LOCAL_STATIC_BUILD_AND_BROWSER_ACCEPTANCE = PASSED
REGRESSION_GATES = PASSED
PYTHON_BYTECODE_TRACKING = REMOVED
ENGINEERING_READY_FOR_MERGE = TRUE
OPERATIONAL_REMOTE_DEPLOYMENT = PENDING
PUBLIC_ENABLE_ACTION = NOT_SHIPPED
```

H3–H5 可以进入定向 staging、staged-tree 独立验证、commit 与 `main` 推送。任何真实 Tailscale、Windows Task、Pages 或公开入口状态都不得因本地测试而改写为 `PASSED`。
