---
status: research
authority: L2
owner: bayes_basket_target
effective_from: 2026-07-28
---

# RKPK strict ΔBasketScore 前向候选影响矩阵 v1

状态：`DESIGN → IMPLEMENTED`；仅用于 `production_no_live` 前向事实采集，不代表 `OOS_PASSED`。

## 合同变化

- `PRODUCTION_NO_LIVE_CANDIDATE` 可解析预注册 `CANDIDATE` strict-delta artifact。
- 候选必须 `oos_gate_passed=false`、`sample_n=0`、`holdout_n=0`、`oos_metrics={}`，禁止伪装历史 OOS。
- `FORMAL_PROMOTED` 仍只接受 `PROMOTED + oos_gate_passed=true`。
- live、gateway、订单授权始终为 false。

## 表与 artifact

- 仅扩展 `basket_strict_delta_policy_promotion.decision` CHECK，增加 `CANDIDATE`。
- 原表所有行逐字复制，不重标、不删除。
- 新增 RKPK/P1/summer/d2_fallback/liquid/target-v1 预注册候选 artifact、生命周期回执和 activation。
- 不改概率、registry、WS、inventory、order-state、资本或订单表。

## 消费者与历史

- `TargetProductionWorker` 可在完整 P1 + 官方 WS + q0/order-state 下计算 strict `ΔBasketScore` 并写 no-live decision/training fact。
- 正式消费者仍不能把候选当作 OOS-passed artifact。
- 历史数据无需重建；已有 blocked decisions 保留审计且不进入训练。

## 测试与回执

- 契约测试覆盖候选零 OOS 声明、正式 promotion 硬门、schema 行保留和重复回绕。
- 真实验收必须证明：RKPK 一行 training fact、零订单、零gateway、受保护 PID 不变。

## 维护窗与回滚

- 使用 SQLite `BEGIN IMMEDIATE` transaction fence，不停止或重启共享服务。
- 切换前创建同盘恢复备份。
- 回滚方式：停止 target 新写入后恢复该备份；不得逆向删除 append-only authority 行。
