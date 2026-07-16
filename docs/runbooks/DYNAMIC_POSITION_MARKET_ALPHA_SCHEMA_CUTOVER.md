---
status: accepted
authority: L2
owner: weather-system
effective_from: 2026-07-14
deployment_state: schema-ready-default-off
---

# Dynamic Position / Continuation / Market-Alpha Schema 切换

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

概率切换与执行账本 schema 切换必须解耦。旧合并入口 `cutover_state_aware_strong_lr_probability_epoch.sh` 已退役，禁止从本页启动或旋转任何概率 activation。当前 v5 概率切换只遵循 `REPORTED_X_PROBABILITY_CUTOVER.md`；本页若仍需补充 additive schema，只允许独立入口 `bash scripts/cutover_dynamic_position_market_alpha_schema.sh`，并必须证明概率 activation SHA 在迁移前后完全不变。

受管脚本会停止所有受管 GUI writer、动态三腿 resident 与 `system/com.weather.monitor.daemon`，并把仍存活的受管 `--auto-trade-job-worker` 子进程视为停服失败，而不是继续迁移。它要求外置 `/Volumes/...` 备份目录可写；失败时自动恢复服务。合并入口只创建一份切换前 SQLite 备份，同时覆盖概率纪元与 additive schema 的恢复边界。

两次 maintenance packet 都必须显示 `writer_quiescent=true`；apply 后只读检查必须显示三个 schema section 均为 `state=ready`，`production_dynamic_three_leg_readiness.py` 必须返回 `state=default_off_ready`。脚本还会核验概率、动态三腿和 market-alpha 三份 activation 的 SHA-256 原样不变，证明 additive schema 没有改写唯一概率主线或打开交易。该 readiness 中的 bridge/T1→T5/accepted activation 只属于后续 live blockers，不反向阻断物理 posterior 或本次 default-off schema 验收。

## 激活仍禁止

Schema 就绪后仍不得直接开启 live。必须先完成 default-off no-order drill、CLOB 批量 snapshot endpoint 部署、同 posterior SELL→continuation→BUY 故障注入、market-alpha target-realization 优先级、partial fill/ACK-loss/restart 恢复和真实 T1→T5 验收，再单独生成 strategy/cutover artifact 与 activation。

## 生产验收记录（2026-07-15）

`scripts/activate_dynamic_position_market_alpha_schema.py --db data/archive.db` 的只读验收已确认 position-lot、continuation、market-alpha 三个 section 全部 `state=ready`，`missing_tables=[] / missing_indexes=[]`。第一阶段 `activate_dynamic_three_leg_execution_schema.py` 同时返回 `state=already_applied`。`production_dynamic_three_leg_readiness.py` 证明 resident persistent disabled、未加载、六个 live flags 全为 0、unresolved intent 为 0；因此当前状态是 `schema-ready-default-off`，不是 live。
