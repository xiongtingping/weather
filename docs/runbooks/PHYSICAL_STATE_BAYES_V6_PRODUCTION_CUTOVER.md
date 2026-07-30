---
status: accepted
authority: L2
owner: weather-system
effective_from: 2026-07-15
activation_state: completed-production-v6-active-v5-retired-no-fallback
depends_on:
  - ../adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md
  - ../constitution/SYSTEM_CHARTER.md
---

# Physical State Bayes v6 生产继任切换

> **完成状态（2026-07-16）**：受管切换已完成，终态为 `production_v6_active_v5_retired_no_fallback`。本 runbook 继续作为 v6 纪元维护与审计依据；不得重新运行首次切换，也不得恢复 v5。

> **后续运维覆盖（2026-07-16）**：本文所记外置盘复制、挂载和 SHA 校验只描述首次切换的历史过程，不再具备后续生产运维授权。所有 successor v6 runtime 切换以 `PHYSICAL_STATE_BAYES_V6_RUNTIME_HOTFIX_CUTOVER.md` 为准：外置盘只用于可丢弃、可重建的无用数据异步冷归档，绝不能进入数据库备份、回滚、readiness、维护窗口或服务启停边界。

> 以下首次切换序列仅是已经完成的历史记录，不再授权任何操作。当前及后续维护只允许使用本机 APFS 瞬时 CoW 回滚边界；外置盘不属于生产系统。

事件日边界固定使用 `America/New_York`（ET），与系统结算口径一致；不得用中国午夜切换到尚未开始的下一 station day。

## 唯一切换顺序

```text
冻结 transition / observation / prior / weak artifacts
→ 内容寻址 v6 runtime manifest
→ canonical production replay 与 Transaction B 零订单/故障注入
→ pre-cutover 只剩 schema / route / activation 三项预期阻断
→ 停止全部 writer 并验证 writer-quiescent
→ 本地 SSD 生成并校验 SQLite 一致性快照
→ 顺序复制到外置盘并进行内外盘 SHA-256 位级等值校验
→ 再验 candidate 文件与内容寻址 runtime manifest 无漂移
→ additive schema
→ METAR 半点/整点、SPECI、TAF AMD 三入口原子改线
→ 每日 18:00 后在生产事实库预建次日 v6 P0，移出首条报文热路径
→ 原子安装唯一 production activation
→ 新 replay generation 的 v6 P0
→ 消费全部 durable canonical event 并发布 v6 latest
→ readiness 与 live acceptance 全绿
→ v5 composer、post-transition artifact 与旧 cutover 退役
→ 恢复服务并验证 3 秒链路
```

## 不可跨越的门

- 不建立第二个 live latest；离线配对只生成验收证据。
- schema applied 不等于 v6 activated。
- strict-as-of/OOS 不阻断当前 v5 live，但必须约束新 v6 artifact 的首次 production promotion。
- v6 activation 必须明确 `v5_fallback_forbidden=true`。
- v6 runtime manifest 必须证明 retired v5 composer/writer 不可达；复用先验、弱预测和 canonical 事实合同不等于复活 v5 composer。
- `forecast_background_context` 必须绑定当前 weak snapshot；TAF AMD 只改变后续 Predict。硬 `mixed` 标签、固定事件次数 `ρ` 和未经校准的 regime bias 不得进入 production transition artifact。
- 当前 v6 的候选条件化温度/云雨/CB 小时强迫已经消费预测天气背景；同一信息不得再以 regime LR 或第二份状态偏置重复计入。
- 禁止将 SQLite `.backup` 的页级随机写或 SQLite `integrity_check` 的随机页遍历直接落到 ExFAT 外置盘；本地 staging 空间不足、本地 integrity check 失败或内外盘 SHA-256 不一致时必须停在 pre-commit。
- 本地已验证 staging 快照必须保留到 v6 提交或回滚完成；pre-commit 失败优先从本地快照恢复，避免依赖外置盘回读。长备份完成后必须再次验证 candidate 文件 SHA 和 runtime manifest SHA 均未漂移，任何漂移在 schema 前 fail closed。
- 外置卷根固定为 `/Volumes/Expansion`，切换开始时冻结其 filesystem ID，并在停写后、复制前后及外置 SHA 前重复校验；掉载、换盘或同名目录落到系统盘时必须在 schema 前失败并恢复服务。
- `com.weather.feedback_loop_hourly` 属于 archive writer，必须与 UI、market、trade、monitor writer 一同停启和审计，不得在备份期间由 KeepAlive 复活。
- 任一 replay、CAS、public SSOT、manifest 或 artifact 身份失败，切换前保持 v5；切换后 fail closed，不回退 v5。
- 概率 activation 本身不授权 CLOB；真实订单仍由独立 bridge、策略和 execution activation 控制。

## 当前 readiness

只读检查：

```bash
cd /Users/xiong/weather
python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
```

维护前该命令的 blocker 集合必须严格等于：

```text
v6_additive_schema_not_applied
v6_live_routes_not_active
v6_active_activation_not_installed
```

三项之外出现任何 blocker，切换脚本在停服前退出。唯一生产切换命令：

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_physical_state_bayes_v6_production.sh
```

脚本不下单；它会暂停全部 writer，先在本地 SSD 生成 `archive.db` 一致性快照并执行 SQLite integrity check，再顺序复制到外置盘并验证内外盘 SHA-256 完全相同，随后应用 schema、改线、安装 v6 activation、建立 P0、消费 canonical、验收并恢复服务。本地临时快照无论成功或失败都必须删除。停服状态的 readiness＋live acceptance 是唯一提交点：提交点前失败恢复数据库、入口文件和 v5 activation；提交点后（包括恢复服务后的验收）任何故障只允许保留 v6 并 fail closed，不允许回退 v5。
