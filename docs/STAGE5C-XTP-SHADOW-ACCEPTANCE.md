# Stage 5C — XTP Shadow Acceptance

> 工程 Fixture 状态：`PASSED`
>
> 真实 XTP Shadow：`PENDING`

## 1. 目标

在不使用真实账户、不改变 Provider 优先级、不产生交易动作的前提下，验证 XTP 快照与现有参考源的差异合同、异常保留和报告结构。

## 2. Fixture

```text
64 个 A 股标的
SH_MAIN / SZ_MAIN / CHINEXT / STAR
16 类场景
256 组比较
```

场景包括：

```text
正常交易
停牌
ST
涨停
跌停
重复回调
乱序回调
重连恢复
午休
开盘
收盘
Provider Sequence 不可用
参考源过期
参考源冲突
高流动性
低流动性
```

参考源：

```text
Tencent
Eastmoney
HiThink Financial-API
free-stockdb
```

HiThink 和 free-stockdb 在当前合同中是日频参考，固定返回 `NON_OVERLAPPING_FREQUENCY`，不得与盘中 XTP 快照硬比较或冒充实时验证。

## 3. 比较状态

```text
MATCH
CONFLICT
SOURCE_UNAVAILABLE
NON_OVERLAPPING_FREQUENCY
```

Fixture 最终计数：

```text
MATCH = 116
CONFLICT = 8
SOURCE_UNAVAILABLE = 4
NON_OVERLAPPING_FREQUENCY = 128
```

冲突不自动指定赢家；输出 `source_winner=null`。

## 4. 门禁

```text
synthetic_fixture_only = true
operational_live_account_pending = true
algorithm_account_used = false
no_real_strategy_claim = true
allow_live_decision = false
allow_model_training = false
allow_public_redistribution = false
auto_trade = false
source_promotion_performed = false
evidence_tier_status = T3_NOT_REACHED
```

## 5. 真实 Shadow 计划

只有在本机配置股票 Quote 账户和官方 Sidecar 后执行：

1. 50–100 个代表性 A 股标的；
2. 沪深主板、创业板、科创板；
3. 开盘、午休、收盘；
4. 停牌、ST、涨跌停；
5. 高低流动性；
6. 至少一次人工断网与恢复；
7. 时间戳、价格、累计成交量、Session、重复与缺口；
8. XTP 与 Tencent/Eastmoney 同频比较；
9. HiThink/free-stockdb 只做日频盘后对账；
10. 生成不含账户值的报告。

真实 Shadow 通过前，XTP 继续不进入正式 Runtime Router、模型训练或动作决策。

## 6. 命令

```text
python scripts/run_xtp_shadow_acceptance.py
```

该命令校验生产数据库 SHA 前后一致。
