---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-12
superseded_by: REPORTED_X_PROBABILITY_CUTOVER.md
deployment_state: not-executed
implementation_symbols:
  - launchd/com.weather.clob_order_service.plist
  - launchd/com.weather.task_queue_worker.plist
  - launchd/com.weather.trade_engine.plist
  - scripts/t1_execution_activation_audit.py
  - scripts/amsc_posterior_handoff_audit.py
verification_gates:
  - python3 scripts/document_authority_audit.py
  - python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
  - python3 scripts/signal_to_order_safety_contract_audit.py --summary
  - python3 scripts/amsc_posterior_handoff_audit.py --latest-only
  - python3 scripts/live_offline_evidence_isolation_audit.py
last_verified_commit: working-tree-2026-07-11
---

# v3.1-only 部署验收

> **RETIRED; DO NOT RUN（2026-07-15）**：v3.1-only 生产纪元已被 reported-X sequential Bayes v5 取代。本文仅供事故追溯；当前唯一概率维护 Runbook 是 `REPORTED_X_PROBABILITY_CUTOVER.md`，禁止执行下列重启、验收或激活命令恢复旧 composer。

## 部署前

```bash
cd /Users/xiong/weather
python3 scripts/document_authority_audit.py
python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
python3 scripts/signal_to_order_safety_contract_audit.py --summary
python3 scripts/amsc_posterior_handoff_audit.py --latest-only
python3 scripts/live_offline_evidence_isolation_audit.py
python3 scripts/t1_execution_activation_audit.py --source-only
python3 -m pytest -q tests/test_bot_main.py tests/test_clob_order_idempotency_store.py tests/test_sequential_remaining_warming_bayes_runtime.py tests/test_sequential_bayes_execution_contract.py tests/test_ncei_zuuu_multimodel_v31_runtime.py tests/test_document_authority_audit.py
```

所有命令必须为零退出码；v3.1 artifact 必须 `production_ready=true`。不得在同一窗口执行历史回填、payload 清理、VACUUM 或参数变更。

`--source-only` 只证明当前工作树的 T1 合同会拒绝迟到 observation；它不证明任何守护进程已经加载代码，也不授权订单。

## 重启顺序

部署动作尚未执行。批准部署后按以下顺序重启，避免旧 worker 与新 outbox 并存：

```bash
sudo launchctl kickstart -k system/com.weather.clob_order_service
launchctl kickstart -k gui/$(id -u)/com.weather.task_queue_worker
launchctl kickstart -k gui/$(id -u)/com.weather.trade_engine
sudo launchctl kickstart -k system/com.weather.monitor.daemon
```

`com.weather.task_queue_worker` 与 `com.weather.trade_engine` 是 `gui/$UID` LaunchAgent；CLOB 与 monitor 才是 system daemon。`hard_source_direct_poller.py --loop` 与 `amsc_zuuu_metar_taf_poll_once.sh` 都是 monitor daemon 的子进程，故 monitor 必须在相关代码更新后重启。不得只重启 task queue 后将旧 poller 或旧 GUI AMSC LaunchAgent 当作新 T1 合同。以上命令仅限批准的受控维护窗口；本 runbook 本身不执行任何重启。

## 即时验收

- `/health` 显示 `idempotency_store=sqlite` 且无 persistence error；
- 新 fact 的 composer 为 `sequential_remaining_warming_bayes_runtime_v31`；
- v3.1 缺失测试产生 `posterior_ssot_fact.state=blocked`；
- 不存在 v2/v3 runtime、UI HTTP 执行旁路或 legacy Node submit；
- 一次模拟超时只能进入 `reconcile_required`，不得产生第二次 POST。
- 等待一条新的 primary AMSC METAR/SPECI forward 事实；不得用旧事实重放替代。
- 新事实出现后执行 `python3 scripts/amsc_posterior_handoff_audit.py --latest-only --require-enqueued`：它必须证明 Transaction A.1 的 task 与同一 `amsc_source_fact_id`、冻结的 official-NWP generation 绑定。若 generation/组合尚不可用，该命令应明确失败；不得对旧槽位 requeue/backfill，更不得用 Open-Meteo 或旧 artifact 补救。
- `python3 scripts/t1_execution_activation_audit.py` 必须为零退出码：它要求唯一 poller 在 T1 相关源码之后启动，且 archive 中新 decision 的 execution contract 同时含 `observation_trigger_at`、来源和 T1 hard deadline。
- 该门只验证“迟到 observation 不会被 T3 伪装为新信号”；真实 `T1→T5≤3s` 仍须由 P10 live trace 单独证明。

## 24 小时验收

- write queue timeout 为 0；
- payload 无递归增长，单 fact 字节数不持续单调放大；
- fact/decision freshness 满足现行 SLO；
- unresolved CLOB intent 全部有明确对账结果；
- 无旧 composer schema、旧 fallback 或非 posterior SSOT 订单。

任一项失败即回到 fail closed 状态排查；不允许恢复旧模型 fallback。
