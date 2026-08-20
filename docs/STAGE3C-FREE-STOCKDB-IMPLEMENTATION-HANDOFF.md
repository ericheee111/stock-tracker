# Stage 3C free-stockdb Sidecar 实现交接

> 日期：2026-08-19
> 实现范围：Stage 3C.1 隔离合同与本地模拟验收
> 工程状态：待独立 Review 与全量门禁
> 证据边界：`T1_BEST_EFFORT / LICENSE_PENDING / T3_NOT_REACHED`

## 1. 实际交付

### Provider

```text
stock_tracker/collector/free_stockdb.py
```

包含：

- `FreeStockDbProvider`；
- `FreeStockDbReadEvidence`；
- `FreeStockDbContractError`；
- loopback-only URL 验证；
- no-redirect HTTP GET；
- 日线/分钟线有限范围 RAW 查询；
- strict UTF-8 / strict JSON；
- 字段、代码、时间、OHLC、单位类型与重复 Bar 校验；
- exact response SHA-256 与本地发行/数据身份绑定。

### Provider 配置与 Router

修改：

```text
stock_tracker/core/config.py
stock_tracker/collector/provider.py
stock_tracker/collector/router.py
stock_tracker/__main__.py
config/providers.toml
```

新增配置字段：

```text
enabled
bars_priority
read_only
trust_tier
allow_live_decision
allow_model_training
allow_public_redistribution
release_version
binary_inventory_sha256
data_snapshot_manifest_sha256
sync_manifest_sha256
```

Router 新增复权能力过滤：

```text
supports_adjustment(adjust)
```

因此 RAW 请求可以选择 Sidecar，`qfq/hfq` 不会错误命中它。

### 验收 CLI

```text
scripts/verify_free_stockdb_sidecar.py
```

作用：

- 对用户显式指定的本地、固定版本 Sidecar 执行有限范围查询；
- 要求 binary/data/manifest SHA-256；
- 输出 T1 PoC JSON 报告；
- 不读取或修改生产数据库；
- 不提供写、训练、回测、Trust 晋级或再分发选项。

### 测试

```text
tests/test_free_stockdb_provider.py
tests/test_config_contract.py
tests/test_bars_provider.py
```

覆盖：

- 默认关闭和注册表跳过；
- loopback-only；
- 禁止 DNS、局域网、HTTPS、凭据、path；
- read-only/T1/no-live/no-training/no-redistribution；
- release 与三个 SHA 身份必填；
- `cmd=get`、RAW-only；
- qfq 路由过滤；
- 日线和分钟线公开字段；
- strict JSON、duplicate key、NaN/Infinity；
- 代码、日期、OHLC、volume/amount/turnover；
- 重复时间和请求区间；
- evidence identity；
- 本地模拟 HTTP Server 到 CLI 的端到端回归。

## 2. 当前验证结果

定向命令：

```bash
python -m unittest \
  tests.test_free_stockdb_provider \
  tests.test_config_contract \
  tests.test_bars_provider -v
```

结果：

```text
40 tests
40 passed
```

静态检查：

```bash
ruff check \
  stock_tracker/collector/free_stockdb.py \
  scripts/verify_free_stockdb_sidecar.py \
  tests/test_free_stockdb_provider.py \
  stock_tracker/core/config.py \
  stock_tracker/collector/provider.py \
  stock_tracker/collector/router.py \
  stock_tracker/__main__.py \
  tests/test_config_contract.py
```

结果：

```text
PASS
```

该结果只覆盖本轮定向实现；合入前仍须执行完整 runtime + quant + smoke + migration dry-run + DB SHA 门禁。

## 3. 没有完成的真实验收

本轮没有：

- 下载或安装真实 free-stockdb Release；
- 执行真实上游更新器；
- 下载真实数据；
- 访问 7899 的真实 free-stockdb 进程；
- 审计真实发行包二进制；
- 监控真实网络连接；
- 核实真实同步源或数据许可；
- 对 50—100 个真实标的进行多源对账；
- 测量真实全市场吞吐；
- 使用上游复权因子或板块映射；
- 修改生产 SQLite；
- 把 Sidecar 用于真实信号、回测或模型。

所以配置继续保持：

```toml
enabled = false
```

## 4. 真实启用操作清单

Stage 3C.2 执行者必须：

1. 在仓库外新建独立目录；
2. 从明确 Release 下载资产，不使用不明网盘或第三方重打包；
3. 计算每个文件 SHA-256；
4. 记录文件清单、签名、版本和下载来源；
5. 在低权限账户或隔离环境中首次运行；
6. 记录更新器和数据库服务的进程/网络行为；
7. 固定 `sync_url`、manifest 和数据快照；
8. 确认服务只监听 `127.0.0.1`；
9. 禁止 7899 的入站局域网/公网访问；
10. 使用验收 CLI 生成初始报告；
11. 执行合同中的 50—100 标的差异矩阵；
12. 经独立 Review 后才允许把 `enabled` 改为 `true`。

示例命令：

```bash
python scripts/verify_free_stockdb_sidecar.py \
  --host 127.0.0.1:7899 \
  --release-version <PINNED_RELEASE> \
  --binary-path <PATH_TO_AUDITED_EXECUTABLE_OR_LIBRARY> \
  --data-snapshot-manifest-path <PATH_TO_DATA_SNAPSHOT_MANIFEST> \
  --sync-manifest-path <PATH_TO_SYNC_MANIFEST> \
  --symbol 600519.SH \
  --symbol 000001.SZ \
  --start 2026-06-01 \
  --end 2026-06-30 \
  --interval 1d \
  --output <NEW_REPORT_PATH>
```

输出路径必须不存在，避免覆盖旧证据。

## 5. 运行层使用方式

只有显式 RAW 查询才可能路由到 Sidecar：

```python
bars = router.fetch_bars(
    symbol="600519.SH",
    market=Market.A,
    interval="1d",
    start=start,
    end=end,
    adjust="raw",
)
```

现有 Scheduler 默认请求 `qfq`，因此不会因为添加 disabled Sidecar 就改变当前运行结果。未来 Shadow Scanner 必须单独显式请求 `raw`，再由本项目正式公司行为链决定是否以及如何构造 adjusted view。

## 6. Review 重点

独立 Reviewer 应攻击：

- `host` 是否可通过 DNS、IPv6、凭据、redirect、userinfo 或 URL 编码逃逸 loopback；
- 是否存在任何 `cmd=set` 或写接口；
- disabled 配置是否仍会实例化或访问本地端口；
- `qfq/hfq` 是否可能使用 RAW Sidecar；
- release 或 SHA 空值是否可能在 enabled 状态绕过；
- `trust_tier` 是否可改成 T2/T3；
- `allow_live_decision/model_training/public_redistribution` 是否可绕过；
- JSON duplicate key、非有限数、bool-as-int；
- 代码、时间范围、重复时间戳和响应形态；
- minute turnover 缺失是否被误解释为有效零值；
- evidence ID 是否绑定所有本地身份和 response bytes；
- CLI 是否能覆盖输入、写生产 DB、访问非本地地址或泄露数据；
- Router priority 是否影响 Quote/Snapshot 主源；
- Sidecar 失败是否污染正式信号或阻止远程回退。

## 7. 合并边界

可以合并的声明仅限：

```text
Stage 3C.1 工程合同已实现
synthetic localhost HTTP 已验证
默认配置不会启用 free-stockdb
```

不得声明：

```text
真实 free-stockdb 安全
真实数据完整或准确
数据许可已确认
Sidecar 已提升 Big Trend/策略准确率
Sidecar 可进入正式回测或训练
T2/T3 已达到
```
