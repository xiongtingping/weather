---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
deployment_state: production-v6-active-v5-retired-no-fallback
supersedes: [reported-x-v5-current-architecture, legacy-v2-v3-fallback-chain, joint-weak-physical-strong-v1-current-claim]
implementation_symbols:
  - config/production_probability_runtime_activation.json
  - production_probability_v6_amsc_archive_ingress.py
  - production_probability_v6_event_runtime.py
  - production_probability_v6_weak_revision_event_runtime.py
  - production_probability_v6_public_ssot_writer.py
  - production_probability_v6_runtime_manifest.py
  - docs/adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md
  - docs/contracts/TARGET_REALIZATION_V5.md
  - docs/adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md
  - docs/contracts/TARGET_REALIZATION_V6.md
verification_gates:
  - python3 scripts/probability_document_authority_audit.py --summary
  - python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  - python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation config/production_probability_runtime_activation.json --event-date ET_TODAY
  - python3 scripts/production_chain_uniqueness_audit.py --summary
last_verified_commit: working-tree-2026-07-16-target-realization-v6-generation-2-current
---

# 当前架构

## 唯一生产概率主线

```text
预测源当时版本 ──► P0(prior × joint weak，一次) ──► joint P(Y,Z)
AMSC raw archive ─► SourceObservation ─► CanonicalObservationEvent

上一 checkpoint + O_<t + 冻结候选条件化未来强迫 + D_t^- + Δt
  ─► pre-event controlled Predict
当前 METAR/SPECI
  ─► current-event joint emission
  ─► regular-METAR continuous-Y/reported-X hard condition
  ─► normalize ─► clocks/history ─► reported-X projection
  ─► posterior_checkpoint_v4_physical_state_bayes_v6
  ─► sequential_temperature_posterior_ssot_v4_reported_x
```

唯一 composer 是 `physical_state_bayes_v6_pre_event_predict_update`；唯一公式是 `p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_then_canonical_joint_emission_then_regular_metar_hard_bound_then_reported_x_projection_v1`。当前报文不得进入 Predict，NWP 小时曲线只作 transition control，Update 后禁止 post-emission state transition。v5 cumulative-replacement composer、v5 writer、旧 v3.1、bucket-product、joint composer 与所有 fallback 均已退役；身份不闭合时只允许 fail closed。

AMSC 常规 METAR 的半点、整点进入同一 v6 canonical ingress，并形成 reported-X 硬下界与日终物理 X 固定口径。AMSC SPECI 是同等级实时事实观测，立即更新动量、轨迹、云雨/露点、First-CB 与 Termination Risk，但不单独抬升日终 X 硬下界。TAF AMD/COR 创建新的 weak forecast snapshot，只影响到达后的 Predict，不回写历史状态。

## SSOT、bridge 与交易边界

```text
v6 reported-X posterior SSOT
  ├─► final X / 弱强模型每日校正（不等待市场）
  └─► approved p(S|X,C) bridge ─► settlement S ─► market bucket B
                                      └─► RealizationState
                                           └─► Transaction C
                                                └─► dynamic three-leg intent
```

UI、决策和执行只读 `data/archive.db` 同一 committed snapshot，不得复算概率。bridge 样本、strict-as-of 或 OOS 不足不得阻断 AMSC 入库、v6 reported-X 后验、final X 或已批准 artifact 运行；bridge 未批准只让市场 projection 和真实订单 fail closed。

v6 桌面看板现直接以同一 `temperature_trajectory_timeline` 最后一条 canonical AMSC 事件补齐 AMSC 实况、动量、云雨露点、八状态边际与 Termination Risk 的只读展示；这只是 SSOT 投影，不生成第二份强似然。`reported_physical_x_distribution` 仍是唯一物理最高温概率，未闭合 bridge 时市场区必须显示 `market_projection_na`。`scripts/production_v6_ui_ssot_acceptance.py` 对该边界执行生产只读验收，并明确 `external_disk_required=false`。

当前 Target Realization v6 generation 2 只消费 committed posterior/RealizationState：动态主 YES 以 `normal/upside_risk/hard_locked/invalidated` 四态运行，最高 20 USDC，阶段累计退出目标为 `min(risk_target_ratio,time_cap_ratio)`；动态次 YES 为当前次高桶，只在预计峰值前最后 30 分钟新增且严格低于 5 USDC；唯一 NO 槽位在 trajectory NO≤100 与 exact hard-exclusion NO≤200 之间互斥。所有 fill 默认建立 0.999 standing coverage，角色随每个 posterior commit 重选。

当前资本策略身份为 `dynamic_three_leg_capital_policy_v6_top20_second4_99_trajectory100_hard200`。同一 TradingPlan 的合格新增 BUY 使用统一 `capital_scale_factor` 缩量；生产 runtime 永久不使用 EV、Edge、LCB、fair value、买入价格上限、repricing、stop-loss 或 trail-stop gate。ADR-0005/v5 的 0.90、NO100 与旧执行服务均已 superseded，禁止 fallback。

### 生产执行热路径

```text
official CLOB WebSocket ─► 本地 hash 原子 quote/depth snapshot（≤1000ms）
authenticated balance API ─► 本地 collateral/allowance 只读预取（≤3000ms）
                                      │
committed posterior outbox ─► Transaction C ─► durable intent
                                      │
                                      └─► local execution guard ─► durable idempotency ─► exchange POST
```

Transaction C 与 POST 前 guard 均只读上述本地证据，不在事件热路径同步抓盘口或余额，且没有 REST fallback。缺失、过期、hash/slug/date/token/side 不一致一律 fail closed。订单传输可使用受管代理；余额预取固定为独立的 direct read-only authenticated 通道。官方 WS 历史落库异步执行，`archive.db` 锁竞争不得阻塞 WS receive loop。

## 当前部署状态

- v6 activation 已原子安装；`v5_fallback_forbidden=true`、`legacy_probability_writer_disabled=true`、`no_post_emission_state_transition=true`。
- v6 P0 与全部 durable canonical event 已提交，pending canonical event 为 0；2026-07-16 当前 active activation 的 replay generation 为 2。
- 半点/整点 METAR、SPECI 与 TAF AMD 三入口均指向 v6；下一日 P0 在热路径外预建。
- v6 probability、bridge 与 market context 已激活；概率 activation 不单独授权 CLOB。
- ADR-0006/Target Realization v6 generation 2 是唯一 deployed 策略解释权威；composer、position-cycle、standing coverage、资本比例分配、精确退出闭集、schema、artifact、双 activation 与唯一 resident 已原子闭合。
- ADR-0005/Target Realization v5 及其旧执行服务均已 superseded/disabled，生产无 fallback；`data/live_paused.flag` 当前不存在。
- 官方 WS 本地快照、余额预取、事件时零外部 REST 与 3 秒 deadline 是当前 v6 执行基础；CLOB gateway 在 pause 时保持健康但逐请求拒绝 place-order，pause 删除后才开放提交。
- 生产 v6 的 `peak_timing_state` 补齐代码已形成未接线 candidate；active runtime 尚未导入该模块，因此当前 live manifest 仍保持已验收身份。只有执行 `PHYSICAL_STATE_BAYES_V6_RUNTIME_HOTFIX_CUTOVER` 后，post-event trajectory、Termination Risk 与弱预测峰时先验才会进入同事件 SSOT。切换前次 YES 必须继续 fail closed，禁止从 UI 或经验时刻补算。

## 唯一运维边界

首次 v6 cutover 已完成，禁止重复执行。任何受 v6 runtime manifest 或 artifact 覆盖的变化，都必须创建新内容寻址 epoch，在 writer-quiescent 维护窗口完成备份、P0、全部 canonical replay、零订单验收、readiness、live acceptance 与 uniqueness 审计；v6 生效后的故障不得回退 v5。

生产回滚只允许使用本机同一 APFS 卷的短生命周期 CoW 克隆，且在验收后删除。外置盘仅可由独立低优先级任务保存可丢弃、可重建的无用数据冷归档；它不属于数据库备份、回滚、切换、readiness、SSOT 或交易边界。生产进程与脚本不得探测其挂载、空间、吞吐或哈希。挂载失败、空间不足、吞吐慢和归档失败均不得暂停或降级 AMSC/METAR/SPECI、概率更新、服务恢复或交易。
