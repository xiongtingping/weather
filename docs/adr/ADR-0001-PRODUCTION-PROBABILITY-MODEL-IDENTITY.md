---
status: superseded
authority: L0
owner: weather-system
effective_from: 2026-07-14
superseded_by: ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md
supersedes: [v2-composer-with-v3-v31-fallback, v31-production-identity]
implementation_symbols:
  - hierarchical_posterior_pipeline_runtime.py:build_hierarchical_posterior_pipeline
  - joint_weak_live_evidence_runtime.py:build_live_joint_weak_likelihood
  - weather_fact_snapshot_writer.py:build_posterior_ssot_fact
verification_gates:
  - python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
last_verified_commit: working-tree-2026-07-14
---

# ADR-0001：生产概率模型身份

> **退役声明（2026-07-15）**：本文只记录 reported-X v5 切换前的 joint-composer 生产纪元，不再拥有 current/deployed 解释权，也不得授权恢复 `joint_weak_physical_strong_composer_v1`。当前唯一 L0 概率权威是 ADR-0002。

决策：当前生产概率模型是 **joint weak × physical prior × strong sequential**，唯一 live composer 为 `joint_weak_physical_strong_composer_v1`。它由 `hierarchical_posterior_pipeline_runtime_v1` 统一编排，连续物理温度域后验先形成、最后才积分为市场桶；任何旧 v3.1/v3/v2/桶级乘法或加性 evidence 模型均不得作为 fallback。

执行合同必须精确验证该 composer、joint weak artifact/forecast evidence identity、物理硬下界、连续网格归一化、CDF 单调终值与其 bucket integral 和 SSOT bucket surface 一致；不得以未识别/不完整的连续面替代，也不得因合同版本滞后而误将有效 joint posterior 阻断。

顶层 fact 的 `fact_snapshot_id/posterior_run_id` 与 `posterior_ssot_fact` 内同名 lineage
必须完全相等；不相等说明消费者面对两个事实身份，执行必须 fail closed。

执行时延合同同时必须携带物理报文的不可变触发时刻 `T1`：新 resident 路径使用
`first_actionable_seen_at`，旧路径只能明确标识为 transport `response_completed_at`。`fact_as_of`
或 posterior commit（`T3`）绝不能重置 `T1→T5` 计时器；缺失 T1、T1 晚于 posterior，或在
决策检查时已超过 3 秒硬 deadline 都必须 fail closed。3 秒同时是 SLO 与最后允许执行时刻，
任何环境变量或调用参数只能收紧、不能放宽。这样一条迟到物化的历史报文不能被
伪装为刚提交的 posterior 而触发下单。
`sell-fill` 后的后续执行计时只适用于已经通过原始 T1 新鲜度检查的已授权 transition；它不能
凭一个新 timestamp 复活迟到 observation 的 BUY。

旧 v3.1、v3、v2、旧桶级乘法和旧加性 evidence runtime 已退出生产，不允许 fallback；当前 joint composer 不可用时输出 blocked posterior SSOT，执行 fail closed。

`target_realization_v3` 仅表示交易策略版本，与概率模型 v3/v3.1 无关。

此决策消除“外层旧模型、内层 joint、任意回退”造成的身份歧义，并使 fact、decision、order 的 composer contract 可被单值验证。它不授权尚在 shadow 的 P0/P8/P10 Transaction B 目标链；后者必须经过 ADR-0002 的独立切换。
