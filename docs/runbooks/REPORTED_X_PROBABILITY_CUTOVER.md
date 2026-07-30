---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-15
superseded_by: PHYSICAL_STATE_BAYES_V6_PRODUCTION_CUTOVER.md
depends_on:
  - ../constitution/SYSTEM_CHARTER.md
  - ../architecture/TARGET_ARCHITECTURE_V2.md
  - ../contracts/STRONG_LR_FIT_CONTRACT.md
---

# Reported-X 概率主线切换 Runbook

> **RETIRED; DO NOT RUN**：2026-07-16 v6 已成为唯一生产概率主线。本 runbook 与其 v5/v3 命令仅供历史事故复盘，禁止再次执行或作为 fallback。

## 目标

将唯一生产概率主线从连续潜变量冒充物理最高温的 v2，切换为：

```text
连续潜变量 Z
→ 常规 AMSC METAR 报文整数口径投影
→ 物理最高温 X
→ 独立 X→S→B bridge
→ 市场桶概率
```

生产目标类型固定为：

`amsc_scheduled_regular_metar_reported_daily_max_c`

## 不变量

- 常规 AMSC METAR：实时强观测，同时形成日终物理 X 的整数硬下界。
- AMSC SPECI：同等级实时强观测，只加速动量、轨迹、云雨和终止风险更新；不得直接抬高 X 硬下界。
- TAF/AMD：弱预测修订，只替换原弱似然，不得作为独立证据重复相乘。
- 公开 SSOT 只允许 `reported_physical_x_distribution`，禁止公开连续 Z。
- bridge 只消费 reported-X，不消费连续潜变量。
- P0、强观测、弱修订均不得创建 outbox、execution intent、交易决策或订单。
- strict-as-of、训练样本与 OOS 不得阻断已批准 live 基线。
- 主模型不可用必须 fail closed，禁止回退旧概率模型。

## 已通过的切换前证据

- successor 专项测试：38/38。
- 应用最终入口补丁后的专项测试：30/30。
- 隔离数据库：1 个 P0＋14 个 METAR＋2 个 SPECI，共 17 个 v3 checkpoint。
- canonical 全量顺序重放完成，无剩余事件。
- SPECI checkpoint 的 reported-X 下界与此前常规 METAR 最大值一致。
- `posterior_commit_outbox`、`posterior_execution_intents`、`auto_trade_decisions` 增量均为 0。
- fast lane 隔离实测 1.606 秒，其中核心处理约 0.258 秒。
- 验收 receipt：`data/logs/reported_x_successor_no_order_drill_latest.json`。

## 执行

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_reported_x_probability_epoch.sh
```

脚本会依次完成：

1. 检查现网 v2、测试、最终补丁及 no-order receipt。
2. 暂停 monitor、bot、任务队列及所有 archive.db writer。
3. WAL checkpoint，并创建精确 APFS 本地备份。
4. 原子切换 SPECI 软证据、canonical schema 校验及实时入口。
5. 应用 8 个 additive reported-X checkpoint 字段。
6. 生成并启用 v3 activation。
7. 重建 P0，并全量重放当日 canonical METAR/SPECI。
8. 验收 v3 SSOT、整数概率支持、SPECI 边界和零订单副作用。
9. 恢复 launchd 服务，并再次验收。
10. 删除已经完成验收的本机瞬时回滚副本；外置盘不参与本切换。

## 自动回滚

在最终验收完成前任一步骤失败，脚本都会：

- 保持 writer 停止；
- 用 APFS 克隆恢复精确切换前 `archive.db`；
- 恢复 v2 activation；
- 恢复所有被切换的入口源码；
- 再启动由 launchd 托管的服务。

切换日志：`data/logs/reported_x_probability_epoch_cutover.log`。

## 切换后验收

必须同时成立：

- activation schema 为 `production_probability_runtime_activation_v3_reported_x`；
- checkpoint schema 为 `posterior_checkpoint_v3_reported_x`；
- public SSOT schema 为 `sequential_temperature_posterior_ssot_v3_reported_x`；
- 概率支持全部为整数摄氏度且总和为 1；
- SSOT 不含 `physical_high_distribution` 和 latent Z；
- pending canonical event 为空；
- monitor 仅运行于 `system/com.weather.monitor.daemon`；
- 下一条 METAR、半点 METAR 或 SPECI 能触发 v3 SSOT 更新；
- TAF AMD 能触发弱似然替换；
- 实况到 SSOT 小于 3 秒；有可执行 bridge 时再单独验收 T1→CLOB。
- fast lane 的 canonical proposal 只包含本次 plan 中尚未 canonical 的 current-day primary AMSC source id；重复或空输入必须在短事务内幂等 no-op，禁止整日扫描。
- AMSC fetch 热路径只同步持久化 immutable raw object 与 signed poll manifest；`coverage_v2` 的全 archive 扫描、完整性审计和日覆盖物化必须在 T3 尝试之后运行，不得占用 T1→T3 预算，也不得成为 live posterior 阻断。
- 维护窗口在 writer-quiescence 审计通过后、备份与 activation mutation 之前，必须实际取得并释放一次 `archive_db_write_gate`。被停止的 governed writer 若留下 dead/zombie owner lock，必须由 PID＋process-state 合同安全回收；不得盲删所有 `*.lock`，也不得让 P0 replay 用 30 秒超时才发现孤儿锁。
- UI、trace 和其他 read endpoint 只能读取 `archive.db` 已提交 SSOT；不得刷新市场、清理执行状态、启动交易或取得 writer gate。pending 时冻结上一 checkpoint 并明确显示 stale/refreshing。

持续只读健康检查：

```bash
python3 scripts/production_reported_x_runtime_readiness.py \
  --event-date "$(TZ=Asia/Shanghai date +%F)"
```

## 强似然 v5 累计证据替换切换

旧 `canonical_event_raw_log_lr` 语义会把重叠轨迹和 First-CB 历史在每条报文重复相乘，禁止用调小系数延续。v5 固定为累计强证据替换：composer 只消费相邻累计评分的 `ΔlogL`；08:00 前自然夜间降温对最终 X 的直接似然中性，常规 METAR 硬下界仍立即生效。

> **一次性边界**：以下 `cutover_cumulative_strong_likelihood_v5.sh` 只用于尚未存在 v5 activation 的首次切换。生产已是 v5 后禁止重复执行；任何受审计 runtime 文件变化都必须使用本节后面的 `refresh_reported_x_runtime_manifest_epoch.sh` 创建新 manifest epoch。旧入口检测到既有 v5 activation 时必须在停服务、备份、数据库或 activation 写入前退出，并打印唯一刷新命令。

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_cumulative_strong_likelihood_v5.sh
```

脚本先运行 `scripts/probability_document_authority_audit.py`，任何 L0、目标架构、强似然合同或唯一 Runbook 回退都会在停服务前失败。随后脚本停写、在本机同一 APFS 卷建立短生命周期精确回滚副本、应用冻结 patch、生成新 activation/P0、全量 replay canonical events、验证逐格条件创新和零 decision/intent/outbox 增量、恢复服务。成功必须原子生成 `data/logs/cumulative_strong_likelihood_v5_live_acceptance.json` 与 `data/logs/cumulative_strong_likelihood_v5_live_readiness.json`；失败时仅在 writer-quiescent 边界使用该本机副本恢复数据库、源码和 activation。

外置盘永久位于生产边界之外：只允许由独立、低优先级、可失败的任务归档可丢弃且可重建的非关键数据。禁止向外置盘复制 `archive.db`、本机回滚副本、activation、运行凭据或唯一验收证据；禁止探测其挂载、容量、速度或哈希并据此改变 readiness、维护窗口、服务启停、AMSC/METAR/SPECI、SSOT 或交易。外置盘拔出、盘满、损坏或归档失败的生产影响固定为零。

## 生产验收记录（2026-07-15）

- v5 activation effective at `2026-07-15T05:42:34.000Z`，runtime manifest `cf472c382d3b8c0bcf16a91d12386dae73e67b75882e50b69fce0497776b22af`；
- 26 条既有 canonical 全量 replay，27 个 checkpoint，逐格累计替换验收无无效 checkpoint；
- `auto_trade_decisions/posterior_commit_outbox/posterior_execution_intents` 增量均为 0；
- 首条真实 post-cutover 14:00 METAR：`first_actionable_seen_at=2026-07-15T06:00:47Z`，SSOT committed at `06:00:48.509Z`，`T1→SSOT=1509ms`，fast lane `998.263ms`；
- acceptance、readiness 与首条前向事件收据分别位于 `data/logs/cumulative_strong_likelihood_v5_live_acceptance.json`、`data/logs/cumulative_strong_likelihood_v5_live_readiness.json`、`data/logs/cumulative_strong_likelihood_v5_first_live_forward_event.json`；
- probability activation 永久 orderless；X→S→B bridge 未闭合时，`T1→CLOB` 明确 NA，不能用 `T1→SSOT` 冒充下单时延。

### Runtime manifest 新纪元刷新

v5 切换后若受审计的 runtime 文件发生语义修改，不得改写原 activation hash，也不得重复执行已应用的 v5 patch。唯一维护入口为：

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/refresh_reported_x_runtime_manifest_epoch.sh
```

它只在 writer-quiescent 窗口创建精确备份、生成新的 orderless successor activation/P0 generation、全量 replay 当日 canonical；replay 提交后立即以 root 权限只读核对 active generation checkpoint，并仅归档已由该 checkpoint 消费、且 `event_date + valid_time + source_observation_id` 精确匹配的 pending 文件计划。随后做零订单 acceptance、恢复服务并验证 readiness；最后必须通过 production chain uniqueness 与 `trade_strategy_loop_audit_v2_reported_x_dynamic_three_leg`，后者允许 `safe_fail_closed`，但不允许任何 safety blocker。失败恢复数据库和 activation。2026-07-15 上一纪元运行结果为 replay generation 6、28 条 canonical、29 个 checkpoint、pending=0，runtime manifest=`69d5c5b68c05f8e0a0c9434ef3d0a10ee1d354c93914930c514fc7397ff58173`；该 hash 只描述当时纪元，后续 runtime 修改必须再生成新纪元，不得把历史记录解释为当前 ready。该次 `auto_trade_decisions/posterior_commit_outbox/posterior_execution_intents` 增量均为 0。

X→S→B 的 production 状态必须单独运行：

```bash
python3 scripts/production_reported_x_market_readiness.py \
  --event-date "$(TZ=Asia/Shanghai date +%F)"
```

当前合法状态为 `safe_fail_closed_accumulating`：bridge 样本不足和 activation disabled 只阻断 `B`/outbox，不阻断 reported-X live。完整定义与晋级顺序见 [X→S→B Resolution Bridge 合同](../contracts/RESOLUTION_X_S_B_BRIDGE.md)。

## Forward 3 秒验收边界

每次 runtime manifest 新纪元只证明代码、activation、P0 与 canonical replay 身份闭合，不能代替真实前向时延。必须等待该纪元之后自然到达的新 METAR、半点 METAR 或 SPECI，并以不可变 `first_actionable_seen_at(T1)` 与对应 Transaction B `committed_at(T3)` 计算 `T1→T3`。验收要求：

- canonical source relation 与 checkpoint consumed sequence 精确绑定同一 source id；
- `T3-T1 ≤ 3000ms`，且 writer-queue wait、SQLite busy、A0、A1、推理、Transaction B 分段计时完整；
- 不得用轮询开始、文件读取、任务领取或 UI 刷新时间替代或重置 T1；
- 超时事件仍可提交 posterior，但执行资格必须 fail closed；
- 没有闭合且启用的 X→S→B bridge/outbox 时，`T1→CLOB` 仍为 NA，禁止用 T1→T3 冒充下单验收。
