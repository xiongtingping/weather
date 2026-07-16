---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-14
superseded_by: REPORTED_X_PROBABILITY_CUTOVER.md
depends_on:
  - ../adr/ADR-0002-TARGET-ARCHITECTURE-SHADOW-AUTHORITY.md
  - ../architecture/TARGET_ARCHITECTURE_V2.md
---

# Sequential Bayes 生产概率主线切换 Runbook

> **退役声明（2026-07-15）**：本文记录旧 `physical_high_distribution / sequential_temperature_posterior_ssot_v2` 切换纪元，仅供事故追溯，不再授权执行。当前唯一概率切换 Runbook 是 [REPORTED_X_PROBABILITY_CUTOVER.md](./REPORTED_X_PROBABILITY_CUTOVER.md)，当前受管入口是 `scripts/cutover_cumulative_strong_likelihood_v5.sh`。禁止执行本文后续出现的旧 `cutover_state_aware_strong_lr_probability_epoch.sh`，也禁止把旧 v2 Transaction-B 字段恢复为 fallback。

## 边界

本 Runbook 只授权 orderless Transaction B：生成 production activation、重建 P0、按 canonical AMSC METAR/SPECI 回放并发布唯一物理 X 后验 SSOT。它不授权订单、CLOB、交易参数或 X→S→B bridge；`places_orders=false`、`authorizes_clob=false` 永久不变。样本、strict-as-of、OOS 与 bridge 不阻断本次 live 切换。

旧 METAR generic posterior queue 与旧 SPECI realtime materializer 已删除，不允许 fallback。安装与 resident monitor 必须同时不再携带 `AMSC_ZUUU_LEGACY_POSTERIOR_ENQUEUE_ENABLED=1` 或 `PM_AMSC_ZUUU_SPECI_REALTIME_INGRESS_ENABLED=1`。

## 历史执行序列（禁止执行）

以下命令只保留为历史证据，已经退役：

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_state_aware_strong_lr_probability_epoch.sh  # RETIRED; DO NOT RUN
```

`cutover_state_aware_strong_lr_probability_epoch.sh` 是旧纪元入口，现已退役；旧 `cutover_sequential_bayes_production.sh` 已删除，不存在兼容入口。Runbook 不复制第二份易漂移 shell。其固定阶段为：验证只读全日回放；先用最近一份已验证外盘备份运行 orderless Transaction-B preflight；停止 launchd 写者并回收 target-realization 子进程；只读确认 writer quiescence；在本机同一 APFS 文件系统创建精确 clone；先以该 clone 追加默认关闭的 position/continuation/market-alpha schema；再原子发布 state-aware Strong-LR successor activation；提交当前 activation 唯一 P0 并按顺序回放 canonical AMSC METAR/SPECI；运行 readiness；恢复服务；依次运行 cutover acceptance、`production_sequential_bayes_chain_audit.py`、状态判别审计与 SSOT alignment；最后从冻结的本机 clone 异步复制外盘备份。默认关闭 schema 必须位于 P0 之前，否则 P0 的 append-only 写入会令切换前备份与当前库页几何必然不同。若 successor epoch 与 schema 已存在，脚本退化为同一组只读验收，不跳过 SSOT 检查。

writer quiescence 采用最多三次、每次间隔 250ms 的同口径复核；`lsof` 中的 PID 还必须在随后 `ps` 复核时存活，否则视为已消失的 TOCTOU 样本。任一真实写者或 write-capable FD 持续存在仍立即 fail closed，不允许通过重试掩盖活跃 writer。

维护脚本还必须等待 `system/com.weather.monitor.daemon` 及 hard-source、METAR、SPECI、P10 子进程全树退出，再实际领取 `archive_db_write_queue`；仅证明“当前没有 DB FD”不足以排除已持 queue、尚未开库的竞态。外盘为 ExFAT 时禁止 SQLite page-wise `.backup`：writer-quiescent 后先 `wal_checkpoint(TRUNCATE)`，再顺序复制到 `.partial`、核验大小与 SQLite page/schema 可读性后原子改名。脚本每 45 秒刷新已由操作员取得的 sudo timestamp，避免长备份/P0 后在恢复服务阶段再次交互停顿。

quiescence 通过后、复制大备份之前，脚本必须实际获取并释放一次 `archive_db_write_queue`。锁 owner 同时持久化 PID 与进程启动身份，防止旧 owner 已死但数字 PID 被复用时形成永久假活锁；PID 存活但启动身份漂移时只回收该旧锁，身份无法核验时仍 fail closed。该 writer-queue 文件属于 production probability runtime manifest，锁协议变化必须触发 successor activation，不能在已授权 epoch 内静默热换。

失败恢复按 append-only 事务边界分两类：

- 当前 activation 尚无 P0 lineage：恢复切换前 activation，概率保持 fail closed；
- 当前 activation 已提交 P0 lineage：不得恢复旧 activation，也不得删除 checkpoint。保留已经提交的 activation epoch，恢复服务后按该 generation 继续回放或审计；否则会让最新 append-only checkpoint 与旧授权合同交叉。

运行时、canonical drain 与 Transaction B 都必须先用当前 `activation_contract_sha256` 定位唯一 P0，再只选择该 P0 的 `replay_generation`；禁止使用 target 全局最新 checkpoint 代替活动 generation。

## 验收

首次执行必须满足：

- readiness 的 `production_cutover.ok=true`；
- latest schema 为 `sequential_temperature_posterior_ssot_v2`；
- writer mode 为 `production`；
- `physical_high_distribution` 非空且归一化；
- latest canonical 必须为 AMSC `METAR/SPECI`，且 SSOT 的 `last_canonical_event_id`、`last_strong_update_id`、StrongObservationUpdate 三者完全绑定；
- 每条 post-event physical-only SSOT 必须含同一 event/update 绑定的 `peak_timing_state`；其概率归一、`entry_peak_at` 只能来自 forecast-supported future slot，并固定 `does_not_update_physical_x=true`；
- installed plist SHA 与仓库一致，resident legacy env 为空；
- activation 与 Transaction B 均不授权订单。
- `production_sequential_bayes_chain_audit.py` 证明活动 P0 generation 线性、canonical AMSC 与 StrongUpdate/checkpoint 一一绑定、public SSOT 指向活动 head，且 physical-only 状态没有 realization/outbox。

若切换后尚无新 canonical 报文，验收器必须返回 `cutover_accepted_forward_slo_pending`，这是正常状态，不得写成 3 秒已通过。下一条真实 METAR/SPECI 到达后再次运行验收器；只有 `T1→T3≤3000ms` 才返回 `cutover_and_forward_slo_accepted`。

实时顺序固定为 `raw archive/frozen plan → minimal SourceObservation → CanonicalObservationEvent → candidate → Transaction B/T3 → legacy fact projection → shadow mirror`。维护或验收时若发现 legacy fact、通用 task queue、AWC、shadow mirror 或弱源网络请求位于 T3 前，必须停止切换并修复；不得通过放宽 3 秒阈值、重置 `first_actionable_seen_at` 或忽略失败样本放行。

monitor 不得直接执行仓库内可变的长轮询脚本。唯一入口是 `run_amsc_zuuu_metar_taf_poll_immutable.sh`：它在 release-window sleep 前复制 poller，要求复制前后源 SHA、快照 SHA 三者一致并通过 `bash -n`，之后只执行该不可变快照。发布恰逢复制时允许短重试；无法取得一致快照则本槽 fail closed 并保留独立 P10 raw 证据。禁止在已经启动的 shell 等待期间依赖原地改写脚本仍可安全继续；代码发布后必须由受管 service restart 接管新入口。

AMSC fast lane 若因 writer queue 竞争未提交，必须把同一冻结 plan 持久化到 `data/amsc_zuuu_metar_taf/pending_probability`，并在兼容事实投影释放写句柄后有界重放。待处理计划必须按最新 AMSC 主观测身份（`event_date + valid_time + source_observation_id + raw_sha256`）去重，不得按整份可变 plan 哈希分裂；重复延迟只能累计审计计数，必须保留最早冻结 plan 与原始因果时间。历史重复 plan 在重放前由 `scripts/amsc_probability_pending_queue.py` 合并，非保留副本连同原收据移入按观测身份分组的 `superseded/`，并生成不可覆盖的 compaction receipt；禁止静默删除证据。补偿只允许 `canonical → sequential posterior`，不得调用 `posterior_target_direct`、旧 claimable-job drain 或 direct order；原始 T1 保持不变，因此补偿成功只修复 SSOT 完整性，不能把该事件计作 3 秒通过。`hard_source_direct_poller` 不拥有 posterior 消费权，也不得通过任何旧环境变量恢复 fast precheck。

fast-lane latest receipt 必须先写同目录临时文件，再以原子 `mv` 替换；成功和失败结果都遵守同一规则。禁止先删除或截断 `p2_canonical_result_latest.json` 再把子进程 stdout 直接重定向进去，否则 UI/审计会在更新窗口读到空文件或半截 JSON。

非关键扫描不得跨网络抓取、模型计算或整个子进程生命周期持有 `archive_db_write_queue`。尤其 `model_scan_hourly` 父 shell 不得先持锁再启动会自行申请同一写队列的 Python 子进程；这会形成跨进程不可重入死锁并阻塞 AMSC。扫描程序只能在实际 SQLite 写连接/短事务范围领取队列，AMSC fresh lane 的单次 A0/A1/Transaction-B claim 保持最高时延优先级。

production probability 对 `event_fact_history.fact_snapshot_id` 的身份查询必须显式包含 `fact_snapshot_id<>''`，以满足既有部分唯一索引的谓词并由 `EXPLAIN QUERY PLAN` 证明命中该索引。缺失谓词会在 13GB archive 上退化为每次约 3 秒的全表扫描；禁止通过增加 SLO、SQLite timeout 或缓存预热掩盖该查询错误。

AMSC SourceObservation/canonical 与 probability Transaction-B 的关键写连接必须显式设置 connection-local `wal_autocheckpoint=0`。提交仍按 SQLite WAL durability 合同正常同步；该设置只禁止 3 秒关键连接因碰巧跨过默认 1000-page 阈值而替后台执行 checkpoint。WAL 页回写由维护窗口或后续非关键 writer 承担，不得把 checkpoint 延迟重新塞回 T1→T3。13GB production clone 的同版本演练必须同时证明两个关键连接的实际 PRAGMA 值均为 0，并保留独立性能审计；clone 结果不能替代正式 production 前向报文。

仅设置 `wal_autocheckpoint=0` 仍不足以控制短命子进程退出尾延迟：SQLite 在最后一个连接关闭时可能同步 checkpoint/清理 WAL。`system/com.weather.monitor.daemon` 必须先于 AMSC METAR/SPECI child 启动 `archive_db_wal_keeper.py`，并持续持有 `archive.db` 的只读、无事务连接；它不得申请 writer queue、写 SQLite 或触发订单。切换验收必须由 `archive_db_wal_keeper_audit.py` 证明 heartbeat 新鲜、PID 存活、数据库绑定正确且 lsof access mode 仅为 `r`。因为 system-domain keeper 属于 root，维护窗口必须以已有的非交互 sudo 执行这项只读 PID/FD 审计；普通用户的 `EPERM` 只能报告为检查权限不足，禁止误报为 PID 死亡。服务恢复后的监督接管允许最多 30 秒有界 readiness 重试，条件始终不变，超时仍 fail closed。14GB clone 的同版本演练需证明 keeper 存在时首日 `P0 ensure + canonical strong update + SSOT` 总墙钟小于 3 秒；该演练仍不能替代新 activation 后真实前向 T1→T3 样本。

物理日 rollover 不得依赖市场先出现。午夜后的首条 AMSC 观测若发现当前 activation 尚无当日 `production_initial_posterior_lineage`，fast lane 必须在同一 writer-queue claim 内先幂等创建 `prior × one joint weak` P0，再顺序消费已经独立落库的 canonical event。P0 public key 由 ZUUU 日期确定性生成，market discovery、finality、bridge、训练样本与 OOS 均不得成为该步骤的 live gate；重复调用不得追加第二个同 activation P0。

维护窗口备份顺序固定为：停写并完成 WAL checkpoint 后，在与 `archive.db` 同一文件系统的真实本机目录 `data/local_cutover_backups`（不得是符号链接）以 `cp -c` 形成精确、不可变的切换前快照；切换、P0 重放与 AMSC 服务恢复后，再把该冻结快照顺序复制到外部磁盘并验收尺寸与 SQLite schema。`data/backups` 当前指向外盘，禁止把它当作本机 clone 目录；同时禁止把 13GB 级 ExFAT 复制留在观测停机窗口内，也禁止服务恢复后直接复制已再次变化的 `archive.db` 冒充切换前备份。外盘必须在维护窗口开始时可写；若它在本机 clone 已验收、probability epoch 已提交且服务恢复后意外掉盘，核心切换保持 accepted，脚本必须在 `data/local_cutover_backups/external_backup_deferred.<stamp>.json` 留下精确源文件、目标路径与失败码，不得把已提交 epoch 误报为概率切换失败。外盘复制仍是待补的耐久性任务，但不允许回滚或停住实时 SSOT。

`scripts/weatherctl.sh stop` 是 GUI 域写进程的维护边界，而不只是 LaunchAgent bootout 请求。它必须在返回前验证并回收由旧 wrapper 遗留的仓库内子进程，包括 market refresh/WS、model scan、task worker、strategy batch、target-realization worker 与 UI；先有界等待，再 TERM/KILL，并以仓库绝对路径或 UI 进程 cwd 证明所有权。任何残留写进程都必须使停机失败，禁止放宽 writer-quiescence 审计绕过。

维护停机顺序固定为 `disable/bootout system/com.weather.monitor.daemon → 确认 monitor PID 退出 → weatherctl stop GUI writers → writer-quiescence audit`。system monitor 拥有 root 的 hard-source、AMSC、WAL keeper 等子进程；若先由普通用户执行 `weatherctl stop`，它既无权回收 root 子进程，仍存活的 monitor 也会再次拉起它们。刷新脚本一进入停机函数就必须设置“恢复服务责任”状态；此后即使任一步失败或被中断，也必须重新安装/启用 monitor 并启动 GUI 服务，禁止留下半停机状态。

旧 `production_probability_runtime_activation_v2.activation_effective_at` 仅是该历史纪元的前向验收起点；当前 reported-X v5 以受管 activation 自身的 `activation_effective_at` 为唯一边界。只有 `first_actionable_seen_at >= activation_effective_at` 的 canonical 报文可以计入 3 秒 SLO；切换前已经落库或失败切换留下的事件不得被误判为新切换的延迟失败。最新 P0 lineage 的 `activation_contract_sha256` 必须与当前 activation 完全一致，否则验收 fail closed。

若前一次切换在 P0/事件回放后失败，数据库中的旧 checkpoint 与 lineage 不删除、不覆盖。重试必须追加新的 activation generation：新 P0 的 `replay_generation` 严格等于既有最大 generation 加一，`supersedes_checkpoint_id` 指向旧 latest checkpoint；随后以新 P0 的空 `consumed_event_ids` 为基准重新回放全部 canonical METAR/SPECI。运行时与 Transaction B 只选择当前 `activation_contract_sha256` 对应的唯一 P0 lineage，禁止旧失败 activation 污染当前后验。

任何失败都必须恢复新版 monitor 继续保存 canonical AMSC 事实，且绝不恢复旧概率 writer。activation 是否恢复只由“当前 activation 的 P0 lineage 是否已经提交”决定；已提交 epoch 只能前向恢复，不能伪装成尚未发生。
