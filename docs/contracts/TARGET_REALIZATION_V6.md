---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
activation_state: production-active-content-addressed-generation-2
runtime_conformance_state: implemented-deployed-and-production-accepted
successor_to: TARGET_REALIZATION_V5.md
implementation_symbols:
  - target_realization_v6_runtime.build_target_realization_v6_plan
  - dynamic_three_leg_v6_position_cycle_runtime.advance_main_yes_position_cycle
  - dynamic_three_leg_v6_capital_allocator.allocate_v6_incremental_capital
  - production_dynamic_three_leg_v6_transaction_c_writer.commit_target_realization_v6_transaction_c
  - production_v6_standing_exit_writer.commit_v6_buy_fill_standing_exit
  - production_v6_standing_release_coordinator.process_v6_standing_release_once
  - production_v6_target_sell_continuation.process_v6_target_sell_continuation_once
  - resident_target_realization_v6_intent_submitter.submit_prepared_target_realization_v6_intent
verification_gates:
  - python3 scripts/target_realization_v6_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v6-successor
---

# Target Realization v6：动态三腿、主 YES 风险退出与双模式 NO

## 0. 权威与切换边界

本文是当前 production 的 L1 交易合同。真实订单授权只来自 `config/trading/target_realization_v6_production_activation.json` 绑定的内容寻址 artifacts；文档本身不单独授权订单。Target Realization v5、0.90 主 YES 与旧 hard-NO 100 已 superseded，禁止 fallback 或混跑。

v6 受管切换必须在同一线性化点完成：安装新 strategy/cutover artifact、schema、composer、position-cycle、standing-exit、精确 reason allowlist 和 activation；随后 v5 立即 superseded 且禁止 fallback。切换前任何 v6 intent 必须 fail closed。

## 1. 宪章与边界

- 后验 `p(B)` 是交易资格唯一概率 SSOT；交易层不得重读 METAR/SPECI、预测源、动量、轨迹、云雨、露点或 Termination Risk。
- 三个经济槽位固定为 `main_yes`、`second_yes`、一个互斥 NO 槽位；NO 槽位只能是 `trajectory_no` 或 `hard_exclusion_no`。
- 角色随每个 committed posterior 动态重选，历史 lot 只保留成交血缘，不冻结桶排名。
- 入场、金额、退出不得使用 EV、Edge、LCB、fair value、买入价格上限、repricing、stop-loss 或 trail-stop。
- 市场数据只证明价格/tick/深度/余额/allowance/幂等可执行性，不决定天气资格或概率金额。
- 所有实际 BUY fill 默认建立 0.999 GTC standing exit；只有本合同明确批准的主 YES target-realization SELL 可以先释放对应 coverage 后执行。

## 2. 三腿与资本上限

| 槽位 | 资格 | 原始目标上限 |
|---|---|---:|
| `main_yes` | latest posterior 第一桶、当前桶未被 hard exclude | 20.00 USDC |
| `second_yes` | latest posterior 第二个不同可达桶，且新买处于峰前最后 30 分钟 | 严格低于 5；执行上限 4.99 USDC |
| `trajectory_no` | 独立桶 `q_NO=1-p_yes` 达到批准分档并与 soft-unreachable 投影一致 | 100.00 USDC |
| `hard_exclusion_no` | exact checkpoint/bridge/target hard certificate | 200.00 USDC |

`trajectory_no` 与 `hard_exclusion_no` 是同一 NO 槽位的互斥模式，不得形成第四腿。组合绝对上限分别为 124.99 与 224.99 USDC。

所有金额都是投入成本上限，不是必须足额执行的固定订单。先从原始目标扣除同槽位所有 current/off-role/pending exposure，得到每腿 `raw_incremental_target_notional`。若合格新增 BUY 总额超过冻结可分配资本，唯一资本分配器对同一计划使用统一比例：

\[
\alpha=\min\left(1,\frac{C_{available}}{\sum_i \Delta N_i^{raw}}\right),
\qquad
\Delta N_i^{final}=\alpha\Delta N_i^{raw}
\]

其中 `0<=capital_scale_factor=alpha<=1`。它只缩放本轮新增 BUY，不强卖已有仓位；不得按执行顺序先到先得。hard NO 的 200 USDC 只是绝对上限，允许低于 200、允许按统一资本比例缩量，不要求足额，也不得因不足 200 单独阻断该腿。比例只由冻结资本/风险可用量决定，不能读取价格收益、EV、Edge 或 LCB。

## 3. 主 YES 四态控制器

定义：

\[
P_{above\_top}=\sum_{B_j>B_{top}}P(B_j)
\]

它必须由同一 committed market-bucket posterior 求和，不能在交易层重建天气概率。每个 checkpoint 只允许一个 `main_yes_mode`，优先级固定：

```text
invalidated > hard_locked > upside_risk > normal
```

| 模式 | 定义 | 动作 |
|---|---|---|
| `normal` | 当前主桶有效、未 hard lock，且 `P_above_top<10%` | 不新增阶段 SELL；保留默认 0.999 |
| `upside_risk` | 至少一个更高桶未 hard exclude，且 `P_above_top>=10%` | 按风险目标与时段上限较小值累计 SELL |
| `hard_locked` | 当前主桶有效，且其上全部市场桶均有 exact hard certificate | 停止阶段 SELL，剩余仓位保留 0.999 或自然结算 |
| `invalidated` | 当前主桶自身 hard exclude，或不再属于当前 YES 角色 | target-realization 退出或按次 YES 上限重分类 |

“更高桶全部 hard exclude”锁定当前主 YES；“当前主桶自身 hard exclude”使其失效。低概率、soft unreachable 或有限核截断不得伪造任一 hard certificate。

### 3.1 风险目标与时段上限

| `P_above_top` | `risk_target_ratio` |
|---:|---:|
| `<10%` | 0% |
| `10%–<20%` | 25% |
| `20%–<35%` | 50% |
| `35%–<50%` | 75% |
| `>=50%` | 100% |

| `peak_phase` | `time_cap_ratio` |
|---|---:|
| 距 `entry_peak_at` 超过 60 分钟 | 25% |
| 距 `entry_peak_at` 30–60 分钟 | 50% |
| 峰值前最后 30 分钟 | 75% |
| 已到峰值且后验仍有继续升温/反弹风险 | 100% |

唯一累计目标为：

\[
main\_yes\_stage\_target\_ratio
=\min(risk\_target\_ratio,time\_cap\_ratio)
\]

时间只限制最大退出速度，不能在无后验风险时强制卖出；高风险也不能越过当前时段上限。实际新增卖量只等于本持仓周期目标累计量减已成交累计量的正差额。

### 3.2 持仓周期与再入场

每个主 YES 暴露周期必须有 `main_yes_position_cycle_id`；`sell_stage_high_watermark` 只在同一周期内单调，防止半点 METAR、整点 METAR 或 SPECI 对同一阶段重复 SELL。它不是“同桶当日永久禁买”。

已执行阶段 SELL 后，只有完成旧 SELL/0.999 coverage reconcile，并满足下列任一条件，才可创建新周期、重置新周期水位并按 latest posterior 再入场：

1. exact `hard_locked` 已成立；或
2. 连续两条新的 canonical 强观测均满足 `P_above_top<10%`，当前桶未失效，期间没有 invalidation。

continuation、行情刷新、订单回执或重复处理同一 observation 不得计入两次确认。

### 3.3 动态角色重分类

原主 YES 降为当前次 YES 时，已有仓位可在峰前 30 分钟窗口外保留最多 4.99 USDC；这是 HOLD/reclassification，不是新 BUY。超过部分使用 `target_realization_main_yes_invalidated`；窗口外禁止对次 YES 新买或补仓。原次 YES 升为主 YES 时，必须按最新主 YES 四态、当前周期与全组合资本重新取得资格。

## 4. 次 YES

新买窗口固定为 `[entry_peak_at-30m,entry_peak_at)`。`entry_peak_at` 缺失或窗口身份不闭合时，只关闭次 YES 新买/补仓，不阻断主 YES、NO、AMSC 入库或 posterior 更新。

次 YES 实际成交后同样挂 0.999。若到市场关闭仍未成交，才自然进入结算；不得另建“必须持有到结算”的执行分叉。

## 5. Trajectory NO

每个二元桶独立定义：

\[
q_{NO}(B_i)=1-P(B_i)
\]

不同桶的 NO 事件重叠，禁止跨桶归一化。资格和金额只读同一 committed `p(B)`：

| `q_NO` | 原始目标上限 |
|---:|---:|
| `<70%` | 0 USDC |
| `70%–<80%` | 10 USDC |
| `80%–<90%` | 25 USDC |
| `90%–<95%` | 50 USDC |
| `95%–<98%` | 75 USDC |
| `>=98%` | 100 USDC |

升档使用表中阈值，降档使用 1 个百分点滞回。NO 后验越高金额越高、越低金额越低；金额不按价格、EV 或 Edge 缩放。`soft_unreachable=true` 但 `q_NO<70%` 表示 SSOT 投影不一致，本轮该 NO 腿 fail closed。

最多选择一个 trajectory NO：先排除主/次 YES、同桶相反仓位和身份漂移，再按 `q_NO` 降序、soft-unreachable、物理可达性分数与稳定 bucket id 仲裁。

## 6. Hard-exclusion NO

hard certificate 必须绑定 `posterior_run_id/realization_state_id`、canonical event、物理 hard support、`X→S→B` bridge、bucket contract 与 authority/artifact SHA，并逐组合证明最终结算不落入目标桶。低 posterior、soft unreachable、核外截断或单一物理字段都不是 hard certificate。

多个 hard-excluded 桶也只能选一个 NO 槽位。hard 模式上限为 200 USDC，但允许统一资本比例缩量；证书决定资格，资本只决定 `<=200` 的实际金额。轨迹模式升级为同桶 hard 模式时只补正差额；不同桶旧 NO 暴露未退出前不得建立第二个 NO 桶。

## 7. 0.999 Standing Exit 与精确退出闭集

每笔实际 BUY fill 默认按实际成交数量建立：

```text
side=SELL
order_type=GTC limit
limit_price=0.999 USDC
post_only=false
```

若 token tick 不支持精确 0.999，本腿 fail closed，禁止改成 0.99。partial fill 只覆盖新增成交量；任一 token 数量只能被一个 child SELL 覆盖。ACK/cancel unknown 只按原 identity reconcile，禁止替代单或重叠卖量。

主 YES staged/invalidation SELL 是唯一例外：先释放对应 0.999 coverage，证明 cancel/remaining size 后，才按 frozen fresh executable bid 创建不可追价的 target-realization SELL；其余数量继续保留 0.999。

批准的 `market_alpha_take_profit` code 只有：

```text
market_alpha_take_profit_main_yes_999
market_alpha_take_profit_second_yes_999
market_alpha_take_profit_trajectory_no_999
market_alpha_take_profit_hard_exclusion_no_999
```

批准的主 YES `target_realization` code 只有：

```text
target_realization_main_yes_upside_risk_stage_1
target_realization_main_yes_upside_risk_stage_2
target_realization_main_yes_upside_risk_stage_3
target_realization_main_yes_upside_risk_stage_4
target_realization_main_yes_invalidated
```

旧 `market_alpha_take_profit_top_yes_wave_completed`、0.90、0.98 NO 价格门、泛化前缀、EV/Edge/LCB、repricing、stop-loss 和 trail-stop 不属于 v6 闭集。

## 8. 原子组合与 SSOT

每个 posterior 的 Transaction C 必须一次生成完整组合：

```text
latest posterior + RealizationState
+ frozen market/capital/risk/position/coverage snapshots
+ accepted v6 strategy artifact
-> role and NO-mode arbitration
-> main-YES state/cycle transition
-> inventory/conflict reconciliation
-> proportional capital allocation
-> decisions + reservations + intents + outbox commit
```

所有阻断只写 `auto_trade_decisions`。任一 unresolved POST、ACK unknown、lot/version drift、coverage overlap、同桶 YES/NO、第二个 NO 桶或 stale snapshot 都先 reconcile/fail closed。网络 POST 不得伪装成本地数据库原子事务。

最小 lineage 必须包含：

```text
posterior_run_id / realization_state_id / strategy_artifact_sha256
strategy_leg / no_mode / bucket / side / p_yes / q_no / soft_unreachable
main_yes_mode / p_above_top / risk_target_ratio / time_cap_ratio / peak_phase
main_yes_position_cycle_id / reentry_confirmation_count / sell_stage_high_watermark
hard_exclusion_certificate_id / hard_exclusion_set_id
raw_incremental_target_notional / capital_scale_factor / final_incremental_target_notional
position_lot_version / standing_exit_plan_id / covered_size / stage_released_size
capital_snapshot_id / risk_reservation_id / decision_state / block_reason
```

UI 只读这些 committed 字段，不得自行计算候选、状态、金额或退出原因。

## 9. Activation Checklist

- [x] L0 ADR-0006、本文、资本策略和 reason allowlist 的内容哈希闭合。
- [x] 四态主 YES、风险目标×时段上限、position-cycle 与连续 canonical 再入场测试通过。
- [x] trajectory NO 分档、1pp 滞回、hard certificate、唯一 NO 槽位与 200 上限缩量测试通过。
- [x] 唯一执行 owner 固定为 `com.weather.dynamic_three_leg_execution`；旧 `auto_trade_entry_1m`、`trade_engine`、`auto_trade_worker`、`trade_sync_loop`、`target_realization_worker` 持久退役并纳入 readiness 硬检查。
- [x] 切换后独立只读 acceptance 同时验证：active release 内容绑定、v5 两份 activation 已 supersede、schema 完整、安装 plist 与发布源逐字节一致、唯一 resident 正在运行、旧服务与遗留进程为零、CLOB cancel/lookup 能力就绪、pause 线性化点已跨越。
- [x] CLOB gateway 在 pause 期间保持可审计健康，但 `/place-order` 必须在任何幂等写、签名或 exchange POST 前逐请求读取 pause 并拒绝；删除 pause 才令 `order_submission_enabled=true`。
- [x] strategy artifact 内嵌并自校验批准时的不可变 L0/L1/AGENTS 文档快照；生产 loader 不把后续纯部署状态文档更新误判为策略语义漂移，当前运行源码仍由完整 runtime closure 严格验哈希。
- [x] 所有 fill 的 0.999 coverage、tick fail-closed、partial fill、cancel/ACK unknown 与 target-realization coverage release 测试通过。
- [x] 同一 Transaction C 的统一 `capital_scale_factor`、旧仓占额、同桶反向和多 NO 冲突测试通过。
- [x] 旧 0.90/v5 reason code、EV/Edge/LCB 和 retired NO 路径在 successor runtime 中为零。
- [x] strategy/cutover artifact、schema、runtime SHA、双 activation 与 pause 线性化点完成受管切换。
- [x] v5 明确 superseded、无 fallback；generation 2 acceptance 证明唯一 v6 resident 与重复 cutover no-op。

当前 production generation 2 已启用：composer、position-cycle、standing coverage、统一资本比例、精确退出闭集、Transaction C、撤单 reconcile、fresh-quote target continuation 与 generation-bound submitter 均由唯一 resident 执行；隔离零订单演练、故障回滚、切换 acceptance 和重复 runbook no-op 已通过。历史样本、strict-as-of 训练覆盖和 OOS 不作为已批准 live 基线的阻断；它们只约束后续参数/模型晋级。
