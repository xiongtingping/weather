---
document_id: global-station-executable-basket-formula-v1
version: 1.3.15
status: TARGET_PRODUCTION_NO_LIVE
live_order_authorized: false
production_mutation: false
effective_date: 2026-08-26
---

# 45站统一可执行篮子公式

- version: 1.3.15

> **2026-08-05 首批上线覆盖**：篮子只消费已发布的 durable native P0/P0-M/P1 roots；首批可按不少于 3 个合格站点 allowlist 上线，不以 45 站同时 READY 为门。历史 native P0 的 OOS 预约必须在读取 truth 前冻结无标签 input commitments；archive `Last-Modified` 仅为 no-live availability 上界，不是 arrival。未在 allowlist 的站点/lead 保持唯一 BLOCKED terminal，绝不跨 scope fallback。
- status: TARGET_PRODUCTION_NO_LIVE

> **2026-08-26 canonical-day 口径换代（当前有效）**：唯一事实日口径由市场挂牌自身声明的 `settlement_timezone` 决定，逐站逐事件日取自 `basket_target_market_registry`，覆盖OOS、P0/P0-M/P1、TAF、策略、WS与篮子。2026-08-05 的 `America/New_York`（ET）统一口径**仅对该代已停产事实继续有效**——`model`(12,600)、`republisher_oos_artifact`(4)、`native_ensemble_oos_event`(280)、`p0_calibrated_distribution`(3) 均于 2026-08-03 至 08-05 之间停止写入——不再适用于现行链路。现行链路本就按各站市场时区产出（近一周 164,939 份概率信封覆盖 30 种时区，ET 仅占 7.9%），与市场赔付口径一致；45 站中仅 KATL/KLGA/KMIA 三站由市场声明为 ET。`calibration_station_registry.timezone_name` 仍非权威且 45 站中 33 站错标，禁止用它推导、补写或改写 `settlement_timezone`；权威仅两处：挂牌侧 `basket_target_market_registry.settlement_timezone` 与其绑定表 `calibration_station_market_timezone_binding`。旧 ET 代事实原样保留为历史，不改名、不回填、不与新代共用 lineage；跨代比较逐 scope `BLOCKED`。

> **2026-08-05 canonical-day / release override（已由 2026-08-26 换代取代；仅适用该代已停产事实）**：四大方案的唯一事实日口径为`America/New_York`（ET）；`market_registry`的站点地理/历史市场时区仅是原始映射元数据，不能推导、补写或覆盖canonical `settlement_timezone`。旧非ET事实没有逐字段同义与同代lineage时只保留逐scope `BLOCKED` terminal。PR04维持最终发布冻结，PR05–PR16仅可输出`MATERIALIZED_UNPUBLISHED / PRODUCTION_NO_LIVE_ONLY / BLOCKED`；不切current pointer、不让篮子消费、不启动服务或live。

> **2026-08-04 当前执行覆盖**：OOS只用闭日历史strict-as-of数据；未来/forward仅登记`observational_candidate_not_oos`。唯一PR04指针为`output/audits/oos_gap_ledger_45station_current.json`，pointer SHA=`35777f0d3dc8aa8542ea039d118e55bf3f248b56efe007fb6eefcb557294e9c1`，release=`fa3e301bceaa82b57bd74fda5c7825348cb230f2c1001b00f66c7d421cfc9032`，ledger SHA=`b6eda076f0d6c9eef673978a7ce7b0a4ed57c1612c6b78309e2face9e8a961e6`，as-of=`2026-08-03T21:08:00Z`。12,600 records中5,690 OOS_READY、6,910 DATA_MISSING，90/90 MODEL_OOS builders均OOS_SAMPLE_SHORT，activation=0；P0 9站、native linkage 41站及其余typed scope缺root使整体保持BLOCKED。官方native仍优先；Open-Meteo只可作显式republisher no-live fallback，禁止重复抓已保留raw。

> **2026-08-04 W3c执行补充**：V9 canonical SSOT当前为103,889行，已追加CYYZ/NZWN两条Gen1静态republisher no-live activation；它们只证明AIFS Single/ECMWF与ICON deterministic/DWD的同holdout历史selection可被no-live P0候选引用，不代表当前包可用。通用/native activation、P0、P1、formal/live和订单均未新增；forward applicability必须另绑同站同事件日native不可用证据、新鲜Open-Meteo receipt、event/lead/season与activation exact SHA，缺任一项即`FAIL_CLOSED_NO_P0`。

> 2026-08-02T10:26Z执行口径刷新（历史记录）：当时ledger=1,797，后续2,503条混合账本也已退为历史；当前authority只认文首PR04 v2 current root。

> 统一的是计算图、特征定义和硬边界；全局系数是冷启动先验，站点通过严格训练窗内的斜率或数值偏移做层级收缩，不能用一个常数假装改变桶排序。

正式阶段唯一为：校准后的多模型原生集合生成P0；校准后官方 AWC TAF TX确认/有限修正与云量、降水、风物理二次确认原子生成P0-M；官方 AWC 同站`:00` METAR/有效 SPECI生成P1。正式P0必须由至少两套正权重官方原生集合系统逐成员数桶、分别校准偏差/离散度/可靠度后，经跨集合严格历史OOS组合生成；跨模型门按独立系统家族计数，ECMWF PF50、NOAA GEFS、ECCC GEPS、DWD ICON Global/EPS及具备官方历史发布证据的区域/加拿大集合分别计家族。官方原生缺失时，Open-Meteo只可作为显式`aggregator_republisher`历史同日 calibration/no-live fallback：仍需至少两个独立家族、闭日AWC truth、严格OOS与exact activation，且产物必须标记为`REPUBLISHER_NO_LIVE_P0`，不能伪造native arrival、正式P0、P1或formal gate。确定性模型加残差扩散退出P0权威，只保留离线benchmark和ablation，条件未满足即`FAIL_CLOSED_NO_FORMAL_P0`。不存在P0-F。未来日只保留`observational_candidate_not_oos`到达/时序观察，不计入任何OOS或样本门，也不阻断`production_no_live`。每个新版本接替唯一SSOT并重算完整篮子，历史成交事实不改写。市场结算时区来自`market_registry.settlement_timezone`，ET仅用于运营报表。45站（含中国及台湾站）统一使用官方 AWC 全球正式泳道；METAR/SPECI是唯一truth、P1及篮子硬事实，硬事实只剔除纯YES篮子中已数学排除的桶，禁止生成BUY NO；TAF只可按 strict-as-of、原始修订链、有效窗、目标峰值覆盖与 exact-OOS join 进入P0-M。AMSC固定为`retired_history_only`，只保留离线历史审计/对照，不得进入production truth、P1、篮子或伪装为AWC lineage；AWC truth lane 仍禁止同站混源、跨源 fallback 或未复核更正，NWP model lane 的 Open-Meteo fallback 只按前述 lineage 与 OOS 门执行。

> 2026-08-01 09:40Z执行证据：合法P1已增至1,552条/36站；纯YES篮子worker复用已物化后验完成一次no-live原子cycle，因策略/候选证据门产生HOLD/blocked决策，新增order intent=0、order event=0、orders submitted=0。

> 2026-08-01T11:10:48Z追加证据：服务名与no-live activation已受控同步至generation 12；受控worker状态为`COMPLETED_PRODUCTION_NO_LIVE`，目标SSOT为225条P0/43站、45条P0-M/1站、2,067条P1/37站。本轮只有P1 scope满足当前官方WS/库存/订单状态证据，因此不伪造P0/P0-M d2fact；intent零增、order event=0、orders submitted=0，gateway保持只读。

> 2026-08-01T12:17:17Z追加证据：最终45站用户域服务名固定为`com.weather.weather_basket_yes/com.weather.market_ws/com.weather.clob_order_service`；全局生产验收全通过。两枚非零历史YES仓位仍保留，但官方registry已确认其market inactive/non-accepting，故显式分类为`REGISTRY_NONTRADABLE`而不要求不存在的WS book；未解析/歧义映射仍fail closed。全量no-live worker再次`COMPLETED_PRODUCTION_NO_LIVE`，P0/P0-M/P1=225/45/2,096，d2fact=P0 5条/2站、P1 1,448条/37站，新增intent=0、order event=0、gateway未调用。

> 2026-08-01T13:50Z追加证据：target-owned只读投影已对fact、P1 envelope、完整官方WS snapshot及latest label逐项重算内容哈希和decision时刻新鲜度，得到425条source-verified observations（6站）、168条candidate prices与425条counterfactual label-attributions，source blocker=0、数据库/activation/order均零写。RKPK的旧`price_coverage_deficit`与`settled_attribution_deficit`已消失；正式DeltaBasketScore v2晋级只剩HEAVY 30和STANDARD 13个独立事件自然缺口，门槛不放宽。

> 2026-08-01 10:07Z策略证据：P0/P0-M/P1各阶段均已形成45站×3策略族候选与exact-stage只读scorecard；由于P0/P0-M无v2 score rows、P1冻结标签仅覆盖5站，所有strict-OOS/LOSO/跨季节/多窗口门均保持`BLOCKED_EVIDENCE`，不能把候选公式晋级为站点正式后验。

> 2026-08-02 全局推进：AIFS-Single 复用179个已保留官方 raw/wrapper 完成45/45曲线闭日扫描，生成14站×14条合法 AWC closed-truth facts；2站等待truth、29站等待本地日关闭。15Z后TAF/P0-M exact-join覆盖45站/42个P0 scopes/132个样本，最高10、24样本门仍为0个合格scope；AMD/COR未复核日已整日quarantine并保留排除证据。全程未重抓ECMWF/DWD、未写生产P0-M/activation/order。最新45站 no-live worker 已逐scope复核：P0 27条已提交回执、P0-M 90条均为无exact TAF/无P0忽略、P1 27个scope中24个因正式轨迹或COR/AMD复核缺口阻断；三服务仍纯YES、read-only、零订单。

> 2026-08-02T02:00Z补充：139条原生no-live activation中仅55条/42站通过`native_ensemble_activation_oos_linkage`精确OOS绑定，其余84条不得进入篮子概率；统一45站账本固定为990条记录并明确数据、arrival lineage、truth、revision、样本、utility六类缺口。策略层最新scorecard为45站×P0/P0-M/P1、target/reserve/cadence三族，642条冻结label但三族score rows仍为0，故篮子只能消费已物化证据并继续HOLD/fail closed。

> 2026-08-02T21:05Z执行刷新（历史记录）：统一校准receipt已按登记时区与strict maturity拆包并聚合到45站；当时 OOS 账本为1704条事件级记录。当前指针见文首 override；策略scorecard读取1484 facts/666 frozen labels，但P0/P0-M/P1×target/reserve/cadence score rows仍为0，utility direct-fill binding继续`UNBOUND`。

> 2026-08-01T22:54Z闭环回绕：OOS ledger 已升级为 1,797 条，加入官方 TAF revision-chain 的 93 个歧义 station-day（最新内容 SHA `6c54591a0f1d045ec8de2d2975d6fb676e18d9ad0e7a089d62290c1c53a469ac`）；P0-M exact join 最高样本数为 9、24 样本合格 scope 仍为 0。AIFS/ICON 继续分别按官方 native lineage 积累，Open-Meteo ICON/AIFS/区域模型仍仅 `aggregator_republisher` candidate。策略 utility v2 读取 1,484 facts/686 frozen labels，但三阶段三族 score rows 全为 0、direct fill binding=`UNBOUND`；因此篮子只消费已物化 P0/P0-M/P1 与官方 AWC/CLOB 证据，缺证据继续 HOLD/fail closed。

> resident P1 replay 只读审计同时记录 17 个重放缺口与 6 个同刻歧义，FORMAL_PROMOTED 轨迹绑定为 0；任何篮子公式/站点策略都不能将 stale P1 或未绑定 artifact 当作可消费概率。

> 接入 resident replay binding 后的统一账本内容 SHA：`9f962a61ed3e87e260361661d92d6ca8aba7ae543163cc1b5d9fd84380f23f4f`。

> 本轮 no-live worker 回执纳入后，统一账本最新内容 SHA：`9067504ce7c0dd131bf6f111d85131630cff88376ea9a24cb65e0185f35d56fa`。

> worker 同一 as-of 的 resident replay 回执刷新后，统一账本最新内容 SHA：`cc48eb1344b76e2a184f9685934d3cdccb0dfe42c96246dae56480005c5f979f`。

> 账本对缺失 replay receipt 的 clean-checkout 可运行性修补后，最新内容 SHA：`6b53ac80525e2810240287268b646fd57be1710e754cea5b9b2ffff9abf0ae80`。

> 2026-08-02T00:45:38Z 证据指针（历史记录）：当时统一账本为 `output/audits/oos_gap_ledger_45station_p1_atomic_20260802.json`（1,797条）；该旧快照不覆盖当前 deterministic+republisher ledger，当前唯一指针见文首 override。

## 1. 直接结论

- 冻结样本覆盖45站、306个三模型事件、15,158笔精确签名YES BUY订单和8,465,432个CLOB分钟参考价点。
- 初始成篮Holdout：支持集Precision为79.6%、Recall为69.6%；篮子桶数MAE为0.92桶。
- 动态补仓共享公式Holdout Top-1为34.3%，加入站点收缩斜率后为32.1%，随机基线28.9%。
- 偏置资金权重Holdout MAE：共享0.247、站点收缩0.243、等权基线0.268。
- 保留价Holdout MAE：共享公式0.0227，站点收缩0.0229 USDC/份。
- clip金额Holdout MAE：共享公式4.34，站点收缩4.32 USDC。
- 补仓间隔Holdout MAE：共享公式2119.4，站点收缩2119.3秒。
- 两级晋级门最终激活：初始选桶=global、桶数=global、偏置权重=uniform、动态补仓=equal_inventory_deficit_only、保留价=reference_price、clip=train_median_cash、节奏=global_ridge。
- 站点偏置权重在7月14-16日晋级片段胜出、但在7月17日最终审计输给等权，因此已自动降级；动态下一补桶则由等份库存缺口单独式胜出，说明“补最欠配桶”比“追概率变化”更稳定。
- 以上历史指标只作为研究基线，不再构成生产执行公式或参数晋级证据；生产唯一目标函数是严格`ΔBasketScore v2`，且当前不携带live下单授权。

## 2. 统一执行方程

```text
stage_t = latest_valid(P1, else P0-M, else P0)
p_t(b)  = committed ProbabilityEnvelope(stage_t, lineage, as_of=t)

A_t     = OfficiallyPossibleBuckets(hard_fact_t, bucket_contract)
p_A(b)  = 0                                      if b not in A_t
          p_t(b) / Sum_{j in A_t} p_t(j)         otherwise

M       = argmax_b p_A(b)
L_center= p_A(M) - second_highest_b p_A(b)
D       = Sum_{b>M} p_A(b) - Sum_{b<M} p_A(b)
direction = NEUTRAL                              if L_center < tau_center
            sign(D)                              otherwise

E_t     = calibrated diagnostic-model extrema    # 只诊断分歧
C_t     = bounded_candidate_search(p_A,E_t,max_two_layers_each_side)
S_t     = sparse_support_argmax(p_A,C_t,reliability,coverage,price_value)
          subject to S_t ⊆ C_t ⊆ A_t

q*_t    = argmax_q J(p_A,q,official_WS_depth,inventory,risk,budget)
Deltaq_b= max(q*_t(b)-q_filled(b)-q_confirmed_pending(b),0)

J(q)=E[PnL(q)]
     + lambda_floor*ProfitFloor(q)
     - lambda_down*Downside(q)
     + lambda_short*ShortfallCoverage(q)
     + lambda_break*BreakthroughCoverage(q)
     - lambda_conc*Concentration(q)
     - lambda_exec*ExecutionRisk(q)

DeltaBasketScore(bundle)=J(q_after_expected_fill)-J(q_filled)
                         - normalized_execution_cost(bundle)

TRADE(bundle)=all_hard_gates
              and complete_maker_leg_for_every_positive_gap
              and DeltaBasketScore(bundle)>epsilon
```

关键语义：

- 运行时禁止`proxy_probability`；只能消费已物化P0/P0-M/P1及其lineage。
- 模型极值决定分歧诊断；候选边界只表示最多搜索到哪里；最终支持集由组合概率和联合经济目标决定。端点之间不自动填满，离群模型不强制扩桶。
- 冷暖方向由组合概率侧质量决定；组合至少包含两个正权重原生集合系统，权重只能由离线严格OOS artifact更新；单套集合的成员数不能替代跨集合独立性。
- 价格不得通过`1/ask`或`reservation_value`反写概率或支持集；低价只通过完整结算利润曲线、覆盖价值、集中度和真实深度影响联合目标。
- 当前目标库存是一个完整篮子。全部正库存缺口必须在同一原子Maker bundle中出现，禁止先选择或提交单桶；成交后按实际份额重算整个`q*`和`ΔBasketScore`。
- 立即执行/FOK路径仍要求选中集合逐档`sum(all_in_yes_ask)<1`；未成交staged GTC可在该合计之外独立挂单，但不计入已实现地板。紧迫FOK只允许在Maker撤销确认后处理单桶剩余缺口。

## 3. 七个可执行模块

| 模块 | 输入 | 输出 | 硬边界 |
|---|---|---|---|
| ProbabilityAuthority | 已物化P0/P0-M/P1、lineage、stage接替 | 唯一`p_t(B)` | 禁止代理概率、裸降级或执行层改概率 |
| SupportCompiler | `p_A`、模型分歧诊断、可靠性、价格覆盖价值 | `M/L_center/D/C_t/S_t` | `S⊆C⊆A`；不Top-K、不填满端点、不按极值定支持集 |
| TargetInventory | `S_t`、概率、WS深度、库存、预算和风险 | 联合`q*`与情景PnL | 不等份、不固定桶数、不拆角色分别配仓 |
| ReserveAndCadence | 数学保留价、OOS站点策略、时点/盘口变化 | 每腿Maker上限与重评估门 | 只限制执行，不授权单腿或改变支持集 |
| CompleteBundle | 全部正库存缺口及逐档官方WS | 原子多腿Maker bundle | 缺任一腿、预算或深度即整篮HOLD |
| DeltaBasketScoreV2 | baseline、预期fill分布、PnL、覆盖、集中度、执行成本 | 唯一增量分数 | `Δ≤epsilon`即HOLD；禁止其他打分器授权 |
| ReconcileAndLabel | 回报、实际fill、资本、结算truth | 新库存、training fact、label | 未知/迟到/冲突先RECONCILE；不得猜测重发 |

## 4. 历史行为复刻结果（非生产参数）

| 城市/站点 | 初始Precision | 桶数MAE | 偏置权重MAE/等权 | 动态Top-1 共享/站点/随机 | 保留价MAE 共享/站点 | clip MAE 共享/站点 | cadence MAE秒 共享/站点 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Chengdu/ZUUU | 91.7% | 1.25 | 0.238/0.153 | 45.0%/42.2%/21.9% | 0.020/0.020 | 5.06/4.91 | 1432/1423 |
| Qingdao/ZSQD | 91.7% | 1.75 | 0.204/0.148 | 33.5%/34.9%/19.8% | 0.022/0.026 | 5.25/4.87 | 1488/1487 |
| Guangzhou/ZGGG | 81.8% | 1.25 | 0.236/0.268 | 18.3%/17.9%/23.0% | 0.022/0.021 | 4.49/4.46 | 1927/1929 |
| Beijing/ZBAA | 84.6% | 0.75 | 0.313/0.281 | 31.8%/15.0%/24.5% | 0.025/0.025 | 4.79/4.87 | 1979/1970 |
| Taipei/RCSS | 69.2% | 0.50 | 0.303/0.301 | 28.1%/27.1%/29.3% | 0.020/0.021 | 4.80/4.73 | 1975/1971 |
| Tokyo/RJTT | 63.6% | 1.50 | 0.326/0.320 | 35.6%/29.3%/28.7% | 0.018/0.020 | 4.17/4.14 | 1864/1871 |
| Shanghai/ZSPD | 76.9% | 1.00 | 0.272/0.264 | 38.7%/34.9%/31.5% | 0.026/0.026 | 4.89/4.85 | 1967/1981 |
| Shenzhen/ZGSZ | 100.0% | 1.50 | 0.193/0.157 | 17.2%/16.1%/25.8% | 0.019/0.019 | 4.30/4.22 | 2049/2056 |
| Cape Town/FACT | 90.0% | 1.50 | 0.271/0.208 | 29.9%/16.4%/26.3% | 0.019/0.019 | 4.22/4.26 | 2853/2845 |
| Chongqing/ZUCK | 83.3% | 0.25 | 0.281/0.262 | 18.2%/17.1%/27.2% | 0.022/0.023 | 4.89/4.83 | 1907/1906 |
| San Francisco/KSFO | 88.9% | 1.00 | 0.331/0.236 | 50.0%/51.2%/22.7% | 0.019/0.018 | 4.37/4.02 | 1443/1438 |
| Seoul/RKSI | 61.5% | 0.50 | 0.224/0.242 | 28.8%/30.7%/29.7% | 0.024/0.024 | 4.69/4.61 | 1924/1931 |
| Wellington/NZWN | 81.8% | 0.50 | 0.143/0.398 | 59.1%/56.5%/34.5% | 0.035/0.035 | 3.92/3.69 | 2211/2201 |
| Buenos Aires/SAEZ | 66.7% | 1.00 | 0.504/0.295 | 31.2%/29.2%/29.4% | 0.017/0.016 | 4.34/4.49 | 1710/1699 |
| Busan/RKPK | 90.0% | 1.00 | 0.222/0.333 | 19.5%/19.5%/27.9% | 0.025/0.025 | 3.92/3.98 | 2531/2543 |
| Amsterdam/EHAM | 88.9% | 0.33 | 0.114/0.225 | 33.3%/43.1%/29.3% | 0.021/0.021 | 4.68/4.60 | 2236/2229 |
| Seattle/KSEA | 66.7% | 1.00 | 0.140/0.184 | 24.3%/12.5%/25.8% | 0.021/0.021 | 5.07/5.12 | 1492/1493 |
| Kuala Lumpur/WMKK | 60.0% | 0.50 | 0.528/0.376 | 36.5%/35.1%/33.2% | 0.025/0.026 | 3.67/3.69 | 2640/2641 |
| Toronto/CYYZ | 88.9% | 0.67 | 0.269/0.308 | 33.3%/28.5%/29.2% | 0.028/0.026 | 4.00/3.98 | 1525/1525 |
| Atlanta/KATL | 87.5% | 1.00 | 0.327/0.253 | 42.7%/35.0%/35.7% | 0.021/0.021 | 3.79/3.81 | 1789/1786 |
| Singapore/WSSS | 75.0% | 0.75 | 0.152/0.310 | 53.8%/55.9%/32.9% | 0.022/0.024 | 3.79/3.60 | 2668/2662 |
| Sao Paulo/SBGR | 87.5% | 0.33 | 0.093/0.304 | 33.1%/27.5%/27.3% | 0.021/0.020 | 3.61/3.88 | 1993/1976 |
| Wuhan/ZHHH | 100.0% | 0.75 | 0.369/0.237 | 40.8%/38.0%/33.3% | 0.018/0.018 | 3.59/3.57 | 2570/2563 |
| Houston/KHOU | 75.0% | 1.00 | 0.123/0.330 | 20.2%/21.7%/26.8% | 0.024/0.025 | 3.33/3.49 | 1745/1747 |
| Ankara/LTAC | 100.0% | 1.50 | 0.249/0.206 | 59.1%/49.6%/28.5% | 0.015/0.014 | 4.91/5.18 | 2587/2582 |
| Karachi/OPKC | 83.3% | 0.25 | 0.219/0.283 | 31.1%/33.6%/36.6% | 0.028/0.029 | 3.86/3.88 | 2956/2957 |
| Manila/RPLL | 88.9% | 1.00 | 0.282/0.266 | 36.9%/32.8%/32.4% | 0.023/0.025 | 4.02/4.01 | 3423/3467 |
| Dallas/KDAL | 62.5% | 0.67 | 0.381/0.349 | 29.2%/28.3%/29.1% | 0.026/0.024 | 4.17/4.01 | 1641/1644 |
| Munich/EDDM | 85.7% | 1.67 | 0.368/0.213 | 38.5%/43.6%/23.1% | 0.025/0.024 | 4.56/4.69 | 1689/1689 |
| Nyc/KLGA | 55.6% | 0.67 | 0.354/0.275 | 23.7%/21.9%/26.6% | 0.027/0.028 | 4.47/4.36 | 1790/1781 |
| Austin/KAUS | 62.5% | 1.00 | 0.168/0.291 | 25.7%/26.5%/26.7% | 0.025/0.023 | 3.87/3.78 | 2104/2102 |
| Chicago/KORD | 60.0% | 1.00 | 0.152/0.381 | 36.4%/32.7%/35.8% | 0.027/0.026 | 3.93/4.03 | 1930/1930 |
| Los Angeles/KLAX | 62.5% | 0.00 | 0.143/0.292 | 35.9%/35.9%/34.5% | 0.027/0.027 | 3.93/4.11 | 2143/2139 |
| Paris/LFPB | 85.7% | 1.67 | 0.077/0.230 | 31.0%/42.0%/24.6% | 0.023/0.023 | 4.33/4.47 | 2183/2183 |
| Lucknow/VILK | 63.6% | 1.25 | 0.247/0.286 | 55.2%/47.9%/31.6% | 0.020/0.019 | 4.38/4.41 | 2328/2331 |
| London/EGLC | 100.0% | 2.00 | 0.191/0.182 | 29.3%/33.7%/26.0% | 0.024/0.023 | 4.42/4.43 | 2449/2452 |
| Milan/LIMC | 70.0% | 0.67 | 0.070/0.374 | 15.6%/16.7%/27.2% | 0.023/0.023 | 4.58/4.52 | 2149/2151 |
| Warsaw/EPWA | 62.5% | 1.00 | 0.328/0.262 | 48.8%/47.6%/37.3% | 0.021/0.020 | 4.56/4.62 | 2386/2381 |
| Mexico City/MMMX | 62.5% | 0.67 | 0.099/0.500 | 53.6%/42.9%/45.2% | 0.021/0.022 | 3.09/3.39 | 2975/2943 |
| Madrid/LEMD | 100.0% | 0.33 | 0.266/0.228 | 36.1%/26.5%/34.7% | 0.022/0.022 | 4.49/4.49 | 2753/2758 |
| Panama City/MPMG | 71.4% | 1.33 | 0.132/0.311 | 25.6%/25.6%/30.6% | 0.022/0.030 | 4.07/4.01 | 2493/2478 |
| Jeddah/OEJN | 88.9% | 0.33 | 0.071/0.341 | 41.9%/44.6%/41.2% | 0.026/0.026 | 4.68/4.67 | 2936/2966 |
| Helsinki/EFHK | 87.5% | 0.67 | 0.234/0.206 | 30.4%/26.1%/27.3% | 0.021/0.021 | 4.27/4.40 | 3440/3466 |
| Miami/KMIA | 50.0% | 1.00 | 0.111/0.400 | 42.4%/42.4%/43.7% | 0.035/0.034 | 3.63/3.81 | 3141/3149 |
| Denver/KBKF | 62.5% | 1.00 | 0.540/0.400 | 33.8%/47.7%/37.7% | 0.020/0.018 | 4.23/4.34 | 3824/3818 |

## 5. 证据与限制

- 精确签名订单给出创建时间、限价、原始USDC和原始份数；这比仅看链上成交更接近下单意图。
- CLOB prices-history是分钟参考价，不是历史可执行ask/bid/depth或队列；B13/B14 live前仍必须使用官方WS实时逐档深度。
- 三个确定性模型代理的旧计算只保留为离线benchmark/ablation；不得进入P0、支持集或交易特征。
- 未成交、撤单和源账户私有pending不可见，所以保留价只恢复到已签名limit层，不能证明是账户最终效用上限。
- 初始/动态选桶的站点常数会在同一choice set内完全抵消，因此必须使用站点斜率，尤其是相对模型中心的有符号偏移；保留价、金额、桶数和节奏才允许数值截距。
- 历史等份/Top-1结果仅保留为基线；生产目标库存必须由完整联合目标求解，不能把行为复刻赢家直接激活。
- 45站只有2026-07-10至07-17八个夏季站点日；它足以做时间外行为复刻，但不足以声称跨季节长期站点模型偏差已建成。

> 2026-08-01T22:41:27Z 运行证据：45站策略 scorecard 读取 1,484 facts/686 frozen labels；P0、P0-M、P1 × target/reserve/cadence score rows 全为 0。utility v2 的 metric/evidence 声明已定义，但实际 candidate/baseline、官方 WS、成交及结算绑定仍 `UNBOUND`，不可把请求量、模拟回执或天气概率当作策略 utility。

## 6. 下一开发门

完成45站真实纵向闭环：`registry → calibration → P0 → P0-M/P1 → 官方WS → inventory/order-state → ΔBasketScore v2完整Maker bundle → decision → training fact → label`。每站必须以SSOT行数、resident消费回执和低→高→低多次回绕证明；未来日/forward只作`observational_candidate_not_oos`时序证据，不属于任何OOS、不计入历史严格OOS且不阻断`production_no_live`物化，只阻断正式参数/live晋级。
