---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-15
activation_state: production-v6-enabled-v5-retired-no-fallback
supersedes:
  - ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md
implementation_symbols:
  - physical_state_bayes_v6/contracts.py
  - physical_state_bayes_v6/core.py
  - production_probability_v6_event_runtime.py
  - production_probability_v6_weak_revision_runtime.py
  - production_probability_v6_public_ssot_writer.py
  - production_probability_v6_weak_revision_writer.py
verification_gates:
  - python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  - python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation config/production_probability_runtime_activation.json --event-date ET_TODAY
  - python3 scripts/probability_document_authority_audit.py --summary
last_verified_commit: working-tree-2026-07-16-v6-production-cutover
---

# ADR-0003：Production Successor v6 报文前状态预测 Bayes

## 决策

批准并启用 `production v6` 作为当前唯一生产概率纪元。v6 不是长期并行 shadow，也不是 v5 函数重排：它先在当前报文到达前推进连续物理最高温与八状态的联合分布，再用当前 canonical 报文做一次联合观测更新。

```text
P0 weak-conditioned joint P(Y,Z)
→ pre-event controlled Predict
→ current canonical observation emission
→ regular-METAR hard bound
→ normalize
→ update post-event clocks/history
→ reported-X projection
```

2026-07-16 受管切换已完成，现行 activation schema 为 `production_probability_v6_activation_v1`。ADR-0002 与 reported-X v5 已退役；v5 runtime 不得进入 v6 manifest、不得写 production latest、不得作为故障 fallback。任何身份或运行时故障都必须 fail closed。

唯一 composer 身份固定为 `physical_state_bayes_v6_pre_event_predict_update`；公共 checkpoint 与 SSOT schema 分别固定为 `posterior_checkpoint_v4_physical_state_bayes_v6` 和 `sequential_temperature_posterior_ssot_v4_reported_x`。

## 目标与状态

```text
Y = 内部连续的 AMSC 计划常规 METAR 报文日最高温潜变量
X = 由冻结整数报文算子 report(Y) 投影出的公共 reported-X
Z = 八状态：
    warming / slowing_warming / temporary_platform / temporary_cooling /
    cooling_then_rebound / weather_suppressed / terminal_cooling / final_locked
```

公共 SSOT 只发布 `P(X)`。`P(Y,Z)`、持续时钟和情景 responsibility 是内部 checkpoint，不形成第二个概率 SSOT。

## 唯一公式

### Predict

```math
\pi_t^-(Y,z')=\sum_z\pi_{t-1}(Y,z)
A_\theta(z'\mid z,H_{t-1}(Y),U_{k,t}(Y),C_k,D_t^-,\Delta t)
```

```math
H_{t-1}(Y)=\max(0,Y-M_{t-1})
```

`M_{t-1}`只能来自当前事件之前已经接受的常规 METAR 历史。`A` 对每个 `(Y,z)` 逐行归一，因此 Predict 只推进 `P(Z|Y)`，不直接改变 `P(Y)`：

```math
\sum_{z'}\pi_t^-(Y,z')=\sum_z\pi_{t-1}(Y,z)
```

第一阶段不引入 clock-only survival/termination hazard。没有新 canonical 事件时，单靠时间流逝不修改 `P(Y)`；未来若增加 clock event，必须另立 artifact、校准与 ADR。

其中 `k` 是当前 checkpoint 绑定的 weak forecast snapshot。`C_k` 的唯一规范名称是
`forecast_background_context`，不得写成或复用 strong observation 中的
`physical.weather_regime`。`C_k` 属于 weak snapshot，不属于 P0 永久常量：TAF AMD/COR
或其他预测修订只能生成新的 snapshot，并从下一次事件 Predict 起生效，绝不回写已完成状态转移。

`forecast_background_context` 的最小合同是各预测天气情景的分布向量、来源贡献、缺源组合、
snapshot SHA 与生效时间。来源分歧必须保留为向量；不得使用“一旦分歧就
mixed 并把全部偏置归零”的硬标签逻辑。`unknown` 表示完全中性，不得伪造天气背景。

当前批准的 v6 production transition 通过同一 weak snapshot 中的候选条件化情景路径，直接消费
未来温度斜率、剩余增量/降幅、峰值距离、降水、云抑制、CB、feature availability 与真实
`Δt`。这些量已经是 `forecast_background_context` 的更细物理展开。不得再从同一预测源派生一份
固定 `regime bias` 叠加进去。若小时强迫缺失而拟用 regime vector 补层，必须以新的 transition
artifact 明确 feature-availability 互斥门、完成离线消融/OOS/校准后再晋级；不能凭手工 `κ` 或
“数值很小”进入 production。

### Update

```math
\pi_t(Y,z')\propto\pi_t^-(Y,z')
\exp\{\ell_t(O_t\mid Y,z',D_t^-)\}
\mathbf 1[Y\ge LB_t]
```

`LB_t` 是连续 `Y` 的编码区间下界，不是直接复制报文整数。对当前冻结的正摄氏度 half-up 报告算子，常规 METAR 报 `k℃` 时：公共 `reported-X` 硬下界为 `k`，连续 `Y` 下界为 `k-0.5℃`。两者必须在 checkpoint 分列保存；SPECI 两者都不抬升。

当前事件只在联合 emission 中消费一次。归一化后不再执行第二次状态转移；只允许生成 `D_t^+=f(D_t^-,O_t,\Delta t)` 与 post-event history，供下一条事件 Predict 使用。

持续时钟必须同时保存 `duration + active flag`。Predict 只把上一事件已经 active 的时钟按 `Δt` 推进到 `D_t^-`；当前事件第一次报告雨、低云或 CB 时，`D_t^+` 从 0 启动，不得把整个报文间隔倒填为已持续时间。当前事件确认上一 active 状态时才继承 Predict 后的持续时长，当前事件否定时立即归零。

### reported-X 投影

```math
P_t(X=k)=\sum_z\int_{report(Y)=k}\pi_t(Y,z)dY
```

离散实现必须绑定冻结网格权重与报告算子，不得把连续格点直接标成公共 `X`。

## 四条证据时序硬边界

1. **当前报文不得进入 Predict。** Predict 只读上一 checkpoint、`O_<t`、冻结未来强迫、`D_t^-` 与 `Δt`。当前温度、云、雨、CB、露点、风压只进入本次 emission。
2. **NWP 不得形成第二份似然。** 最高温/情景能力只在 P0 或 weak revision 消费；小时轨迹只作为冻结的 `U_t(Y)` 控制状态转移，不在每条报文再次乘 LR。
3. **TAF AMD 只修订未来。** AMD 替换受影响的弱预测窗口并生成新的 future-forcing generation；不得回写已完成转移，不得伪装成观测。
4. **v5 post-transition 在 v6 切换时退役。** v6 内部禁止 `emission → normalize → event-conditioned state transition`；两种时序不能并存。

## 多峰与候选条件化强迫

禁止把多个预测源的小时轨迹平均成一条中间曲线。P0 冻结情景曲线及每个 `Y` 的 responsibility：

```math
U_t(Y)=\sum_s P(s\mid Y,F)U_{s,t}
```

每个 `Y` 的转移核由其情景 responsibility 混合；posterior 仍只保存 `Y×8`，不增加情景维。缺源降低信息量与仓位能力，但不阻断 AMSC canonical 入库或已经批准的生产 v5。

状态转移按连续时间 hazard 计算：保持概率随 `Δt` 衰减，而不是每条报文固定乘一次常数
`ρ`。因此同一 30 分钟物理区间出现 1 条 METAR 或额外出现多条 SPECI，不得仅因事件条数
不同而重复强化预测背景。SPECI 只增加新的观测 emission 与实际经过时间，不增加一份预测似然。

## 明确拒绝的最小补丁

以下方案不是 v6 的替代品，也不得作为长期生产分叉：

```text
current strong LR → normalize → forecast-regime-biased post transition
```

它只改变 `P(Z|Y)`，不能在当前事件直接改变 `P(Y)`，也缺少候选剩余可达性和报文间状态推进。
若需要评估 regime 是否提供小时曲线之外的增量信息，只允许离线 paired replay/消融；不得建立
第二个 live latest，不得复活 v5 post-transition，也不得用 shadow 结果触发订单。

## 边界与非目标

v6 不替代逐源偏差校正、缺源组合校准、`X→S→B` bridge、市场桶校准、CLOB 签名与网络链路，也不会自动产生安全 NO。训练 strict-as-of、OOS 和样本量只控制 v6 artifact 晋级，不得成为 live v5 或 AMSC 入库阻断。

## 生产切换与退役

v6 必须依次完成：

- 新 schema、artifact manifest 与内容寻址；
- 全 canonical replay 与增量/全量等价；
- v5/v6 同事件、同 as-of、同 hard bound 的离线切换验收；不得建立长期双 live latest；
- 概率守恒、无自确认、无 NWP 重复似然、多峰保真测试；
- 最高温 MAE/CRPS/NLL、正确反应领先时间及 p95/p99 延迟验收；
- 独立零订单演练、新 replay generation 和 writer-quiescent 受管切换。

只有新 activation 原子生效并通过 live acceptance 后，v5 composer、post-state transition artifact 字段和旧 cutover 才进入明确退役清单。维护事务在 v6 生效前失败时恢复完整切换前 v5 状态；v6 生效后的运行期失败只允许 fail closed，不得 fallback 到 v5。
