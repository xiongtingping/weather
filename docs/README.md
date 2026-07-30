---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-11
verification_gates:
  - python3 scripts/document_authority_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v4-contract
---

# 文档权威入口

## 支线研究方案（非生产权威）

[全国官方报文轨迹 NO 低延迟支线重构方案](research/REPORTED_TRAJECTORY_NO_LOW_LATENCY_BRANCH_PLAN.md) 仅定义隔离支线的研究和实施边界：全国 AMSC 低延迟采集（AWC 暂缓）、唯一 `reported_trajectory_no_v1` 策略、分层资金计划及报文到 gateway 的低延迟热路径。其 `status: proposed`、`authority: none`，不改变当前 v6 主线、SSOT、交易执行权或 production activation。

## 总方案入口

本系统的“总方案”不是一份能单独覆盖所有层级的研究稿，而是以下 accepted 权威的组合：

1. [SYSTEM_CHARTER](constitution/SYSTEM_CHARTER.md)：不可突破的目标、定义、边界和交易/SSOT 宪章；
2. [ADR-0003](adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md)：当前唯一 production v6 概率身份与 Bayes 时序；
3. [ADR-0005](adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md)：已退役的 v5 历史交易语义；
4. [ADR-0006](adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md)：当前唯一生产动态三腿 v6；
5. [TARGET_ARCHITECTURE_V2](architecture/TARGET_ARCHITECTURE_V2.md)：各层组件、数据流、原子事务、P10 时延链和验收合同。

当前唯一生产概率主线由 [ADR-0003 Production v6 报文前状态预测 Bayes](adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md) 授权；v6 已激活，v5 已退役且禁止 fallback。

动态三腿交易的唯一目标权威由 [ADR-0006](adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md) 与 [Target Realization v6](contracts/TARGET_REALIZATION_V6.md) 共同定义并已内容寻址激活：主 YES 四态与风险×时段阶段退出、position-cycle 再入场、次 YES 峰前最后 30 分钟且 <5 USDC、trajectory NO≤100 或 hard-exclusion NO≤200 的唯一 NO 槽位、统一资本比例缩量及所有 fill 默认 0.999。

[ADR-0005](adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md) 与 [Target Realization v5](contracts/TARGET_REALIZATION_V5.md) 已 superseded，只保留历史审计，禁止 fallback。当前 activation generation 2 同时证明唯一 resident、旧执行服务为零、CLOB pause 边界与重复 cutover no-op。

[WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR（研究与实施清单）](research/WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md) 是上述目标的完整研究脉络、缺口台账与迭代 checklist；它用于指导重构和验收准备，**不能单独改变 current/deployed production 语义**。发生冲突时，按 L0 → L1 → L2 优先级处理。

实现只能依据以下 accepted 文档：

1. L0：[SYSTEM_CHARTER](constitution/SYSTEM_CHARTER.md)
2. L0：[ADR-0003 Production v6 报文前状态预测 Bayes（当前生产概率权威）](adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md)
3. L0：[ADR-0006 当前生产动态三腿 v6](adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md)
4. L0：[ADR-0005 已退役动态三腿 v5](adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md)
5. L1：[CURRENT_ARCHITECTURE](architecture/CURRENT_ARCHITECTURE.md)
6. L1：[TARGET_ARCHITECTURE_V2](architecture/TARGET_ARCHITECTURE_V2.md)
7. L1：[GLOSSARY](architecture/GLOSSARY.md)
8. L1：[SSOT 合同](contracts/SSOT.md)
9. L1：[数据源角色合同](contracts/SOURCE_ROLES.md)
10. L1：[当前交易合同（Target Realization v6）](contracts/TARGET_REALIZATION_V6.md)
11. L1：[历史退役合同（Target Realization v5）](contracts/TARGET_REALIZATION_V5.md)
12. L1：[X→S→B Resolution Bridge 合同](contracts/RESOLUTION_X_S_B_BRIDGE.md)
13. L2：[Refresh Coordinator](REFRESH_COORDINATOR.md)

`SYSTEM_ARCHITECTURE_ADVERSARIAL_AUDIT_2026-07-11.md` 是审计证据，不得覆盖 L0/L1。

没有 YAML front matter、`status: accepted` 和明确 authority 的 Markdown 不得作为实现依据。研究结论必须先形成 accepted ADR 才能进入代码。

## Current / Target / Deployed

- `CURRENT`：ADR-0003、`CURRENT_ARCHITECTURE` 及 production activation，拥有 v6 概率解释权；bridge 已独立激活。
- `CURRENT`：ADR-0006 与 Target Realization v6 generation 2 是唯一交易执行权威；ADR-0005/v5 只作历史审计。
- `DEPLOYED`：必须由不可变 runtime manifest 绑定 git commit、schema、model/strategy artifact、launchd plist 和进程启动身份；`working-tree-*` 不能证明实际部署身份。

任何 market execution production cutover 必须另有内容寻址 activation；文档不单独授权真实订单。

当前生产退出语义读取顺序固定为：`SYSTEM_CHARTER -> ADR-0006 -> TARGET_REALIZATION_V6 -> SSOT -> 当前内容寻址 activation`。ADR-0005/v5 不在运行读取链中；文档、artifact 或 runtime 任一混代必须 fail closed。
