# Hybrid Local + Cloud 部署兼容入口

> 状态：`SUPERSEDED_COMPATIBILITY_POINTER`
>
> 最新对齐日期：2026-08-24
>
> 唯一规范来源：`docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`
>
> 保留本文件名仅为了兼容中断会话、旧链接和历史审计；本文件不再独立定义部署合同。

## 1. 规范优先级

部署设计、实现、测试和验收必须以以下顺序解释：

1. 当前用户指令；
2. 根目录 `AGENTS.md`；
3. `docs/PRD-股票辅助判断与交易参考网站.md` v1.1；
4. `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`；
5. `docs/PRODUCT-GAP-MATRIX-v1.1.md`。

本文件与上述主规格冲突时，始终以主规格为准。

## 2. 旧术语映射

| 已退役草稿术语 | 当前规范术语 | 说明 |
|---|---|---|
| `LOCAL` / Mode L | `LOCAL_ONLY` | 本地开发与恢复入口，必须始终可用 |
| `HYBRID` / Mode H | `HYBRID_PRIVATE` | 当前默认正式模式 |
| `SNAPSHOT` / Mode S | `HYBRID_SNAPSHOT` | 后续非默认、脱敏、签名、短 TTL、只读能力 |
| `CLOUD` / Mode C | `PURE_CLOUD_EXPERIMENTAL` | 通过完整门禁前仅可实验 |
| D0–D4 | Hybrid H0–H5 | 不存在一一对应关系；实施顺序以 H0–H5 为准 |

`HYBRID_PUBLIC_AUTH` 是当前主规格中的可选公开访问模式，必须独立通过认证、CORS、限流、审计和隐私 Review。

## 3. 不变边界

- 默认正式路线是本地数据与决策引擎、云端静态网页、Tailscale Serve 私有远程访问；
- Oracle Cloud 为 `EXCLUDED`，不得成为账号、成本、灾备或阶段依赖；
- 本地 Backend 默认监听 loopback，不使用家庭路由器端口转发；
- 云端静态资产不得包含 Token、账户、持仓、成本、券商凭据或服务端密钥；
- 云端页面不得直连行情 Provider、SQLite 或 `free-stockdb` Sidecar；
- Engine、Tunnel、Auth、Provider、Data Freshness 与 Snapshot 状态必须分开；
- 断连或过期数据不得生成新的强执行动作；
- `HYBRID_SNAPSHOT` 不属于 Stage 1.5 H0–H5 的上线前置条件；
- `PURE_CLOUD_EXPERIMENTAL` 只有通过 Provider 可达性、持续运行、持久化、安全、恢复和成本门禁后才可升级。

## 4. 变更规则

后续部署决策只修改主规格 `docs/HYBRID-DEPLOYMENT-ARCHITECTURE-v1.md`，并同步 PRD、Gap Matrix、Overview、Handoff 与 `CHATGPT_HANDOFF.md`。不要在本文件重新建立第二套 Mode、状态、Runtime Config 或 Stage 合同。
