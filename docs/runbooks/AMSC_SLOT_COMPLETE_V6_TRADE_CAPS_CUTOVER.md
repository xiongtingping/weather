# AMSC 槽完成与动态三腿额度受管切换

状态：已于 2026-07-16 完成生产切换。唯一生产 activation 为 `7b94b2a81f77e385d55786f5ca89bf8d24322ea30f7ee6f65083475e35dbc830`，runtime manifest 为 `f0274475d0e53e05fa3bff6ec72ed5eb49d7c05ce25568cc149c14ff392d68a5`，禁止回退旧模型。受管入口保留为 `scripts/cutover_amsc_slot_complete_v6_and_trade_caps.sh`，不得重复执行已完成的切换。

## 目标

- 常规 METAR 的 `:00/:30` 调度只在精确本槽报文进入 canonical/v6 后才算完成；
- 迟到的上一槽报文先作为真实强观测补入 posterior，但不能吞掉当前槽，也不能获得过期交易执行资格；
- 每次轮询清空事件级 P2 receipt，禁止把上一槽成功状态误报为本槽成功；
- monitor 重启补偿窗口统一为 1800 秒；
- 动态三腿额度固定为主 YES 20 USDC、次 YES 4.99 USDC、联合物理排除 NO 100 USDC；
- 继续永久禁用 EV/Edge/LCB gate，且本次切换不自动开启当前 disabled 的真实交易 activation。

## 原子边界

脚本先生成不激活的 candidate，并用 `cutover_candidate_pre_rehearsal` 口径验证当前 runtime、artifact、schema、依赖闭包和 live route；该口径只证明候选可以进入受管演练，不宣称候选已经生产激活，也不要求 `active=candidate`。

输入 `sudo` 后才执行以下原子序列：

1. 暂停 system monitor 与全部用户域 writer；
2. writer quiescence 审计通过后，检查 `data/db_writer.lock`；只有记录 PID 已不存在时，才允许用 sudo 回收 root 所有的失效租约，活 PID 或不可归属租约一律阻断；
3. 安装候选轮询路由，重新生成并冻结绑定当前代码、路由和 artifact 的 candidate activation；
4. 完成全 canonical replay、零订单演练与当前主机演练；
5. 在本机生成 SQLite 一致快照，完成 `integrity_check`，复制到外置硬盘并比较两端 SHA-256 后才升格为正式备份；
6. 原子安装 activation，创建新 generation P0，重放全部 canonical 事件；
7. live acceptance 通过后才设置不可回退提交边界并恢复服务。

新 generation 提交前失败时，数据库、旧 activation 与旧轮询路由必须全部恢复并再次通过旧 activation live acceptance，才能恢复服务。数据库回滚使用同一 APFS 文件系统内的原子重命名，不创建第二份 16GB 恢复副本。任何回滚步骤失败时必须保持服务停机，并保留本地快照、失败数据库和 rollback 目录。

新 generation 已提交后禁止回退旧概率模型；任何验收失败必须暂停服务、保留新 v6 fail closed，不允许旧模型 fallback。

## 执行

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_amsc_slot_complete_v6_and_trade_caps.sh
```

## 验收

- `production_probability_v6_live_acceptance` 为 `production_v6_live_accepted`；
- `pending_canonical_event_count=0`，`replay_generation` 单调增加；
- 半点/整点 catch-up 与 exact-slot 状态可区分；
- probability transaction 小于 3000ms；
- `production_trade_gate_zero_audit` 全通过；
- UI 只读 SSOT，并显示次 YES 4.99 USDC；
- 动态三腿 activation 仍按独立受管流程启用，本脚本不下单。

生产验收结果：

- `production_probability_v6_readiness=production_v6_live_ready`；
- `production_probability_v6_live_acceptance=production_v6_live_accepted`；
- `replay_generation=2`，`pending_canonical_event_count=0`；
- 07:00 与 06:30 延迟槽在服务恢复后进入 canonical/v6，六事件补录总 ingress 为 1223.533ms，最大单事件概率事务为 239.268ms；
- 切换后的首个原生 07:30 半点由 monitor 自动触发：AMSC METAR 37℃，T1=`07:30:49Z`，T3=`07:30:51.149Z`，T1→T3=2149ms，fast-lane ingress=743.433ms，reported-X 下界更新为 37℃；
- `PM_AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC=1800` 已安装，active/candidate 轮询路由 SHA 均为 `5a5fb1f09a63c993133deb48dc0cbe79f86d7fc104ab0c7829ddae06367658cc`；
- SSOT、交易零门和 UI 概率一致性审计通过；切换期间没有新增订单、posterior outbox 或 execution contract；
- 动态三腿保持 `default_off_ready`：概率主线已就绪，live 仍由 activation、市场 bridge 和真实 T1→T5 证据独立阻断。

精确槽的长期验收入口：

```bash
python3 scripts/production_amsc_exact_slot_live_acceptance.py \
  --db data/archive.db \
  --event-date YYYY-MM-DD \
  --slot-valid-at YYYY-MM-DDTHH:MM:00Z \
  --expect-no-order
```

该审计只证明 T1（首次可行动观测）到 T3（v6 SSOT checkpoint）小于 3000ms；不得把它表述成 T1→T5 真实下单证明。T1→T5 必须来自启用交易后实际 gateway/exchange post 样本。

## 首次执行故障结论

- `writer_quiescent=true` 只能证明没有数据库句柄，不能证明 `db_writer.lock` 租约已消失；root writer 异常退出可留下 root 所有的死租约；
- `os.kill(pid, 0)` 返回 `PermissionError` 表示外 uid 进程仍可能存活，不能解释为 PID 已死亡；
- 回滚不得依赖额外 16GB 可用空间；恢复复制失败后更不得继续启动服务；
- 以上三条已进入代码、测试与本运行手册，不能作为临时人工经验留在日志中。
