---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-15
depends_on:
  - ../constitution/SYSTEM_CHARTER.md
  - ../architecture/TARGET_ARCHITECTURE_V2.md
  - SSOT.md
  - SOURCE_ROLES.md
verification_gates:
  - python3 scripts/resolution_bridge_pair_coverage_audit.py
  - python3 scripts/production_reported_x_market_readiness.py --event-date "$(TZ=Asia/Shanghai date +%F)"
---

# X→S→B Resolution Bridge 合同

## 1. 定义与唯一公式

- `X`：AMSC ZUUU 冻结常规 METAR 计划槽形成的整数报文日最高温；唯一实时物理 posterior 为 `p(X|E_t)`。
- `S`：Wunderground ZUUU 历史观测按冻结 authority、日期、单位和解析规则得到的最终结算标量。
- `B`：冻结市场 target contract 定义的互斥且完备的结算桶。
- `C`：authority、物理/结算来源、目标日、单位、舍入与 bucket 边界的不可变合同。

唯一映射为：

```text
p(S=s | E_t,C) = Σ_x p(S=s | X=x,C) p(X=x | E_t)
p(B=b | E_t,C) = Σ_s 1[bucket_C(s)=b] p(S=s | E_t,C)
```

`p(B)` 是 `p(X)` 经独立 observation-definition kernel 的投影，不是第二个天气 posterior，不得再与 prior、弱似然或强似然相乘。市场价格、订单簿、持仓、盈亏、EV、Edge、交易结果永不进入 bridge。

## 2. 宪章

1. AMSC→`X` 是实时事实/物理概率主线；bridge 只在其后消费已提交 `reported_physical_x_distribution`。
2. Wunderground `S` 只服务结算口径映射、市场级 OOS 与标签，不得反向改写当天 `X`。
3. `X/S` pair、strict-as-of、OOS 或样本阈值不足时，只令 `S/B/RealizationState/outbox` fail closed；不得阻断 AMSC raw/source/canonical 入库、StrongUpdate、checkpoint、public `X` SSOT 或 final-X 日标签。
4. 已观测 AMSC 温度只能形成 `X` 的硬下界。任何 market `NO` 排除必须由闭合 bridge 产生的 bucket exclusion certificate 授权，禁止 `当前报文温度 → B` 直接比较。
5. 极端 `or below/or higher` 桶保持真实单侧无界；不得用物理温度网格边界代替市场边界。
6. candidate、production artifact、activation、target contract 四种身份分离。candidate 不能自我晋级；probability activation 永久 orderless，bridge activation 也永久 `places_orders=false/authorizes_clob=false`。

## 3. 边界与所有权

```text
L1 physical posterior owner
  reported-X checkpoint / public SSOT p(X)
                 │
                 ▼
L2 bridge owner  approved p(S|X,C) artifact + immutable target contract
                 │
                 ▼
L3 realization   p(S), p(B), exclusion certificates, dynamic rank state
                 │
                 ▼
Transaction B    StrongUpdate + checkpoint + RealizationState
                 + public fact/latest + posterior outbox + 3s contract
                 │
                 ▼
Decision Core    dynamic main YES / second YES / physically excluded NO
```

进入 market Transaction B 前必须完成四方内容绑定：physical resolution contract 的 `resolution_contract_id` 必须等于当前 checkpoint，且其 authority id/SHA、settlement source、resolution local date 与 immutable Target contract、获批 Bridge artifact 完全一致。任何一项漂移只允许将 market projection 标记为 NA 并继续提交同一 v6 physical-X；不得生成 B、RealizationState 或 outbox，也不得阻断 AMSC 实况和物理后验。

Bridge 不拥有天气推理、交易策略、资金、价格判断或 CLOB。Decision Core 不得重算 `p(X)`、kernel、`p(B)` 或物理排除。

## 4. 数据与校准合同

pair 唯一来源是 isolated `data/shadow/resolution_final_labels.db`：每个 event day 最多一个 active authority pair，并同时保留 X/S 两端 source-observation ID、raw ref、raw SHA、target contract SHA 与 authority SHA。append-only registry 中的旧 authority pair只保留审计，不计入当前覆盖。

market finality 只证明冻结 target 的 Gamma event 已 closed 且全部 market 为 resolved，不读取胜出 outcome、价格、持仓或盈亏。finality poll 的最早时刻固定为 `resolution_local_date + 1 day 00:00 Asia/Shanghai`；在 ZUUU 当地物理日结束前必须返回 `deferred_event_day_open` 且禁止网络请求。日终后的 `URLError/TimeoutError` 属于可重试采集状态，不得阻断物理 `X`、实时 posterior 或 shadow registry；原文 JSON、slug、日期、resolution source、全桶 resolved 等结构错误仍 fail closed，禁止降级成 pending。

开始拟合前至少需要：

- 90 个不同 event day；
- 12 个不同 physical `X`；
- 12 个不同 settlement `S`。

`resolution_bridge_calibration_runtime.py` 只离线拟合离散残差 `D=S-X`：全局采用零中心离散 Laplace 收缩；季节样本达到 30 日才产生季节 PMF，否则回退到全局 PMF。评估使用 chronological expanding prequential OOS，至少 18 个评估日，并报告 log loss、MAE 与 CRPS。达到覆盖只表示可进入校准审查，不表示 artifact 获批或 bridge 已闭合。

生产 artifact 固定 schema `resolution_x_s_bridge_artifact_v1`，必须：

- 绑定 accepted authority/source/target/settlement/kernel identity；
- residual support 在 `[-8,+8]℃`，所有 PMF 归一；
- 绑定训练集 SHA、覆盖、OOS、approval ID 与 approval receipt SHA；
- `state=production_approved, bridge_closed=true, validated=true`；
- 不含任何交易字段。

## 5. 唯一运行实现

| 职责 | 唯一实现 |
|---|---|
| pair 覆盖 | `resolution_bridge_pair_coverage_runtime.py` |
| Gamma finality 采集窗口 | `market_finality_shadow_capture_runtime.py` |
| candidate 校准 | `resolution_bridge_calibration_runtime.py` |
| artifact 合同/闭合 resolution contract | `resolution_bridge_artifact_runtime.py` |
| 独立 promotion | `resolution_bridge_promotion_runtime.py` |
| orderless activation | `resolution_bridge_activation_runtime.py` |
| `X→S→B` 投影 | `posterior_market_projection_runtime.py` |
| 强物理证据的 B-space realization | `reported_x_market_realization_runtime.py` |
| public SSOT/outbox materialization | `production_reported_x_market_ssot_runtime.py` |
| 原子提交 | `production_reported_x_market_transaction_writer.py` |
| 持续 readiness | `scripts/production_reported_x_market_readiness.py` |
| 无订单演练 | `scripts/production_reported_x_market_no_order_drill.py` |

缺少或未批准 bridge 时，现行 `production_reported_x_public_ssot_writer.py` 继续原子提交 physical-only `X`，明确写 `market_projection_na`，并结构性禁止 RealizationState/outbox。

闭合后，`production_reported_x_market_transaction_writer.py` 必须在一个 `BEGIN IMMEDIATE` 中完成：StrongUpdate、reported-X checkpoint、RealizationState、public fact/latest、posterior outbox 与 `posterior_outbox_execution_contracts`。提交前必须重验 active probability/bridge activation、前驱 checkpoint/run/posterior SHA CAS、P0 冻结上下文、唯一初始 lineage、realization 内容哈希/内容寻址 ID、public `X` 与 checkpoint 完全一致、bridge/target SHA、归一 `p(B)` 及直接报文桶为空。任一步失败整笔回滚；writer 不含网络、签名或 CLOB。

## 6. 状态机

```text
accumulating
→ ready_for_calibration_review
→ shadow_candidate_ready
→ independent review / prequential OOS accepted
→ production_approved artifact
→ separate orderless bridge activation
→ market projection ready
→ no-order Transaction-B drill accepted
→ Decision/Execution 独立切换与 T1→CLOB 前向验收
```

任何阶段不得跳级。artifact 或 activation 漂移、target contract 不匹配、`p(B)` 不归一、direct `X→B`、CAS/lineage/hash 不一致均 fail closed，但不得删除已提交 canonical 或物理 `X`。

## 7. 当前状态与后续 checklist

截至 2026-07-15：

- reported-X v5 production replay generation 6 已 ready，pending canonical=0；
- bridge activation 为 `disabled`；
- 当前 authority 有效 pair 为 1 日、1 个 X、1 个 S，另有 1 条 retired-authority pair；
- finality sidecar 已固定 ZUUU 当地日终窗口：2026-07-15 与 2026-07-16 目标在各自 `00:00 Asia/Shanghai` 日终边界前均为 `deferred_event_day_open`，不再提前抓取；
- successor market code-level no-order drill 已 accepted，18 个隔离场景通过，维护备份执行表增量均为 0，receipt=`data/logs/production_reported_x_market_no_order_drill.json`；该 receipt 绑定 disabled activation，未来 bridge 激活后仍须重新生成 activation-bound receipt；
- readiness 为 `safe_fail_closed_accumulating`；物理 `X` live 不受影响，市场 `B`、outbox 与真实订单保持关闭。

后续只按以下顺序推进：

- [ ] 连续积累真实 current-authority X/S pair，禁止历史伪造 forward/as-of 时间；
- [ ] 达到 90/12/12 后生成 candidate artifact；
- [ ] 完成 expanding prequential OOS、季节/全局残差与相关错误审查；
- [ ] 由独立 approval receipt 晋级 production artifact；
- [ ] 在 writer-quiescent 维护窗口原子写 bridge activation，不修改 probability activation；
- [ ] 对维护备份运行 successor market no-order drill并保存内容寻址 receipt；
- [ ] 使用闭合 bridge 重放 `X→S→B`，核验多峰、边缘桶、exclusion certificate 与 UI 单快照；
- [ ] 证明 Transaction B 故障注入全部回滚且执行表零增量；
- [ ] 单独启用 Decision/Execution，采集真实 eligible event 的 `T1→CLOB accepted≤3s`；
- [ ] 交易策略仍遵守动态主 YES/次 YES/物理排除 NO 合同，永不恢复旧 EV/Edge gate。
