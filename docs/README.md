---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-11
verification_gates:
  - python3 scripts/document_authority_audit.py
last_verified_commit: working-tree-2026-07-11
---

# 文档权威入口

## 总方案入口

本系统的“总方案”不是一份能单独覆盖所有层级的研究稿，而是以下三份 accepted 文档的组合：

1. [SYSTEM_CHARTER](constitution/SYSTEM_CHARTER.md)：不可突破的目标、定义、边界和交易/SSOT 宪章；
2. [ADR-0002](adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md)：reported-X v5 的生产概率身份、目标类型、Bayes 时序与默认关闭交易边界；
3. [TARGET_ARCHITECTURE_V2](architecture/TARGET_ARCHITECTURE_V2.md)：各层组件、数据流、原子事务、P10 时延链和验收合同。

下一代概率主线由 [ADR-0003 Production Successor v6 报文前状态预测 Bayes](adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md) 授权直接按生产 successor 开发。它不建立长期 shadow 主线；受管切换前 v5 仍是唯一 production，v6 激活后 v5 立即退役且禁止 fallback。

[WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR（研究与实施清单）](research/WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md) 是上述目标的完整研究脉络、缺口台账与迭代 checklist；它用于指导重构和验收准备，**不能单独改变 current/deployed production 语义**。发生冲突时，按 L0 → L1 → L2 优先级处理。

实现只能依据以下 accepted 文档：

1. L0：[SYSTEM_CHARTER](constitution/SYSTEM_CHARTER.md)
2. L0：[ADR-0002 Reported-X v5 生产概率权威](adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md)
3. L0：[ADR-0001 旧 joint-composer 生产身份（已 superseded）](adr/ADR-0001-PRODUCTION-PROBABILITY-MODEL-IDENTITY.md)
4. L0：[ADR-0003 Production Successor v6 报文前状态预测 Bayes（生产继任开发权威，待受管 activation）](adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md)
5. L1：[CURRENT_ARCHITECTURE](architecture/CURRENT_ARCHITECTURE.md)
6. L1：[TARGET_ARCHITECTURE_V2（Reported-X v5 / Market Default-Off）](architecture/TARGET_ARCHITECTURE_V2.md)
7. L1：[GLOSSARY](architecture/GLOSSARY.md)
8. L1：[SSOT 合同](contracts/SSOT.md)
9. L1：[数据源角色合同](contracts/SOURCE_ROLES.md)
10. L1：[交易退出合同（Target Realization v3 + Market Alpha）](contracts/TARGET_REALIZATION_V3.md)
11. L1：[X→S→B Resolution Bridge 合同](contracts/RESOLUTION_X_S_B_BRIDGE.md)
12. L2：[Refresh Coordinator](REFRESH_COORDINATOR.md)
13. L2：[Reported-X 概率主线切换与 manifest 刷新](runbooks/REPORTED_X_PROBABILITY_CUTOVER.md)
14. L2：[v3.1-only 部署验收（已 superseded）](runbooks/V31_ONLY_DEPLOYMENT_ACCEPTANCE.md)
15. L2：[Dynamic Position / Continuation / Market-Alpha Schema 切换](runbooks/DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA_CUTOVER.md)

`SYSTEM_ARCHITECTURE_ADVERSARIAL_AUDIT_2026-07-11.md` 是审计证据，不得覆盖 L0/L1。

没有 YAML front matter、`status: accepted` 和明确 authority 的 Markdown 不得作为实现依据。研究结论必须先形成 accepted ADR 才能进入代码。

## Current / Target / Deployed

- `CURRENT`：ADR-0002、`CURRENT_ARCHITECTURE` 及现行 L1 合同，拥有 reported-X v5 生产概率解释权。
- `TARGET`：ADR-0003 的 production successor v6，以及 `TARGET_ARCHITECTURE_V2` 中尚未激活的 bridge、动态三腿与执行验收；它们在各自受管 activation 前不拥有 production latest 或真实订单权限。
- `DEPLOYED`：必须由不可变 runtime manifest 绑定 git commit、schema、model/strategy artifact、launchd plist 和进程启动身份；`working-tree-*` 不能证明实际部署身份。

任何 market execution production cutover 必须另有 accepted activation/ADR；ADR-0002 已 supersede ADR-0001 的旧概率身份，但不授权真实订单。

退出语义与实现读取顺序固定为：`SYSTEM_CHARTER -> TARGET_REALIZATION_V3 -> SSOT -> ADR-0002 activation boundary`。L2 研究方案不得把 accepted 目标规则写成 current/deployed 事实；CURRENT 文档中的 P0 不符合项必须由代码与可失败审计关闭，不能靠改文档消除。
