---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-19
activation_state: implementation-authorized-production-cutover-pending
depends_on:
  - ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md
  - ../research/PHYSICAL_STATE_SPACE_BAYES_UPGRADE_PLAN.md
  - ../contracts/SSOT.md
  - ../contracts/RESOLUTION_X_S_B_BRIDGE.md
implementation_symbols:
  - identifiable_physical_state_bayes/contracts.py
  - identifiable_physical_state_bayes/core.py
  - identifiable_physical_state_bayes/reporting.py
  - identifiable_physical_state_bayes/revision.py
  - production_probability_v6_event_runtime.py
  - production_probability_v6_weak_revision_runtime.py
  - identifiable_physical_probability_worker.py
  - prospective_physical_evidence_capture.py
  - scripts/build_identifiable_physical_state_release.py
  - scripts/cutover_identifiable_physical_state_production.py
verification_gates:
  - python3 -m pytest tests/test_identifiable_physical_state_bayes.py tests/test_identifiable_physical_basket_transaction_b.py
  - python3 scripts/replay_identifiable_physical_state_bayes_candidate.py --days 7 --end-date 2026-07-19 --summary
  - python3 scripts/identifiable_physical_state_bayes_zero_order_drill.py
  - python3 scripts/identifiable_physical_state_production_readiness.py --end-date 2026-07-19 --replay-days 45 --summary
  - python3 scripts/production_chain_uniqueness_audit.py --summary
---

# ADR-0007：可辨识物理状态空间 Bayes 替换 v6 composer

## 决策

批准用固定可辨识状态 `Z=(T_tilde,q,G_net,W,C_cp,R)` 替换 v6 的
`continuous Y × 八标签状态` 概率 composer。替换只发生在概率内核；以下边界不得改变：

- `CanonicalObservationEvent` 的身份、去重、乱序和 correction 语义；
- v6 的 `pre-event predict → current-event joint emission → normalize` 在线时序；
- 常规合同槽 reported temperature 对 `X` 的硬下界；
- Transaction B、公开 `PosteriorSSOTFact`、唯一 latest pointer；
- 冻结 `X→S→B` bridge；
- 篮子/硬 NO 的 decision、资本预留、intent、幂等和唯一 CLOB gateway 合同。

新 composer 不允许 post-emission 第二次状态转移。当前事件只在一个联合观测模型中
消费一次；温度、温度变化、露点、云、雨、风、能见度和气压不得拆成独立 LR 重复相乘。

## 固定状态与证据角色

```text
continuous = (T_tilde [degC], q [kg/kg], G_net [degC/h], W [0,1], C_cp [degC/h])
discrete   = R in {
  clear_heating, cloud_attenuated, precipitation_cold_pool,
  warm_advection_or_mixing, cold_advection, post_rain_recovery,
  evening_stable_decay
}
```

站点/盆地/季节只建立一次层级 prior。NWP、TAF 与 AMD 只生成带 lineage 的弱未来
driver 与多情景；AMD 在有效窗口内替换旧 TAF，不作为独立票。AMSC METAR/SPECI 是唯一
canonical 强观测。市场价格、盘口、持仓和收益不得进入物理 posterior。

弱预报修订采用严格 forward-only 语义：先用被替代的旧 driver 从当前物理状态参考时刻
前推到修订 `decision_as_of`，再把已条件化状态与新情景做独立乘积换绑。换绑前后忽略
forcing-scenario 后的状态一、二阶矩、regime 质量和参数 draw 质量必须保持不变；新情景只
影响生效时刻之后的 Predict/路径投影。不得计算新旧温度 likelihood 比值，不得消费
canonical event，不得回放既往 transition。若生效时刻前已有可见而未消费的 canonical
event，必须先 drain event，否则修订 fail closed。

## 目标投影

物理状态 posterior 必须先生成未来连续温度路径，再在冻结 AMSC 合同槽应用编码、QC 和
缺报算子，得到唯一公开 `P(X)`。连续曲线裸峰值只作内部诊断。`P(X)` 继续经既有 bridge
生成 `P(S)`/`P(B)`；bridge 不可用时只提交 physical-only SSOT，不创建交易 outbox。
bridge 可用时，新 composer 只在同一 Transaction B 物化篮子消费的 `P(B)`；不得进入已退役
三腿的 strong-evidence、realization 或 outbox 表面，也不得由概率层直接创建策略信号。

有限路径求积不得把连续 Gaussian mixture 未枚举到的合法整数桶误写为数学概率零。冻结
报告 artifact 必须显式声明有界连续支持补全核；仅当仍有未来合同槽时，最多重分配 `1e-6`
总质量用于数值尾支持。补全不得越过 reported-X 硬下界，不得改变已完全观测日的 delta，
且其参数、是否生效和支持边界必须进入路径投影 lineage。

## 切换规则

新内核在确定性回放、增量/全量等价、硬下界、概率守恒、证据单次消费、缺报算子、
故障注入、延迟和零订单演练全部通过前不得改写 production activation。切换必须是一次
原子 generation 更新；切换后旧 composer 禁止 live fallback，但可保留为离线历史回放。

生产接入不得恢复 monitor、独立 SPECI watcher 或第四个概率服务。新 composer 必须作为
无下单 worker 驻留在现有 `com.weather.reported_trajectory_no` 服务中：硬 NO lane 与其证据
完全隔离，篮子 lane 只消费它原子物化的 P0/P1/SSOT。旧 activation 时 worker 必须在打开
数据库前休眠。受控发布先冻结 release bundle；writer quiescence 后先提交 additive schema、
再安装内容寻址 artifacts，activation 文件最后原子替换。提交后故障只能 fail closed，不得
自动回退旧 composer。

NLL 与 CRPS 是非阻断诊断指标：必须计算、持久化并可告警，但即使劣于旧 composer 也不得
单独否决生产激活。它们不属于 `promotion_gates`，不得参与 `promotion_ready` 的布尔合取。
严格 as-of、独立日/事件量、真实值正支持、因果/数值/原子合同、延迟和零订单演练仍是
fail-closed 阻断项。

严格样本的前瞻采集不受新 composer activation 控制，否则会形成“未校准所以不激活、未
激活所以不积累校准证据”的闭环。TAF/AMD 与官方 NWP Tier-A 采集作为无订单后台任务嵌入
同一 `reported_trajectory_no` 进程，但与硬 NO lane 完全隔离：只追加不可变 raw/P1 证据，
不得写 posterior、SSOT、decision、intent 或订单。NWP provider 必须按持久化游标逐源轮转，
单轮只完成一个 provider 的整条官方曲线；失败也推进游标，禁止单一慢源长期饿死其他源。
依赖 ECMWF client、TLS roots 与 ecCodes 的 NWP collector 必须由同一服务以有墙钟上限的
临时子进程调用批准的 `weather-official-nwp-venv`；临时子进程不是守护服务，不持有订单
权限，超时即终止并 fail closed。不得让缺这些依赖的 system Python 假装采集成功。
