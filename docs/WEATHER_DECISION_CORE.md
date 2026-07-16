---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-15
supersedes: [legacy-weather-decision-core]
implementation_symbols:
  - scan_weather_market.py:_entry_order_policy_block_reason
  - scan_weather_market.py:_exit_order_policy_block_reason
  - auto_trade_decision_writer.py
verification_gates:
  - python3 scripts/weather_decision_core_audit.py
last_verified_commit: working-tree-2026-07-15
---

# Weather Decision Core

本文件只定义决策边界，概率与架构分别以 [系统宪章](constitution/SYSTEM_CHARTER.md) 和 [当前架构](architecture/CURRENT_ARCHITECTURE.md) 为准。

## 唯一入场口径

- posterior settlement；
- posterior SSOT target realization；
- hard-source direct SSOT target。

候选必须来自 reported-X v5 activation，携带 `posterior_checkpoint_id/posterior_run_id`、activation/runtime-manifest 身份、同一 committed RealizationState、`decision_ssot` 和 freshness 合同。缺少任一身份，或 market bridge 尚未提供本次 B projection，即阻断交易但不阻断物理 X。

## 唯一退出口径

目标合同中的真实卖单只允许两个互斥原因族；执行器必须验证 `exit_reason_family + exit_reason_code` 闭集，不得只匹配前缀：

- `target_realization_*`：posterior、hard condition 或 settlement 改变天气/结算目标；
- `market_alpha_take_profit_*`：latest posterior 当前最高概率 YES 的 top-entry lot 达到批准策略 artifact 固定的 0.90 可执行目标；桶排名不冻结。

`market_alpha_take_profit_*` 不是天气目标失效，必须携带独立 `exit_reason_family`，当前唯一批准语义是 `market_alpha_take_profit_top_yes_wave_completed`。

`target_realization` 的精确允许 code 以 [Target Realization v3 合同](contracts/TARGET_REALIZATION_V3.md) 为唯一来源。未知 code、`target_realization_market_alpha_*`、改名 repricing、缺失 family 或同时携带两个 family 必须 fail closed。

Edge、LCB、exec_edge、signal_edge、动态 repricing、stop-loss 和 trail-stop 只允许展示、诊断或历史复盘，不能触发或阻断上述两个原因族。生产 runtime 不恢复旧 EV/Edge 硬门。

market-alpha runtime 与 schema 已就绪但尚未 live；在 current-posterior role、lot/version、artifact、fresh quote、partial-fill/reconcile、T1→T5 与独立 activation 全部闭合前必须 fail closed。旧 wave cycle、frozen bucket target 与泛化 `market_alpha_cycles` 永久退役。

当前 `production_trade_gate_zero_audit.py` 与精确 reason-family 负向测试证明 runtime 不使用 EV/Edge 类硬门或退出前缀通配；`trade_strategy_loop_audit.py` 进一步区分安全 `ok` 与真实 `live_ready`。bridge、accepted strategy activation 或 gateway-attested T1→T5 缺失时保持 safe fail-closed，不能据此声称下单已授权。

## 执行边界

决策先于网络提交；CLOB intent 必须 durable；未知结果只按确定性 `expected_order_id` reconcile。UI、snapshot 文件和旧信号文件不能成为 Live 执行输入。

## 文档边界

旧开发计划、迁移计划、shadow 计划和旧数据源宪章已删除。不得从 Git 历史复制旧概率公式、旧 fallback 或旧执行门槛回当前代码；任何新方向先提交 ADR。
