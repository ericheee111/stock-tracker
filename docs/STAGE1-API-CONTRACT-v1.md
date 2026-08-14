# Stage 1 API Contract v1｜Today Action 与 Portfolio

> 状态：已实现并通过真实 API + Web 集成验收
> 原则：字段可以后续扩展，但现有字段不得在没有迁移说明时改名或改变语义。

---

## 1. 通用规则

所有决策响应至少带：

```json
{
  "as_of": "2026-08-14T01:30:00+08:00",
  "data_status": "LIVE",
  "ranking_mode": "RULE_EVIDENCE",
  "schema_version": "stage1-v1"
}
```

规则：

- datetime 必须带时区；
- JSON 不允许 NaN/Infinity；
- 概率未知时必须为 `null`；
- `RULE_EVIDENCE` 不得显示成“历史成功率”；
- Big Trend 未实现时返回 `NOT_AVAILABLE`，不能使用 SectorScore 代替；
- API 错误使用明确 HTTP 状态和 `error.code/message`；
- malformed JSON 返回 400，不能静默当空对象；
- 私有 JSON 请求体最大 64 KiB，超出返回 413 `REQUEST_TOO_LARGE`。

### 1.1 私有 API 访问规则

以下端点含账户净值、持仓或建议股数，属于私有 API：

```text
/api/brief/today
/api/portfolio
/api/portfolio/profile
/api/portfolio/positions
/api/portfolio/positions/*
```

安全规则：

- 本机直连时，只有 TCP 客户端地址和 HTTP `Host` 都是 localhost/loopback 才可免认证；
- 经 Render、Cloudflare 或其他反向代理访问时，即使后端看到的 TCP 来源是本机，也不能绕过认证；
- 公网部署若未配置 `STOCK_TRACKER_PRIVATE_ACCESS`，私有 API 返回 503 `PRIVATE_API_DISABLED`；
- 配置后必须发送完全匹配的 `Authorization: Bearer ...`，否则返回 401；
- Web 端只在当前浏览器会话的 `sessionStorage.stockTrackerPrivateAccess` 中读取访问值，不写入仓库或长期本地存储；
- 不得把访问值提交到 Git、写入公开前端文件、日志或错误响应。

本机浏览器无需额外设置。公网私有部署可在用户主动配置后，于浏览器控制台执行：

```javascript
sessionStorage.setItem('stockTrackerPrivateAccess', '[PRIVATE_ACCESS_VALUE]');
location.reload();
```

---

## 2. GET `/api/brief/today`

Stage 1 mock/目标结构：

```json
{
  "schema_version": "stage1-v1",
  "as_of": "2026-08-14T09:45:00+08:00",
  "data_status": "LIVE",
  "ranking_mode": "RULE_EVIDENCE",
  "market_posture": {
    "market": "A",
    "regime": "ROTATION",
    "label": "震荡轮动",
    "aggression_level": 55,
    "strongest_theme": "机器人",
    "main_risk": "高位科技股拥挤"
  },
  "summary": {
    "mode": "DETERMINISTIC_TEMPLATE",
    "text": "今天以持仓管理为主，只选择性开新仓。",
    "facts": [
      "2 个新机会达到可执行或接近执行条件",
      "1 个持仓需要处理",
      "校准成功概率尚不可用"
    ]
  },
  "actions": {
    "executable_count": 1,
    "waiting_count": 2,
    "holding_attention_count": 1
  },
  "core_opportunities": [
    {
      "symbol": "600000.SH",
      "market": "A",
      "name": "示例股票",
      "action_state": "WAIT_PULLBACK",
      "action_label": "等回踩确认",
      "opportunity_grade": "A",
      "strategy_id": "S2",
      "strategy_version": "runtime-v1",
      "scores": {
        "opportunity": 84,
        "timing": 72,
        "risk": 41,
        "confidence": 66
      },
      "model": {
        "tendency": "STRONG",
        "score": null,
        "calibrated_probability": null,
        "probability_evidence_level": "INSUFFICIENT",
        "message": "真实样本或校准证据不足，暂不展示概率"
      },
      "trade_plan": {
        "entry_low": 10.1,
        "entry_high": 10.4,
        "trigger_price": 10.5,
        "no_chase_above": 10.7,
        "invalidation_price": 9.7,
        "target_1": 11.5,
        "target_2": 12.2,
        "reward_risk": 2.1,
        "next_trigger": "回踩 10.10—10.40 后止跌确认",
        "suggested_position_pct": null,
        "suggested_shares": null,
        "position_message": "请先设置账户净值、可用现金和风险参数"
      },
      "positive_reasons": ["板块相对强", "回踩量能收缩"],
      "negative_reasons": ["距离前高较近"],
      "hard_blockers": [],
      "soft_blockers": [
        {
          "code": "WAIT_FOR_PULLBACK",
          "message": "尚未完成回踩确认",
          "recoverable": true
        }
      ],
      "data_status": "LIVE",
      "freshness": 0.92,
      "evidence_id": null
    }
  ],
  "holding_actions": [
    {
      "position_id": "pos-1",
      "symbol": "000001.SZ",
      "market": "A",
      "shares": 1000,
      "average_cost": 11.2,
      "last": 11.8,
      "pnl": 600.0,
      "pnl_pct": 5.36,
      "action_state": "WARNING",
      "action_label": "风险上升，密切观察",
      "reason": "价格接近结构失效位",
      "invalidation_price": 11.4,
      "distance_to_invalidation_pct": 3.39,
      "data_status": "LIVE"
    }
  ],
  "avoid_reasons": [
    {
      "code": "NO_CHASE_OVEREXTENDED",
      "message": "不追高位加速股"
    }
  ],
  "big_trend": {
    "status": "NOT_AVAILABLE",
    "message": "正式主升浪算法尚未启用",
    "items": []
  },
  "strategy_evidence": {
    "status": "INSUFFICIENT_REAL_EVIDENCE",
    "message": "当前只有工程合同和合成验证，不展示真实策略战绩"
  }
}
```

### Stage 1 降级规则

- 未配置账户时，仓位与股数为 `null`；
- 概率未校准时为 `null`；
- Big Trend 未实现时为 `NOT_AVAILABLE`；
- Strategy Scoreboard 未实现时为 `INSUFFICIENT_REAL_EVIDENCE`；
- 数据 STALE/UNKNOWN 时不能出现新的 `EXECUTABLE`。

---

## 3. GET `/api/portfolio`

```json
{
  "schema_version": "stage1-v1",
  "profile": {
    "account_equity": 500000.0,
    "available_cash": 120000.0,
    "risk_mode": "BALANCED",
    "per_trade_risk_pct": 0.005,
    "max_position_pct": 0.20,
    "max_portfolio_heat_pct": 0.06,
    "max_sector_pct": 0.35,
    "max_theme_pct": 0.35,
    "updated_at": "2026-08-14T09:00:00+08:00"
  },
  "positions": [
    {
      "id": "pos-1",
      "symbol": "000001.SZ",
      "market": "A",
      "shares": 1000,
      "average_cost": 11.2,
      "added_at": "2026-08-01T10:00:00+08:00",
      "closed_at": null
    }
  ]
}
```

未设置 profile 时：

```json
{
  "schema_version": "stage1-v1",
  "profile": null,
  "positions": []
}
```

---

## 4. PUT `/api/portfolio/profile`

请求：

```json
{
  "account_equity": 500000.0,
  "available_cash": 120000.0,
  "risk_mode": "BALANCED",
  "per_trade_risk_pct": 0.005,
  "max_position_pct": 0.20,
  "max_portfolio_heat_pct": 0.06,
  "max_sector_pct": 0.35,
  "max_theme_pct": 0.35
}
```

成功：HTTP 200，返回标准化 profile。

最低校验：

- 数字字段拒绝 bool；
- 拒绝 NaN/Inf；
- account equity > 0；
- available cash >= 0；
- 当前 cash-account 合同下 cash <= equity；
- 百分比在 `(0, 1]`；
- per-trade risk <= max portfolio heat；
- risk mode 只允许固定枚举。

---

## 5. POST `/api/portfolio/positions`

请求：

```json
{
  "symbol": "000001.SZ",
  "market": "A",
  "shares": 1000,
  "average_cost": 11.2,
  "added_at": "2026-08-01T10:00:00+08:00"
}
```

成功：HTTP 201，服务端生成稳定 position id。

最低校验：

- symbol 非空、使用规范大写形式且与 market 一致；
- shares 必须为正整数；现有持仓是账户事实，可因送股、部分成交或公司行为形成零碎股，不强制 100 股整数倍；
- A 股 100 股、港股每手股数和美股 1 股等交易单位约束，只用于新开仓建议与未来订单计划；
- 港股交易单位不得由 Portfolio API 猜测；
- average cost > 0；
- datetime 带时区；
- 同一未关闭 symbol 的重复持仓返回 409，除非后续明确支持分批 lot。

---

## 6. PATCH `/api/portfolio/positions/{id}`

允许字段：

```json
{
  "shares": 1200,
  "average_cost": 11.35
}
```

禁止任意字段透传。不存在返回 404，冲突返回 409，校验失败返回 400。

---

## 7. DELETE `/api/portfolio/positions/{id}`

Stage 1 语义：删除当前持仓记录，不表示自动卖出，不生成真实成交结果。

成功：HTTP 200。

```json
{
  "ok": true,
  "position_id": "pos-1"
}
```

---

## 8. 错误合同

```json
{
  "error": {
    "code": "INVALID_NUMBER",
    "message": "account_equity must be a finite number",
    "field": "account_equity"
  }
}
```

建议状态码：

- 400：格式或字段校验；
- 401：已启用私有访问但认证缺失或不匹配；
- 404：资源不存在；
- 409：身份/重复冲突；
- 413：JSON 请求体超过 64 KiB；
- 500：未处理内部错误，同时服务端完整记录日志；
- 503：公网请求私有 API，但服务端尚未配置私有访问值。

不得把内部堆栈、数据库路径、凭据或原始上游响应直接返回给前端。
