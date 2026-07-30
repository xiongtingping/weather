---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-19
---

# Basket + Hard NO 生产策略

## 生产身份

生产唯一执行服务仍是 `com.weather.reported_trajectory_no`，内部包含两条互相独立的执行腿：

1. `weather_basket_plus_hard_no_v2`：读取已物化的 P0/P1 市场桶概率、已物化 regime 和官方 CLOB WebSocket 盘口，按后验覆盖率从 4→3→2 连续精确温度 YES 分阶段等份 FOK；每阶段严格要求所有 YES ask 加总小于 1，持有至结算；新 checkpoint/盘口签名且资金未耗尽时允许只增不减的窄阶段补仓。
2. `reported_trajectory_no_v1`：只读取 AMSC/AWC 官方 METAR/SPECI，买入最近一个已数学排除桶的 NO，FAK ≤ 0.99，沿用原 GTC 0.99 退出。

篮子不做选择性配平、合并或提前退出；4→3→2 是后验收敛阶段，不是按价格分层加仓；硬 NO 不读取概率或模型。两者共用本机唯一 `clob_order_service` gateway。三腿、Target Realization 和旧 take-profit resident 已持久禁用。

盘口口径：篮子没有固定“四腿盘口”要求。每次只检查当前收敛阶段的全部腿，阶段宽度为 4、3 或 2；三腿阶段就是三腿逐腿 FOK，后验/盘口变化只允许推进到下一阶段或在同阶段做只增不减补仓。

## 运行边界

- 篮子证据缺失、盘口过期、连续桶不足、`sum(YES ask) >= 1` 或任意待处理 cycle 未完成时，篮子 fail closed。
- 篮子逐腿 FOK；任一腿失败后 cycle 进入 `reconcile_required`，停止后续篮子，不自动补单。
- 切换过程不下单，不清理既有仓位；既有仓位按交易所结算与现有退出账本处理。
- SSOT：`basket_strategy_activation_state`、`basket_strategy_cycles`、`basket_strategy_legs`；执行阻断仍回写 `auto_trade_decisions`。

## 受控切换

```bash
cd /Users/xiong/weather
python3 scripts/cutover_basket_plus_hard_no_production.py --confirm-live-basket-plus-hard-no
```

该命令只切换激活状态、禁用三腿 user-domain 入口并重启唯一 reported-trajectory system service；不会在切换事务中提交交易单。
