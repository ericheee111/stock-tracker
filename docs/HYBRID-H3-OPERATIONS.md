# Hybrid H3 Target Lane 与主机恢复操作合同

> 状态：`ENGINEERING_READY / REAL_HOST_ACCEPTANCE_PENDING`
>
> 日期：2026-08-26

## 1. Listener

```text
127.0.0.1:8080  本地整站和 H0 恢复入口
127.0.0.1:8081  H3 API-only Target
```

API Target 对 `/`、HTML、CSS、JS、图片等静态路径返回 404，仅处理 `/api/...`。两个 Listener 共享同一 Store、Repository、Scheduler 和 SSE Hub。

## 2. 基础启动

```bash
set STOCK_TRACKER_PRIVATE_ACCESS=<强随机值>
python -m stock_tracker --host 127.0.0.1
```

确认：

```text
8080 可加载整站
8081 / 返回 404
8081 /api/runtime/health 返回 metadata-only Health
非 allowlist Origin 被拒绝
```

## 3. Tailscale Serve Target 迁移

只读：

```bash
python scripts/hybrid_h3.py preflight
python scripts/hybrid_h3.py status
```

从 exact H0 整站 Target 迁移到 API Target：

```bash
python scripts/hybrid_h3.py migrate-target
```

恢复 H0 整站 Target：

```bash
python scripts/hybrid_h3.py rollback-target
```

工具只认 exact `127.0.0.1:8080` / `127.0.0.1:8081` ownership。存在其他 Serve/Funnel/Services/Mount 时失败，不运行 `serve reset`。迁移失败会验证恢复迁移前 Target。

## 4. Windows Task Scheduler

生成无密钥 XML 和 dry-run 计划：

```bash
python scripts/hybrid_h3.py task-plan
```

实际安装必须双重确认：

```bash
python scripts/hybrid_h3.py task-plan --apply --acknowledge-host-change
```

移除：

```bash
python scripts/hybrid_h3.py task-remove --apply --acknowledge-host-change
```

Task 直接监督 Python 进程，并配置失败重启、登录启动、StartWhenAvailable 与 WakeToRun。Token 不写入 XML；应通过受控用户环境或后续凭据管理方案提供。

## 5. Power Guard

默认：

```toml
prevent_sleep_during_trading = false
```

只有用户接受个人机器在交易时段保持唤醒后才改为 `true`。Guard 只请求系统保持运行，不请求显示器常亮；交易窗口结束或 Engine 停止即释放。此功能仍需在实际 Windows 电源策略、睡眠/唤醒和网络恢复环境完成 operational 演练。

## 6. Token 轮换

准备：

```bash
set STOCK_TRACKER_PRIVATE_ACCESS=<current>
set STOCK_TRACKER_NEW_PRIVATE_ACCESS=<replacement>
python scripts/hybrid_h3.py token-rotation-plan
```

输出只包含强度/不同性检查和步骤，不包含两个值。切换流程：

```text
将 replacement 写入受监督 Engine 环境
→ 重启 Engine
→ 验证 Runtime Health/Build
→ 目标设备重新认证
→ 验证旧值返回 401/403
→ 删除旧值
```

## 7. 审计

remote-style 写请求必须先写 metadata-only JSONL 审计：

```text
data/remote_access_audit.jsonl
```

日志不可写时远程写失败关闭。日志不包含 Token、请求 Body、账户、持仓、成本、Symbol、IP、数据库路径或 Provider 密钥。

## 8. Operational Pending

以下必须在真实主机/设备执行，仓库单测不能替代：

```text
Tailscale CLI 安装与登录
exact API Target 实际迁移/回滚
第二台独立 Tailnet 设备 REST/SSE/CRUD
Task Scheduler 实际安装、重启恢复
睡眠/唤醒/断网/网络恢复演练
Token 轮换与设备撤销
```
