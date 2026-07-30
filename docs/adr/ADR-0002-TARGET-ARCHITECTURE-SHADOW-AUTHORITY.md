---
status: superseded
authority: L0
owner: weather-system
effective_from: 2026-07-15
activation_state: reported-x-v5-retired-no-fallback
superseded_by: ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md
supersedes: [ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md, target-architecture-shadow-only-phase]
implementation_symbols:
  - config/production_probability_runtime_activation.json
  - production_reported_x_amsc_archive_probability_ingress.py
  - production_reported_x_weak_revision_event_runtime.py
  - sequential_bayes_filter_shadow_runtime.py
  - production_reported_x_public_ssot_writer.py
  - production_reported_x_market_transaction_writer.py
verification_gates:
  - python3 scripts/probability_document_authority_audit.py --summary
  - python3 scripts/production_reported_x_runtime_readiness.py --event-date ET_TODAY
  - python3 scripts/production_chain_uniqueness_audit.py --summary
  - python3 trade_strategy_loop_audit.py --summary
last_verified_commit: working-tree-2026-07-16-v6-production-cutover
---

# ADR-0002：Reported-X v5 生产概率与默认关闭交易架构

> **退役声明（2026-07-16）**：ADR-0003 v6 已通过受管生产切换并成为唯一概率权威。本 ADR 仅保留 v5 历史设计与事故回放价值；v5 composer、writer、post-transition 和 cutover 均不得恢复为 production 或 fallback。

## 决策

批准 reported-X sequential Bayes v5 为唯一生产概率主线，结束 ADR-0002 的 shadow-only 阶段并 supersede ADR-0001 的旧 joint-composer 生产身份。唯一公式和 composer 固定为：

```text
p0_prior_z_weak_once
→ canonical_cumulative_evidence_replacement_delta_log_lr
→ weak_revision_replacement
→ reported_x_projection_v3

composer = sequential_bayes_filter_reported_x_v5_cumulative_replacement
```

旧 v3.1、旧 `joint_weak_physical_strong_composer_v1`、bucket-product、absolute-strong map 及任何 fallback 永久退役。当前 activation 不可用、manifest 漂移、前驱身份或依赖闭包不一致时必须 fail closed，并保留 raw/source/canonical 供修复后重放；不得恢复旧 posterior。

该决策只授权 orderless 概率主线。X→S→B bridge、动态三腿 Transaction C 与 CLOB live 必须各自满足独立 activation；schema ready、代码存在或 `trade_strategy_loop.ok=true` 都不构成下单授权。

## 类型与来源宪章

```text
Z = scheduled regular-METAR slot latent daily high
X = AMSC ZUUU 当地日 scheduled regular METAR 报告整数温度最大值
S = 冻结 resolution source 的 settlement scalar
B = 冻结 market contract 从 S 映射的市场桶
```

AMSC regular METAR 是 X 硬下界和日终 X 的唯一事实口径；整点、半点均触发 canonical update。AMSC SPECI 是同等级实时事实观测，立即进入强似然、温度动量/轨迹、云雨露点和 Termination Risk，但不填充 scheduled slot、不单独改写 X 硬下界。所有 NWP、TAF/AMD/COR、WU、TWC 都是弱预测，不得晋升为事实。

## Bayes 时序

P0 只消费 prior 与当时可见的唯一 joint weak likelihood 一次。每条 canonical event 只以事件前 checkpoint `Z_t^-` 计算累计强证据评分，并以相邻累计评分差 `ΔlogL_t` 更新；事件产生的轨迹状态只能进入 `Z_t^+`。首次 CB 只在首次出现事件消费一次，后续只保留状态；夜间自然降温和重叠历史不得逐报文重复相乘。TAF/AMD/COR 进入独立 weak-revision replacement，不得伪装为强观测或新增一份 prior。

StrongObservationUpdate、PosteriorCheckpoint、RealizationState（若 bridge 可用）、public SSOT history/latest 与 posterior outbox 必须遵守同一 Transaction B 原子边界；physical-only 分支在 bridge 不可用时仍提交 reported-X SSOT，但不生成 market projection/outbox。posterior 是概率唯一 SSOT，UI 和交易层不得复算。

## 训练与 bridge 解耦

AMSC 日终独立形成 final X；弱模型使用“当时预测版本 + final X”，强模型使用“当时实况轨迹/物理证据 + final X”。它们不等待市场、finality、S 或 bridge。strict-as-of、OOS 和样本量只控制候选 artifact 晋级，不阻断 AMSC 入库、已批准基线或物理 X live。

市场通道独立执行 `p(X)→p(S|X,C)→p(B|S,bucket_contract)`。bridge 未批准时 B 必须 NA，Transaction C 与真实订单 fail closed；禁止当前报文 X 直接映射市场 NO。

## 交易边界

交易只消费 committed RealizationState：动态主 YES（当前最高后验、20 USDC、0.90 波段退出）、动态次 YES（当前次高、预计峰值前最后 30 分钟、严格小于 5 USDC、执行常量 4.99 USDC、吃结算）和联合物理证据经 approved bridge 排除的 NO（100 USDC）。生产 runtime 永久禁止 EV、Edge、LCB、repricing、stop-loss 和 trail-stop gate。

两阶段动态三腿 schema 已安装，但 resident persistent disabled、未加载，六个 production/live 开关为 0。只有 probability identity、approved bridge、accepted strategy activation、无 unresolved intent 与 signal-to-order 硬截止安全全部闭合，`trade_strategy_loop.live_ready` 才能为真。gateway-attested `T1→T5≤3s` 是首笔真实 POST 后的持续验收证据，不得与初次 live activation 互锁。

## 部署与变更

旧 v5 patch/refresh cutover 已退役，禁止二次执行。任何受 runtime manifest 覆盖的代码或 artifact 变化都必须通过当前唯一入口 `cutover_v6_probability_and_soft_market_projection.sh` 在 writer-quiescent 窗口创建新 orderless activation/P0 generation、全 canonical replay、零订单验收并验证 readiness。该入口只允许本机 APFS CoW 回滚阶段；外置盘仅是可丢弃冷归档，缺失、离线、变慢或失败均不得影响 AMSC/METAR/SPECI 采集、SSOT、readiness、切换或恢复。文档、测试或运维口头确认不能热换 deployed identity。
