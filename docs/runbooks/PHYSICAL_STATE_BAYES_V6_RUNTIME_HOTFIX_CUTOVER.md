---
status: accepted
authority: L2
owner: weather-system
effective_from: 2026-07-16
verification_gates:
  - python3 scripts/production_probability_v6_hotfix_bundle_audit.py --summary
---

# Physical State Bayes v6 运行时换代 Runbook

状态：`ready_for_controlled_peak_timing_runtime_cutover`

## 目标与边界

本 Runbook 只用于已经运行 v6 时，将已验证的 v6 运行时代码正式绑定到新的激活合同。它不是 v5→v6 切换，不改变 Bayes 公式、模型 artifact、交易策略或 CLOB 权限。

- ZUUU 物理事件日固定使用 `Asia/Shanghai`。
- 半点/整点调度补偿窗覆盖完整 30 分钟槽；维护恢复后必须立即补采当前槽，不得因恢复时间超过 8 分钟而漏掉报文。
- AMSC 常规 METAR、半点 METAR、SPECI 与 TAF AMD 的既有 v6 路由保持不变。
- 新激活建立独立 P0 lineage 和 replay generation，并重放当日全部 canonical 事件。
- 概率事务不创建订单或 outbox；旧激活不得作为失败后的运行时 fallback。
- strict-as-of/OOS 样本仍用于训练与晋级，不阻断已批准生产基线的 live 观测更新。
- 本次变更把同事件 post-event canonical 轨迹与 `forecast_peak_timing_prior` 合成为 `peak_timing_state`，写入 StrongObservationUpdate、checkpoint 辅助状态和 public SSOT；该状态明确 `does_not_update_physical_x=true`，不修改任何 `p(X)` 数值。
- 默认关闭的 v6-native market Transaction-B 分支随 runtime 一起安装，但 market context 与 bridge 继续关闭；因此本次仍不产生 market B、RealizationState、交易 outbox 或订单。该分支不可用时显式降级为同一 v6 的 physical-only 事务，只关闭市场投影，绝不阻断物理 X，也不允许旧概率模型 fallback。

## 两阶段执行

第一阶段无需维护窗口：`prepare_production_probability_v6_hotfix_bundle.py` 在 `/private/tmp` 创建隔离的轻量源码树，安装候选变换、冻结 activation/manifest，并对生产库只读运行候选测试、确定性 canonical replay、零订单演练和真实 schema host rehearsal。通过后只在本机 `data/candidates` 发布内容寻址 bundle；不修改 active runtime、不写 `archive.db`、不暂停 AMSC，也不接触外置盘。`production_probability_v6_hotfix_bundle_audit.py` 同时绑定旧 active 源码/activation SHA，防止准备后到切换前发生漂移；`production_probability_v6_hotfix_install_rehearsal.py` 再在独立临时根目录实际复制候选 runtime/activation，并重算完整 runtime closure，证明维护窗口安装顺序和路径自足。

第二阶段才请求 `sudo`：暂停所有 archive writer并校验静默；备份 active 源码与 activation，复核预制 bundle 的 baseline 身份后原子安装两份冻结 runtime 和 candidate activation，并验证安装后的完整 runtime manifest。维护窗口不再重跑候选测试、离线 replay 或 host rehearsal；只在同一 APFS 卷创建瞬时 CoW 数据库回滚克隆、安装新激活、创建新 generation P0、重放切换瞬间最新的全部 canonical 事件，再通过 readiness、live 与 peak-timing 专项验收后恢复服务。新 activation 验收前任何失败都同时恢复源码、activation 与数据库。

外置归档盘明确不属于生产切换边界：它只允许保存可丢弃、可重建的无用数据冷归档，不得保存或承担 `archive.db` 的生产备份、回滚副本、切换凭据或验收证据。脚本不检查其挂载、空间、吞吐或哈希，不向外置盘复制生产数据库，也不会因外置盘异常暂停 AMSC/METAR/SPECI。无用数据归档只能由独立低优先级任务异步执行；失败、掉盘或空间不足不得改变 readiness、live 状态、SSOT 更新或交易。

切换的 `event_date` 必须由 `TZ=Asia/Shanghai` 生成并在进入维护窗口前复核，因为它代表冻结合同中的 ZUUU `resolution_local_date`。`operations_date_et` 只用于交易、统计和值班，不得替代物理 `X`、结算 `S` 或市场目标日。

`scripts/production_storage_boundary_audit.py` 在维护窗口之前只读证明：`archive.db` 是本机非符号链接普通文件、与 workspace 及 `/private/tmp` 同设备，切换脚本只使用本机 APFS CoW 克隆且不引用 `/Volumes`、`data/backups` 或任何外置备份变量。该审计本身不探测外置盘。

当同一次维护需要同时启用当日 soft X→S→B 投影时，唯一组合入口为 `scripts/cutover_v6_probability_and_soft_market_projection.sh CUTOVER_V6_AND_ACTIVATE_SOFT_MARKET_PROJECTION`。它先完成 v6 epoch，再按执行当日重新物化 bridge/context、运行绑定候选的只读无订单演练，最后原子替换 bridge/context active pointer；因此跨日旧 context 不会被误用。该入口不打开动态三腿 activation，不启动真实交易 resident，完成后的下一边界是等待下一条 canonical 观测把 p(B) 写入最新 SSOT。

唯一入口：

```bash
cd /Users/xiong/weather
sudo -v
bash scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh
```

## 成功条件

- candidate activation 与当前 runtime manifest 哈希一致；
- 预制 bundle 的 tests/replay/zero-order/host-rehearsal 收据全部绑定同一 activation 和 manifest，且切换时 active baseline SHA 未漂移；
- canonical replay 确定性成立，hard bound 单调；
- 当前日所有 canonical 事件已消费且 pending 为 0；
- 单次 probability transaction `< 3000ms`；
- `auto_orders`、`posterior_commit_outbox`、`posterior_outbox_execution_contracts` 无新增；
- public SSOT 的 activation、posterior run 与最新 checkpoint 一致；
- public `peak_timing_state` 与同一 checkpoint/update/event 严格绑定，且与 `StrongObservationUpdate.path_auxiliary_json` 完全一致；
- market Transaction-B 候选必须验证 physical resolution contract 与 checkpoint、Target、Bridge、日期和 authority SHA 的四方绑定；无效市场输入只关闭市场投影，不阻断 physical-X；
- `peak_timing_state` 只分配已观测峰与弱预测支持的未来峰时质量，未来槽缺失时显式 `NA/future_peak_time_unresolved`，绝不伪造时钟；
- UI 概率仅投影 `reported_physical_x_distribution`，一致性审计无 mismatch。

## 失败语义

在新激活验收前失败：使用同卷瞬时 CoW 回滚克隆和旧激活恢复原 v6 状态。新激活验收后失败：保留新 v6 并 fail closed，禁止回退旧激活。验收成功后立即删除本地回滚克隆。
