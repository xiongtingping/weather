---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-14
deployment_state: schema-ready-default-off
superseded_by: ../adr/ADR-0004-DYNAMIC-THREE-LEG-V4-POSTERIOR-NO-STANDING-EXIT.md
---

# Dynamic Position / Continuation / Market-Alpha Schema 切换

> **已完成、禁止重跑**：additive schema 已验收。本页只保留历史证据，不再授权维护操作。外置盘只用于可丢弃、可重建的无用数据冷归档，绝不保存生产数据库，也不进入备份、回滚、readiness、服务启停、SSOT 或交易边界。

## 边界

本维护窗口只追加以下默认关闭的执行账本：

- `dynamic_three_leg_position_lots`
- `dynamic_three_leg_position_lot_events`
- `dynamic_three_leg_continuation_events`
- `dynamic_three_leg_continuation_transition_plans`
- `market_alpha_take_profit_cycles`
- 对应索引

它不修改 posterior、概率 activation、交易 activation 或 launchd，不回填 legacy `edge_trades`，也不下单。迁移前后两个 activation JSON 和五个 production/live 环境开关必须保持 disabled/0。

## 唯一维护序列

概率切换与执行账本 schema 切换必须解耦。旧合并入口 `cutover_state_aware_strong_lr_probability_epoch.sh` 与已完成的 additive-schema 入口均已硬退役，禁止从本页启动或旋转任何 probability activation，也禁止重跑 schema。当前唯一概率切换入口是 `scripts/cutover_v6_probability_and_soft_market_projection.sh`；它不得探测或依赖外置盘。

历史受管脚本曾停止全部 writer 并要求外置备份；该路径已经退役，不得重跑或借用。任何后续 schema successor 必须使用本机同卷瞬时 CoW 回滚边界，并且不得探测或依赖外置盘。

两次 maintenance packet 都必须显示 `writer_quiescent=true`；apply 后只读检查必须显示三个 schema section 均为 `state=ready`，`production_dynamic_three_leg_readiness.py` 必须返回 `state=default_off_ready`。脚本还会核验概率、动态三腿和 market-alpha 三份 activation 的 SHA-256 原样不变，证明 additive schema 没有改写唯一概率主线或打开交易。该 readiness 中的 bridge/accepted activation 只属于后续 live blockers，不反向阻断物理 posterior 或本次 default-off schema 验收；真实 T1→T5 是 live 启动后的持续 SLO，不得反向阻断第一笔真实样本的产生。

## 激活仍禁止

Schema 就绪后仍不得直接开启 live。必须先完成 default-off no-order drill、CLOB 批量 snapshot endpoint 部署、同 posterior SELL→continuation→BUY 故障注入、market-alpha target-realization 优先级、partial fill/ACK-loss/restart 恢复，再单独生成 strategy/cutover artifact 与 activation。初次受控 live 不等待一个只有真实 POST 才能产生的历史 T1→T5 样本；不可延长的 3 秒 deadline 先约束每笔 POST，首批真实样本产生后立即纳入持续验收。

successor 发布固定为两步：先运行 `scripts/prepare_dynamic_three_leg_production_release.py --apply`，只生成内容寻址 strategy/cutover artifact 与两份 candidate activation，不改 active 配置、不写 `archive.db`、不下单；待 v6、bridge、市场上下文已激活且下一条 canonical 观测已把市场桶写入最新 SSOT 后，再运行 `scripts/activate_dynamic_three_leg_production_release.py --apply --confirm ACTIVATE_DYNAMIC_THREE_LEG_PRODUCTION_LIVE`。后者先创建本机 pause、安装并加载 resident，验证全部 active identity 后才删除 pause；删除动作是唯一执行线性化点。外置盘在两步中均不得被探测或依赖。

## 生产验收记录（2026-07-15）

`scripts/activate_dynamic_position_market_alpha_schema.py --db data/archive.db` 的只读验收已确认 position-lot、continuation、market-alpha 三个 section 全部 `state=ready`，`missing_tables=[] / missing_indexes=[]`。第一阶段 `activate_dynamic_three_leg_execution_schema.py` 同时返回 `state=already_applied`。`production_dynamic_three_leg_readiness.py` 证明 resident persistent disabled、未加载、六个 live flags 全为 0、unresolved intent 为 0；因此当前状态是 `schema-ready-default-off`，不是 live。
