# Hybrid H5 公开访问门禁

> 状态：`FAIL_CLOSED`
>
> 日期：2026-08-26

## 默认选择

少量可信用户优先加入 Tailnet：

```bash
python scripts/hybrid_h5.py --mode TRUSTED_TAILNET
```

该命令只读检查强 Bearer、API-only Target 和远程写审计，不修改 Tailscale、DNS、Tunnel、Firewall 或 Router。

## 公开入口

以下模式当前只能执行门禁检查：

```bash
python scripts/hybrid_h5.py --mode TAILSCALE_FUNNEL
python scripts/hybrid_h5.py --mode CLOUDFLARE_TUNNEL
```

仓库故意不提供 enable 动作。即使提供 acknowledgement 和 Review ID，当前也会因以下阻断保持失败：

```text
PUBLIC_RATE_LIMIT_NOT_IMPLEMENTED
PUBLIC_ENABLE_ACTION_NOT_IMPLEMENTED
```

其他前置条件：

```text
HYBRID_PUBLIC_AUTH 显式部署模式
强 Bearer
API-only Target
metadata-only remote write audit
exact HTTPS CORS
public-exposure acknowledgement
独立公开安全 Review ID
```

不得通过手工执行 Funnel/Tunnel 命令绕开本门禁后声称项目已批准公开访问。真正实施公开入口需要单独 Stage：公开速率限制、滥用测试、撤销流程、故障演练、日志留存与独立 Review。
