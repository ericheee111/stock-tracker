# 多周期产品 P1–P3：可用的手工计划切片

2026-09-11。基线 c0c39977f4460cc9102fb3731b35cbf88786fc1d。设计已按用户明确画像冻结；本文件中的完成状态由本轮验证报告更新。波段数周至数月，长持/短线兼容；持仓和机会并重。

## 交付范围

P1：兼容现有 Position 的虚拟用途分配、核心保留数量、计划依据/失效/复核日期。SWING / LONG_TERM / SHORT_TERM 是用途；未分配余量显示 UNCLASSIFIED，不复制真实持仓。

P2：独立的 MANUAL_UNVERIFIED 计划库。显式 init 才创建；普通应用启动只打开已初始化文件。支持人工可卖量/外部预留/币种现金确认、T 两方向条件预演、内部额度预留、未执行取消、已转实际操作标记及人工对账关闭。API 继承私有认证、严格 JSON、remote-write audit；不改生产 Portfolio DB，不接券商、不创建委托。

P3：今日页并列持仓/机会，两边摘要始终可达；增加持仓计划工作区，支持上述编辑及手工情景的相对持有测算。不是算法买卖建议，也不生成真实胜率。原机会排序、风控阈值、Signal 状态机和核心仓退出逻辑均不改变。

## 账户与数量边界

初版是单一使用者、每币种一个 LOCAL 资金池，不支持多个券商账户合并/外汇兑换。A/CNY、HK/HKD、US/USD 仅用于本项目现有普通股票范围；不支持多币种证券、衍生品、融资、借券或碎股。T 的整手大小和规则说明由用户当次确认，不宣称自动完成证券规则核验。

计划关联现有 Position 的完整身份/数量/成本快照。父 Position 被改动或删除时，旧计划配置需重新核对，不覆盖父 Position。两个数据库不宣称跨库事务；条件预演不是执行授权，实际操作前仍须查看券商可卖量及现金。

每一用途分组的 core_quantity <= quantity，三组 quantity 之和 <= 真实 shares；T 可用的机动量 = quantity-core_quantity-该组所有未关闭计划数量。所有用途共享同一可卖旧仓和现金：

- sellable_gross 是人工确认的总可卖旧量（未扣外部未成交委托）；external_reserved_sell 必须单列。
- remaining_sellable = sellable_gross-external_reserved_sell-内部所有未关闭计划预留。
- 任意方向都预留 q 股可卖旧量；当日新买量不会增加可卖额度。
- 任意方向都预留 q*buy_limit + total_fee_buffer 的现金；不预支预计卖出款。这是保守规划规则，不是券商制度说明。
- BUY_THEN_SELL additionally 检查 shares + 所有同股 BUY_THEN_SELL 未关闭数量 <= 人工指定 maximum_position_quantity，显示峰值；SELL_THEN_BUY 显示买回缺口风险。
- 所有金额是有界十进制字符串、内部 Decimal，币种隔离；未知不补0。
- 手工库存/现金有效期最多15分钟（工程时效策略 v1，不是行情承诺）；expired 计划仍预留额度，不能因为时钟过期假设外部委托不存在。

## 命令与恢复

一份命令须携 command_id + expected_revision，SQLite BEGIN IMMEDIATE 内重放并验证，然后追加一次。相同命令重试幂等；同 ID 不同内容409；旧 revision409。每个历史命令记录应用时观察到的父 Position（不是调用方自报身份）。全局版本控制同时限制多页面抢占同一额度。

CANCEL 仅适用于 RESERVED，明确确认没有执行/未成交委托。MARK_EXECUTED 只表示“已转实际操作，等待对账”，继续保留预留，不产生 fill。RECONCILE 必须列出该币种全部未关闭计划，确认外部委托清空、账户已核对，记录原因；关闭后清空该币种的现金和关联库存确认，必须重新录入，避免释放旧额度后立即复用旧快照。

不修改/删除历史命令，无自动过期释放，无自动unblock。记录仅为内部手工计划日志，不是独立签名或 Broker Execution Evidence。对账关闭不是 Outcome COMPLETE。

## 做 T 相对持有测算

仅接受人工情景输入；假定同证券、同币种、相同起点/估值时点、无额外资金流和公司行为。费用包括用户填写的全部成本。

cash_delta = sell_quantity*average_sell - buy_quantity*average_buy - fees
relative_hold_delta = cash_delta + (buy_quantity-sell_quantity)*mark_price

显示未配对数量、剩余持仓变化；空腿保留为0数量且价格null，不用0价格表示未知。卖出数量不得超过输入的可卖旧仓；不将测算列入胜率、真实绩效、校准或4F/4H证据。

## 持久化与保护

新包 portfolio_planning 使用自己的小规模 SQLite 命令日志（最多5000条/128持仓/1000计划，满额显式报错，后续导出版本另立）；不是替代 B1 高频Store。每次读取重放历史，核对严格schema、hash chain、store identity；显式临时同目录初始化、no-overwrite publication；生产 stock_tracker.db、链接和未知schema一律拒绝。所有测试只用临时库。

## 验收与审查

红绿测试覆盖：跨用途超分配、跨计划超卖/超现金、两线程旧版本竞争、重复请求、币种混用、父Position变化、过期不释放、未执行取消/实际操作后的对账、零/缺失/NaN/bool/字符串边界、Decimal context、日志篡改/替换、未初始化不写库、私有API/CORS/严格JSON、双主线可达、真实临时API/浏览器、测算未完成腿与费用。

最终以精确提交进行独立只读 Codex review，思考强度 High（当前 CLI/模型支持时）；该实例不参与实现、不写代码、不提交。审查发现由本对话修复并补测。完整 Runtime/Quant、分发、类型、Ruff、编译、产品集成通过才普通快进push。保护原main/旧Agent工作区及生产DB/WAL/SHM。

## 后续阶段（本轮不冒充完成）

P4：版本化多周期策略对照与reachability审计（触发价冻结、持有/观察/标签周期分开），无默认权重晋级。
P5：真实手工 fill ledger、部分成交、公司行为/现金流与完整相对持有归因；与B2/B3证据桥接分开审查。
B1d→B2→B3：真实语义ReadPort、路径/覆盖与Collection；多交易日Shadow及4H/4I门禁保持。
