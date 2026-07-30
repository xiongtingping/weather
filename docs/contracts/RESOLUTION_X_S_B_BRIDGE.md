---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
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
3. `X/S` pair、strict-as-of、OOS 或样本阈值不是 live gate。获批的零样本运营基线可以生成明确标注“未校准”的软 `p(S)`/`p(B)`；这些证据只控制后续 calibrated successor 的晋级。bridge/context 缺失或损坏仍只令市场分支 fail closed，不得阻断 AMSC raw/source/canonical 入库、StrongUpdate、checkpoint、public `X` SSOT 或 final-X 日标签。
4. 已观测 AMSC 温度只能形成 `X` 的硬下界。低概率、PMF 截断、软不可达、温度动量、轨迹、云雨和运营基线的软残差概率都不能直接授权 hard NO。Target Realization v6 的 hard-exclusion NO 只接受绑定 exact checkpoint、物理硬支持 `H_X`、authority-proven 残差硬支持 `H_D`、bridge/target SHA 与 bucket 边界，并逐组合证明 `∀x∈H_X,∀d∈H_D: bucket(x+d)≠b` 的内容寻址证书；策略额度绝对上限为 200 USDC。

当前 Target Realization v6 不改变 bridge 的证据含义：trajectory NO 只能读取 committed soft `p(B)` 的独立 `q_NO`，不能生成 hard certificate；hard-exclusion NO 仍须上述全支持逐组合证书。200 USDC 只是资本上限，允许统一 `capital_scale_factor` 缩量，不是 bridge 输出或必须足额金额。
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
Decision Core    dynamic main YES / second YES / one trajectory-or-hard NO slot
```

进入 market Transaction B 前必须完成四方内容绑定：physical resolution contract 的 `resolution_contract_id` 必须等于当前 checkpoint，且其 authority id/SHA、settlement source、resolution local date 与 immutable Target contract、获批 Bridge artifact 完全一致。任何一项漂移只允许将 market projection 标记为 NA 并继续提交同一 v6 physical-X；不得生成 B、RealizationState 或 outbox，也不得阻断 AMSC 实况和物理后验。

Bridge 不拥有天气推理、交易策略、资金、价格判断或 CLOB。Decision Core 不得重算 `p(X)`、kernel、`p(B)` 或物理排除。

## 4. 双层 Bridge 合同

生产只允许两个互斥层级：

```text
Operational baseline（立即可用）
  固定零样本、不可在线学习、未校准
  → 允许软 p(S|X) / p(B)、YES 排名与 trajectory-NO q_NO 分档
  → sample/OOS required_for_live=false
  → hard exclusion certificate 永久禁止

Calibrated successor（证据成熟后整体替换）
  current-authority X/S pair + chronological OOS
  → 独立审批后原子替换 baseline
  → 经验 PMF 仍不自动构成确定性残差硬包络
```

运营基线由 `config/resolution_authorities/zuuu_x_s_soft_projection_operational_v1.json` 明确授权。它绑定 v4 source authority，但不会把 v4 的 shadow 数据采集合同篡改成 production 权威；其 scope 只允许 soft production projection，并明确不改变 `p(X)`、不声明校准、不直接授权订单/CLOB、不授权硬 NO。baseline kernel 名称固定为 `same_station_sample_free_operational_residual_prior_v1`，任何 observed pair 注入均拒绝；真实 pair 只能进入 calibrated candidate。该 soft kernel 必须保留显式双侧尾部质量，禁止用有限 `±2℃` 截断把边缘桶制造成精确 0%；没有 authority hard-support certificate 时，边缘桶只能是极低概率，不能宣称物理不可能。

### 4.1 校准 successor 数据合同

pair 唯一来源是 isolated `data/shadow/resolution_final_labels.db`：每个 event day 最多一个 active authority pair，并同时保留 X/S 两端 source-observation ID、raw ref、raw SHA、target contract SHA 与 authority SHA。append-only registry 中的旧 authority pair只保留审计，不计入当前覆盖。

market finality 只证明冻结 target 的 Gamma event 已 closed 且全部 market 为 resolved，不读取胜出 outcome、价格、持仓或盈亏。finality poll 的最早时刻固定为 `resolution_local_date + 1 day 00:00 Asia/Shanghai`；在 ZUUU 当地物理日结束前必须返回 `deferred_event_day_open` 且禁止网络请求。日终后的 `URLError/TimeoutError` 属于可重试采集状态，不得阻断物理 `X`、实时 posterior 或 shadow registry；原文 JSON、slug、日期、resolution source、全桶 resolved 等结构错误仍 fail closed，禁止降级成 pending。

开始拟合前至少需要：

- 90 个不同 event day；
- 12 个不同 physical `X`；
- 12 个不同 settlement `S`。

`resolution_bridge_calibration_runtime.py` 只离线拟合离散残差 `D=S-X`：全局采用零中心离散 Laplace 收缩；季节样本达到 30 日才产生季节 PMF，否则回退到全局 PMF。评估使用 chronological expanding prequential OOS，至少 18 个评估日，并报告 log loss、MAE 与 CRPS。达到覆盖只表示可进入校准审查，不表示 artifact 获批或 bridge 已闭合。

生产 artifact 固定 schema `resolution_x_s_bridge_artifact_v1`。运营基线必须额外绑定 operational projection authority ID/SHA、`sample_count=0`、概率支持非硬包络和 `hard_exclusion_certificates_authorized=false`；校准 successor 必须：

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
| 零样本运营基线 | `resolution_bridge_operational_baseline_runtime.py` |
| 软投影运营授权 | `resolution_operational_projection_authority_runtime.py` |
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

缺少或未批准 bridge/context 时，v6 Transaction B 继续原子提交 physical-only `X`，明确写 `market_projection_na`，并结构性禁止市场 RealizationState/outbox；该降级不能阻断物理 X。

闭合后，`production_reported_x_market_transaction_writer.py` 必须在一个 `BEGIN IMMEDIATE` 中完成：StrongUpdate、reported-X checkpoint、RealizationState、public fact/latest、posterior outbox 与 `posterior_outbox_execution_contracts`。提交前必须重验 active probability/bridge activation、前驱 checkpoint/run/posterior SHA CAS、P0 冻结上下文、唯一初始 lineage、realization 内容哈希/内容寻址 ID、public `X` 与 checkpoint 完全一致、bridge/target SHA、归一 `p(B)` 及直接报文桶为空。任一步失败整笔回滚；writer 不含网络、签名或 CLOB。

## 6. 状态机

```text
source authority v4 + operational soft-projection authority
→ immutable zero-sample baseline candidate
→ independent baseline/no-hard-NO review
→ production_approved operational artifact
→ separate orderless bridge activation
→ market projection ready
→ no-order Transaction-B drill accepted
→ Decision/Execution 独立切换与 T1→CLOB 前向验收

parallel calibration lane:
X/S pairs → coverage/OOS → calibrated candidate → independent approval
→ atomic full replacement of operational baseline
```

任何阶段不得跳级。artifact 或 activation 漂移、target contract 不匹配、`p(B)` 不归一、direct `X→B`、CAS/lineage/hash 不一致均 fail closed，但不得删除已提交 canonical 或物理 `X`。

## 7. 当前状态与后续 checklist

截至 2026-07-16：

- production v6 `pre-event Predict → current-event emission → hard condition → reported-X projection` 已是唯一概率主线，v5 fallback=false；
- 零样本 soft operational bridge、独立 projection authority 与 daily market context 已内容寻址并激活，`p(B)` 已进入同一 Transaction-B SSOT；
- 当前 authority 有效 pair 为 1 日、1 个 X、1 个 S，另有 1 条 retired-authority pair；
- finality sidecar 已固定 ZUUU 当地日终窗口：2026-07-15 与 2026-07-16 目标在各自 `00:00 Asia/Shanghai` 日终边界前均为 `deferred_event_day_open`，不再提前抓取；
- activation-bound v6 market no-order drill 已 accepted；soft baseline 保留双侧尾部且不生成 hard-NO certificate；
- 动态三腿由 Target Realization v5 与内容寻址 strategy/cutover activation 授权；ADR-0004/v4 从未激活且已 superseded。训练/OOS/bridge 样本不足仍不阻断已激活 soft projection 或物理概率主线。

后续只按以下顺序推进：

- [x] 固定零样本运营基线、独立 soft-projection authority、样本非 live gate 与“soft probability 不得伪造 hard-NO”合同；
- [x] 物化内容寻址 baseline artifact、approval receipt 与 daily market context；
- [x] 使用隔离 candidate activation 运行 v6-native Transaction-B 零订单演练；
- [x] 原子写 bridge/context activation，不修改 probability activation，不暂停或阻断 AMSC；
- [x] 使用 active bridge 投影 `X→S→B`，核验多峰、边缘桶、无伪 hard exclusion certificate 与 UI 单快照；
- [x] 证明 Transaction B 故障注入全部回滚且执行表零增量；
- [x] 原子实现并激活 Target Realization v5 Decision/Execution；[ ] 采集首个真实 eligible event 的 `T1→exchange POST start≤3s` 前向证据；
- [ ] 并行连续积累真实 current-authority X/S pair，达到 90/12/12 后完成 expanding prequential OOS 与 calibrated successor 审批；该通道永不阻断 active baseline live；
- [ ] 交易策略仍遵守动态主 YES/次 YES/物理排除 NO 合同，永不恢复旧 EV/Edge gate。
