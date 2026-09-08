---
document_id: station-basket-policy-hierarchy-v1
version: 1.4.7
canonical_format: markdown
status: TARGET_PRODUCTION_NO_LIVE_RUNNING
live_order_authorized: false
effective_date: 2026-08-26
---

# 全局篮子先验 → 站点策略后验

- version: 1.4.7

> **2026-08-05 策略冷启动当前有效覆盖**：正式策略父根必须来自至少两套官方原生集合的历史 OOS native P0；历史 archive availability proof 不授予实时 arrival 或 live authority。PR14 首次上线采用形式验证过的保守纯 YES baseline，真实 fill/settlement utility 只用于学习策略后续晋级，不再作为首批上线前置条件。P1_BASE 由官方实时 AWC METAR/SPECI 生成，历史轨迹增强不阻断基础 P1。
- status: TARGET_PRODUCTION_NO_LIVE_RUNNING

> **2026-08-26 canonical-day 口径换代（当前有效）**：唯一事实日口径由市场挂牌自身声明的 `settlement_timezone` 决定，逐站逐事件日取自 `basket_target_market_registry`，覆盖OOS、P0/P0-M/P1、TAF、策略、WS与篮子。2026-08-05 的 `America/New_York`（ET）统一口径**仅对该代已停产事实继续有效**——`model`(12,600)、`republisher_oos_artifact`(4)、`native_ensemble_oos_event`(280)、`p0_calibrated_distribution`(3) 均于 2026-08-03 至 08-05 之间停止写入——不再适用于现行链路。现行链路本就按各站市场时区产出（近一周 164,939 份概率信封覆盖 30 种时区，ET 仅占 7.9%），与市场赔付口径一致；45 站中仅 KATL/KLGA/KMIA 三站由市场声明为 ET。`calibration_station_registry.timezone_name` 仍非权威且 45 站中 33 站错标，禁止用它推导、补写或改写 `settlement_timezone`；权威仅两处：挂牌侧 `basket_target_market_registry.settlement_timezone` 与其绑定表 `calibration_station_market_timezone_binding`。旧 ET 代事实原样保留为历史，不改名、不回填、不与新代共用 lineage；跨代比较逐 scope `BLOCKED`。

> **2026-08-05 canonical-day / release override（已由 2026-08-26 换代取代；仅适用该代已停产事实）**：唯一事实日、OOS、P0/P0-M/P1、策略utility和篮子scope统一为`America/New_York`（ET）。`market_registry`的站点地理/历史市场时区不再是canonical日界权威，且不能被用来回填、改名或跨表兼容旧事实；无一一语义/lineage的旧行只输出`BLOCKED` terminal。PR04保持冻结，未发布P0/P0-M/P1/basis不得被策略或篮子消费。

> **2026-08-02 当前策略边界**：站点策略只消费已物化、带 lineage 的 P0/P0-M/P1 与官方 AWC/CLOB 证据。所有 OOS 与 utility 晋级样本使用闭日历史 strict-as-of 数据；未来/forward 一律 `observational_candidate_not_oos`，仅作观测，不计 OOS、不阻断 `production_no_live`。官方原生模型缺失、无效、过期或迟到时，才允许使用 Open-Meteo `aggregator_republisher` no-live fallback；其 lineage、native-arrival/P1/formal 禁入边界和独立历史 OOS activation 必须完整。utility score rows 在真实 candidate/baseline、WS、成交/零成交回执、结算和 reserve/cadence opportunity-cost 证据绑定前保持 0。

> **2026-08-04 current-root override（当前有效）**：策略层唯一上游账本为`output/audits/oos_gap_ledger_45station_current.json`，pointer SHA=`35777f0d3dc8aa8542ea039d118e55bf3f248b56efe007fb6eefcb557294e9c1`，release=`fa3e301bceaa82b57bd74fda5c7825348cb230f2c1001b00f66c7d421cfc9032`，ledger SHA=`b6eda076f0d6c9eef673978a7ce7b0a4ed57c1612c6b78309e2face9e8a961e6`，baseline identity=`a95ae2ef6d2cd9128b78b76d0e204c22fc6dd63b08ea547866cd8ce23b35c7e4`。当前activation=0，STRATEGY_UTILITY typed scope为`BLOCKED(ACTIVATION_ROOT_MISSING)`，因此不得从5,690条OOS_READY模型记录推断策略后验、score或order intent；旧2,503条混合账本仅保留历史引用。

> **2026-08-04 W3c策略边界补充**：V9中新增的CYYZ/NZWN两条Gen1 `republisher_no_live_activation`是模型层静态历史证据，尚未进入PR04 current release，也不是策略层可消费的P0/P0-M/P1 root。它们不含forward freshness、native-unavailable、WS或utility；因此`STRATEGY_UTILITY`、站点后验、target/reserve/cadence score、intent和order状态均不改变，继续fail closed。只有后续独立P0 snapshot进入统一release并满足策略/WS/utility全链合同后，策略层才可消费。

> 2026-08-02T10:26Z策略回绕（历史记录）：此前1,797及后续2,503条账本均已退为历史；当前统一指针只认文首PR04 v2 current root。官方AWC closed labels、utility direct-fill与score rows仍按真实绑定状态判定。

> 统一公式不再作为所有城市的直接执行公式；它只承担冷启动先验、层级收缩中心和缺样本回退候选，各站点分别学习目标仓位、保留价和补仓节奏。

> 本合同受`bayes_basket_target_architecture_v1.md`冻结生产宪章约束；RKPK-only门已解除，canonical 45站按资源有界轮转纵向推进。每站必须分别证明`MATERIALIZED＋RUNNING＋确定性回绕`，不能以45站decision壳、全局fallback或对象数量冒充闭环。未来日时序观察并行运行但不是OOS，不阻断`production_no_live`或扩站，只控制参数、权重及live晋级。

> 2026-08-01T11:10:48Z 运行边界：`com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service`仅在`gui/501`用户域运行，旧`com.weather.reported_trajectory_no`与system同名服务均为0；`production_no_live`、live pause、只读gateway、零订单保持。动态快照为225条P0（43站）、45条P0-M（仅RKPK）和2,067条P1（37站）；d2fact仍只有P1（992条/36站），因本轮当前可执行scope未出现无P1且同时具备正式WS/库存/订单状态证据的合法P0/P0-M站点日，不得伪造stage-zero事实。受控worker已完成`COMPLETED_PRODUCTION_NO_LIVE`原子cycle/decision/P1 training fact，新增intent和order event均为0；`basket_policy_promotion`仍为空，formal/live promotion继续阻断。当前只是exact-stage no-live骨架与fail-closed链运行，不得写成45站DONE。

> 2026-08-01T12:17:30Z 刷新：全量no-live worker完成且零新intent/零order event；当前d2fact为P0 5条/2站与P1 1,448条/37站，标签表643条。exact-stage scorecard读取1,452条fact，仅425条具有合法冻结标签，P0/P0-M/P1的target/reserve/cadence score rows仍全为0，故`basket_policy_promotion=0`且继续`BLOCKED_EVIDENCE`。LEMD 2026-07-31的35条P1 d2fact缺正式truth，同日官方AWC存在COR，必须先复核，不得为补标签而绕过。

> 2026-08-01T13:50Z 语义闭环：exact-stage scorecard刷新为1,470条fact/425条冻结label，并明确区分“自然样本缺口”和“非时间型utility合同缺口”。425条已标注旧P1事实均为`LEGACY_FACT_NO_OUTCOME_MATERIALIZATION`；三族score rows仍为0，因为target尚缺候选/基线目标库存＋同一WS成本＋结算utility合同，reserve尚缺真实fill utility，cadence尚缺时序机会成本合同。未来新fact只原子记录这三个`UNAVAILABLE`缺口，禁止在decision时自报candidate/baseline score、生成第二桶概率、用Brier/Delta/action/天气代理冒充分数或回写历史事实；因此promotion继续false。

> 2026-08-02 全局推进：45站不再以RKPK作完成门。AIFS-Single闭日扫描已复用179个官方保留对象生成14站训练事实（2站等待truth、29站等待当地日关闭）；TAF/P0-M exact-join覆盖45站、42个P0 scope、132个样本，最高10且24门为0合格，AMD/COR未复核日整日停牌。最新no-live worker按完整registry运行，P0 27条已提交、P0-M 90条只读忽略回执、P1 27个scope中24个阻断；策略层仍保持三族score rows=0，直至candidate/baseline库存、同一WS成本、fill/settlement utility和cadence机会成本合同齐备。

> 2026-08-02T02:00Z策略审计固定完整口径：45站P1 dry-run为0 eligible、45 blocked；最新exact-stage scorecard读取1482 facts/642 frozen labels，P0、P0-M、P1 × target/reserve/cadence score rows均为0，不能从天气、quote、零成交或旧label推断utility。站点策略只继续记录缺口合同和证据指针，不能生成正式后验或改变上游概率。

> 2026-08-02T12:00Z utility 回绕：exact-stage scorecard 已读1484 facts/666 frozen labels；45站×P0/P0-M/P1×target/reserve/cadence score rows仍为0。官方 CLOB snapshot 4323条、candidate order intents 685条、candidate execution cycles 9892条均只作未绑定候选证据；confirmed execution receipt=0，direct fill binding=UNBOUND，不能把零成交推成零填充或 utility。promotion 继续 false，production_no_live 继续零订单。

> 2026-08-02T21:05Z策略链回绕：45站 scorecard 与 calibration/OOS receipts 已重新对齐；官方 WS、candidate intent/cycle 仍是候选证据，实际 execution receipt/order event 均为0，三族 utility score rows 继续保持0。未补齐 candidate-vs-baseline、真实确认成交、结算 utility 与 cadence opportunity-cost 前，不生成站点后验、不改变上游P0/P0-M/P1。

> 2026-08-01T22:54Z全局策略证据：统一 OOS ledger 为 1,797 条（含 93 条 TAF revision-chain 歧义行；最新内容 SHA `6c54591a0f1d045ec8de2d2975d6fb676e18d9ad0e7a089d62290c1c53a469ac`），45 站策略仍只读运行；utility contract 已定义但真实 direct-fill/settlement/cadence 证据未绑定，score rows=0、promotion=false。LEMD 2026-07-31 的 AWC COR/AMD sequence 冲突继续整日隔离，不能为补 label 选择任一 predecessor；所有模型、P0-M、P1 缺口仍按站点级 lineage 处理，不以 RKPK 或单站结果代表 45 站。

> resident replay 回执显示 17 站必须重放 P1、6 站存在 P1 同时刻歧义，FORMAL_PROMOTED 轨迹绑定为 0；策略层不得绕过该上游 lineage 门径或静默回退到旧 P0/P1。

> 接入 resident replay binding 后的统一账本内容 SHA：`9f962a61ed3e87e260361661d92d6ca8aba7ae543163cc1b5d9fd84380f23f4f`。

> 本轮 no-live worker 回执纳入后，统一账本最新内容 SHA：`9067504ce7c0dd131bf6f111d85131630cff88376ea9a24cb65e0185f35d56fa`。

> worker 同一 as-of 的 resident replay 回执刷新后，统一账本最新内容 SHA：`cc48eb1344b76e2a184f9685934d3cdccb0dfe42c96246dae56480005c5f979f`。

> 账本对缺失 replay receipt 的 clean-checkout 可运行性修补后，最新内容 SHA：`6b53ac80525e2810240287268b646fd57be1710e754cea5b9b2ffff9abf0ae80`。

> 2026-08-02T00:45:38Z站点策略边界对齐（历史记录）：当时OOS ledger为`output/audits/oos_gap_ledger_45station_p1_atomic_20260802.json`（1,797条），后续2,503条混合账本也已退为历史。策略三族score rows仍为0，utility合同虽已定义但direct execution binding仍`UNBOUND`；不得从cancelled candidate intents、WS snapshot或天气概率推断fill/utility。

> 历史RKPK完整Maker bundle与prepared supersession回执继续作为原子性/回绕证据保留，但不再作为45站扩展前置门、当前PID快照或跨站物化证明。

## 1. 决策边界

- 上游 Bayes 仍是唯一天气信念权威，输出已物化且带 lineage 的 `p_s(B)`；本站点策略层不得修改概率、中心桶或方向符号。
- 市场结算目标的唯一时区权威是`market_registry.settlement_timezone`；概率artifact、truth、历史曲线、TAF revision、P0/P1与官方WS scope必须完全一致，ET只能作为运营/报表日界，错配一律fail closed。
- 正式P0必须在至少两套正权重、已激活官方原生集合系统的exact mask成熟后物化；每套必须先逐成员数桶并完成偏差、离散度与可靠度校准，再通过跨集合严格历史OOS组合。跨模型门按唯一`independence_family_id`计数，ECMWF ENS的51个成员只算一个家族，不能拆成51个模型。若官方原生不足，只有至少两个独立家族的 Open-Meteo `aggregator_republisher` 历史样本完成闭日AWC strict-OOS与`PRODUCTION_NO_LIVE_ONLY` exact activation时，才可生成明确标注的`REPUBLISHER_NO_LIVE_P0`；它不是正式P0，不进入native arrival、P1或formal/live。单套集合、未经校准的成员频率、确定性模型（含AIFS Single）或其残差扩散均不得物化正式P0；条件不足即fail closed。TAF到达后只能生成同一P0根上的有界P0-M修订。P1到达后成为该决策时点唯一最终SSOT，P1损坏、过期或缺证据时不得回退P0继续执行。未来日只作`observational_candidate_not_oos`观察，不计入OOS或样本门，也不阻断`production_no_live`。
- 现行正式实时来源统一为官方 AWC 全球正式泳道：45站（含中国及台湾站）均以同站`:00` METAR及有效 SPECI生成 truth/P1 与篮子硬事实；硬事实只用于剔除纯 YES 篮子中已数学排除的桶，禁止创建任何 BUY NO intent。官方 AWC TAF 只可在 strict-as-of、原始修订链、有效窗、目标峰值覆盖与 exact-OOS join 齐备时进入P0-M。AMSC固定为`retired_history_only`，仅保留离线历史审计/对照，禁止进入 production truth、P1、篮子或伪装为 AWC lineage；AWC truth lane 仍禁止同站混源、跨源 fallback、AWC缺失/陈旧/重复/未复核更正或不完整TAF，模型 NWP lane 的 Open-Meteo fallback 则必须走显式 republisher lineage 与独立历史 OOS activation。
- P1中已报最高温只形成硬下界，不得直接给当前温度桶加权。软强似然必须预测`最终最高温−当前已报最高温`的条件分布，条件至少包含结算日剩余时间、完整温度前缀的动量与加速度、距当前高点回撤、官方METAR/SPECI露点/云量/降水/风/气压、P0/P0-M模型根对剩余升温的约束，以及同一原生捕获按正式系统权重组合的逐小时未来温度曲线（预测峰值、距峰时间、下一曲线点和未来斜率）；曲线必须内容寻址、严格不晚于P1切点、绑定明确P0根，并由至少两个已激活独立家族共同构成。P0-M只显式继承该P0动力曲线绑定，TAF修订后的最终温度约束仍由P0-M概率根承担；再从不可变P0/P0-M根原子重算P1。只有上述逐小时条件路径模型完成站点×季节×时段×maturity严格同日OOS并正式激活时才可进入执行，历史无逐小时曲线、静态remaining-warming、alpha-zero、晚到/错根曲线或物理状态不完整均fail closed，禁止为补历史样本重新抓取或伪造曲线。
- cash-phase forward observation只接受同一原子cycle receipt，不再接受cycle、概率、WS、cash四份文件事后拼装。receipt必须由`auto_trade_decisions.details_json`内容地址绑定`market_registry.settlement_timezone`、已物化P0/P0-M/P1、cash decision、官方CLOB WS、inventory/order-state、站点策略以及DeltaBasketScore v2 policy/fill artifact/evaluation双SHA；各来源保留自身观测时钟，并满足概率/cash/WS均不晚于cycle decision且报价在decision时仍有效。resident只把验证后的receipt物化到target-local v2 SQLite证据投影，按source rowid分页、generation CAS和append-only键幂等写入；该投影不是第二决策权威，不能授权订单。任一哈希、时区、时钟、订单数、资源边界或官方结算lineage不一致均fail closed。该证据只训练现金阶段策略，不得反写P0/P0-M/P1、中心、方向或支持集。
- forward store的结算label只在主SSOT label事务提交后复制；投影失败保留主SSOT并在后续keyset轮次重放，不得跨库伪造原子提交。poison quarantine不得保存坏JSON，且任何资源探针失败、identity不稳定或CAS冲突都不得跨过坏rowid。retention必须同时绑定最新label revision、正式scorecard receipt、正式attribution receipt、无下游依赖与保留期；仅凭调用方声明的两个SHA不得删除历史。
- 站点目标、保留价和补仓节奏对P0、P0-M、P1三阶段都重新求值：P0用于早期建仓，P0-M用于过程确认与调整，P1用于整点硬实况约束下的动态篮子；不能把站点策略层误写成只在P1调整。
- P0-M只能消费上游已物化的官方 AWC TAF有界修订后验：TAF须先完成逐样本绑定同站点×季节×lead×起报时次×结算定义的OOS校准，并与同决策时点正式NWP组合artifact exact join。校准后TX先形成第一次确认/有限修正，云量、降水、风场随后做物理证据二次确认并折扣强度；两层必须原子生成同一个P0-M checkpoint，不得另立stage或第二中心。TAF与NWP组合holdout残差相关性必须用于折扣重复信息；本站点策略层不得读取原始TAF、重复加权或硬排除温度桶。
- 官方正式报文在进入篮子策略层时必须先生成有效桶集合 `A_t`：已被硬实况确定性排除的桶不进入支持集、目标仓位、成本/份额账本、情景地板或补仓排序。整数精确桶中，报文确认30℃时，29℃及更低桶必须剔除，剩余概率重新归一化。
- 当前 production/no-live 以完整Maker bundle推进自适应目标库存，不允许先买单桶。每个SSOT周期中，全部正库存缺口桶必须同时进入同一原子多意图计划；任一腿缺失、总预算不足、逐档官方WS不完整或硬门失败，整篮HOLD。各Maker腿可独立部分成交，任一回报后全篮重算。只有确认撤销Maker、仅剩单桶紧迫缺口且全部硬门通过时，才允许有价格上限的单桶FOK；FOK不得与Maker bundle共存。未成交Maker不计入已实现地板，未知状态进入`reconcile_required`。
- 站点策略层位于 `ProbabilityEnvelope` 之后、`AdaptiveBasketEvaluator` 之前，只把同一份后验映射为目标库存、保留价折扣和补仓节奏。
- 所有站点共用一个逻辑 SSOT，以 `station_id` 为主键；禁止为每个城市建立独立数据库、独立晋级权威或在线临场训练。
- 钱包历史只支持行为候选训练；源账户私有概率、未成交/撤单、并发资本预留不可见，因此精确参数不得写成源账户事实。

## 2. 分层参数

对 `j∈{target,reserve,cadence}` 分别建立全局参数与站点参数：

```text
theta_global,j ~ N(mu_0,j, Sigma_0,j)
theta_station,j | theta_global,j ~ N(theta_global,j, tau_j^2 I)
rho_s,j = n_eff,s,j / (n_eff,s,j + kappa_j)
theta_post,s,j = rho_s,j * theta_local_hat,s,j + (1-rho_s,j) * theta_global,j
```

- `rho` 是可审计收缩权重；样本越少，越接近全局先验。
- `n_eff` 必须按事件簇、季节、阶段和订单相关性折算，不能把同一事件的数百笔订单当作独立样本。
- `kappa/tau`、特征集合与损失函数必须预注册并通过严格时间外、留一站点和跨季节验证。

## 3. 三类站点策略

### 3.1 目标仓位

```text
z_s,b,t = phi_target(p_stage^{eligible}(B), stage, season, maturity, spread, depth, inventory)_b · theta_post,s,target, b∈A_t
w_s,b,t = softmax(z_s,b,t), b in S_t⊆C_t⊆A_t
q_target,s,b,t = ProjectToBudgetAndRisk(w_s,b,t, Budget_t, PnL_floor, concentration_caps)
```

硬边界：

- `D_t`是当前同周期、同scope且通过诊断资格门的校准模型集合；其极值只形成分歧诊断，不直接定义或夹紧候选边界。`C_t`是以组合概率核心支持集为锚、仅按经策略封顶的分歧扩展层数生成的最远候选搜索范围。`S_t`是真正进入目标篮子的稀疏最终支持集，始终满足`S_t⊆C_t⊆A_t`，不得因模型极值、候选端点或区间边界而自动连续化。
- 正式组合必须来自离线严格OOS晋级且至少含2个正权重原生集合系统；单集合与全部确定性模型只允许benchmark/ablation。近期表现只能通过新版不可变跨集合artifact调整权重，禁止在线临时追随任一集合。
- `S_t`、名义中心 `c*`、中心领先差 `L_center` 和天气方向来自应用官方实况排除后的当前阶段 `p_stage^{eligible}(B)`。组合概率决定核心支持集；方向侧质量决定UP/DOWN和方向桶排序；外围桶还必须逐桶通过概率质量、正式组合模型可靠性、价格覆盖价值和扩桶上限。若`L_center`低于该scope经OOS晋级的阈值，必须标记`center_stable=false`并将方向降级为`NEUTRAL`；诊断模型分歧边界保持独立，只约束`C_t`，不得直接决定`c*`、方向或`S_t`。
- 站点目标权重可以学习集中度、左右侧预算和主/副/保护桶比例，但不得把偏置方向翻转为与 `sign(D_s)` 相反。
- 中心领先阈值属于概率置信门参数，只能经离线OOS形成新版artifact；不得在线追随单次预测或按站点临时手改。
- `q_target` 与 `k_target`双账本同时保存；任何固定等份、2:1或4→3→2不得重新进入候选闭包。
- `q_target`必须由唯一全情景联合目标函数求解。base/bias/edge预算只是同一可行域中的角色约束，禁止三个角色分别归一、分别配仓再相加；外围桶联合最优正缺口为零时必须从最终`S_t`移除并重新求解。
- 新报文导致某桶进入 `B\A_t` 时，该桶在新计划中的目标缺口强制为零；历史已成交仓位保留为不可变事实并单独对账，不因重算被删除，也不再用于新篮子的覆盖和收益计算。

### 3.2 保留价

```text
r_math,s,b,t = sup{l: EV_after(l)>=EV_min and min_y Pi_after(y,l)>=PnL_floor and Capital_after(l)<=Budget}
r_learned,s,b,t = p_stage^{eligible}(b) - margin(phi_reserve · theta_post,s,reserve), b∈A_t
r_final,s,b,t = tick_down(min(r_math,s,b,t, r_learned,s,b,t))
```

- `r_math`是不可放宽的数学上限；站点模型只能给出更保守的价格，不能突破风险、预算或后验价值上限。
- `PnL_floor`只表示可配置的风险限制，不把动态篮子定义为无条件地板保本策略；它与 production/no-live 立即执行阶段的`sum(all_in_yes_ask)<1`硬门同时成立，互不替代。
- 公开成交只观察到已成交限价，无法识别真正未成交保留价；源账户历史仅能产生区间约束，精确保留价必须依赖本系统 `production_no_live` 前向挂单/撤单/成交证据学习。

### 3.3 补仓节奏

```text
h_s,b,t(Delta t)=sigmoid(phi_cadence(stage,time_to_close,gap,fill_rate,spread,depth,quote_change) · theta_post,s,cadence)
eligible = B13_ALLOW * 1[b∈A_t] * 1[Delta q_b>0] * 1[a_eff<=r_final]
action = HOLD if eligible=0; otherwise CadencePolicy(h_s,b,t, station_activation)
```

- 补仓节奏模型输出等待/重评估强度，不直接授权下单。
- `checkpoint/quote/fill/time_gate`仍只是重新求值触发；B13、B14、预算、风险和订单状态任一失败都必须HOLD/STOP/RECONCILE。
- Maker/Taker表达属于执行层；站点节奏不得改写概率、支持集天气方向或提高数学保留价。
- `A_t`为空、剩余概率无法归一化、桶映射与官方事实不一致或官方事实版本不确定时，必须停止新增并fail closed；不得用旧篮子、旧概率或便宜盘口补回被排除桶。

### 3.4 全局现金阶段策略

最近7天45站精确签名订单证明，单笔标准现金不是站点概率或edge的连续函数，而是独立的`策略版本 × 站点本地时间阶段`状态；它位于站点目标仓位/保留价解析之后、订单金额与份数计算之前：

```text
cash_phase = ClockPhase(event_date_local, station_timezone, observed_at_utc)
C_base = T[explicit_clip_policy_version, cash_phase]
C_order,b = min(C_base, r_limit,b * max(q*_b-q_filled,b-q_pending,b,0), remaining_budget, executable_depth_cash_b)
q_order,b = C_order,b / r_limit,b
```

| 源行为先验版本 | `HEAVY` | `STANDARD` | `TAIL` | 使用边界 |
|---|---:|---:|---:|---|
| `source_clip_clock_v20260714_16` | 15.00 | 9.05 | 2.22 | 已观察完整路径，仅作production_no_live行为先验 |
| `source_clip_clock_v20260716_17_partial` | 未观察 | 10.77 | 0.25 | `HEAVY`必须fail closed |

- 现金阶段与`P0/P0-M/P1`证据阶段是两条正交状态轴：前者只控制单笔现金上限，后者决定天气概率、支持集与目标仓位。
- `BasketCashPhasePolicyArtifact`必须显式绑定版本、IANA时区、两个本地阈值、阶段模板、观测/晋级有效期和artifact SHA；未知版本、有效期空洞或未观测阶段不得猜值。
- 源账户新旧版本的私有切换秒只被定位在最后`9.05`与第一`10.77`之间的10.800分钟区间，系统不得把第一笔成交时刻伪装成精确配置时刻。
- 源行为模板不是本系统live参数；自有版本仍须按预算、风险、成交质量和前向 `production_no_live` 晋级后才能获得activation。

## 4. 数据合同

| 合同 | 必须字段 | 作用 |
|---|---|---|
| BasketPolicyTrainingFact | station、event、decision_at、stage、posterior/official-exclusion-mask/quote/inventory/order-state SHA、候选桶特征、chosen/fill/wait、partition | strict-as-of训练事实；一决策点一版本，记录当时哪些桶已被官方实况排除 |
| GlobalBasketPriorArtifact | 三参数族、特征规范、超参数、训练窗、站点集合、事件簇权重、OOS指标、artifact SHA | 冷启动与收缩中心；禁止直接冒充站点最优参数 |
| StationTargetPolicyArtifact | station、season/stage/maturity scope、theta_local、theta_post、rho、n_eff、目标损失和风险指标 | 学习目标库存与集中度 |
| StationReservePriceArtifact | station scope、margin模型、区间删失规则、fill/queue指标、rho、n_eff | 学习保留价折扣，最终仍受B14数学上限约束 |
| StationCadenceArtifact | station scope、离散时间hazard、Maker等待/重评估指标、rho、n_eff | 学习补仓时间与频率 |
| StationBasketPolicyActivation | station、三个artifact ID、global prior ID、valid_from/until、fallback mask、generation、content SHA | 按decision_at解析唯一可用站点策略 |
| ResolvedStationBasketPolicy | station、resolved IDs、rho、fallback reason、target/reserve/cadence参数、resolution SHA | 在线纯读取结果；绑定AdaptiveBasketPlan |
| BasketCashPhasePolicyArtifact | clip policy version、IANA阈值、阶段现金表、valid_from/until、证据状态、artifact SHA | 独立选择标准现金片段；不改天气概率或目标仓位 |
| BasketCashPhaseDecision | previous/clock/next cash phase、模板、checkpoint、reason、policy SHA | 只进不退；未知版本/有效期空洞/未观察阶段fail closed |

## 5. SSOT与查询

```text
station_id
+ stage
+ season_bin
+ maturity_bin
+ liquidity_bin
+ strategy_version
+ decision_at_utc
-> one promoted StationBasketPolicyActivation
```

- 严格回放固定 `global_prior_id + station artifact IDs + activation_id`，禁止查询模糊的 `latest`。
- 原始训练事实、不可变artifact、晋级决定和activation指针解耦；artifact不得原地更新。
- 线上只解析已晋级artifact并执行纯函数；训练、拟合、交叉验证和参数搜索全部离线。

## 6. 冷启动与回退

1. 有精确站点activation且三个参数族均有效：使用站点后验。
2. 站点样本不足但activation明确允许某个已晋级全局/区域fallback mask：使用收缩后的全局先验并记录原因。
3. artifact缺失、过期、scope不匹配或fallback未预先晋级：`FAIL_CLOSED_NO_STATION_POLICY`。
4. 不允许运行时临时删除缺失参数、重新归一或借用邻站参数。

## 7. 晋级指标

| 目标 | 主指标 | 必须同时通过的保护指标 |
|---|---|---|
| 初始选桶 | Precision@observed K、候选覆盖 | 外桶损失、支持集宽度稳定性、方向不翻转 |
| 动态补仓 | next-bucket Top-1 / rank likelihood | 相对随机/全局先验增益、事件簇bootstrap、跨季节稳定性 |
| 保留价 | 区间似然、成交率、成交后成本 | queue/撤单偏差、未成交机会成本、B14上限零突破 |
| 补仓节奏 | 等待时间似然、时间校准 | 订单重复率、未知态、过度交易、截止前未完成率 |
| 经济结果 | 全情景PnL、成本、覆盖 | 最大集中度、篮子外损失、资本占用、故障成本 |

任何站点artifact只有在以下三种比较都不劣时才允许晋级：

1. 同站点时间外对比全局先验；
2. 留一站点迁移对比随机与市场基线；
3. 跨季节、3/7/15/30天窗口和极端盘口压力测试。

## 8. 当前证据与结论

- 历史行为复刻支持“全局先验 + 站点收缩”优于直接复制同一常数参数，但不构成正式天气准确率、胜率或PnL证据。
- SSOT当前有45站×3族共135个策略artifact和200个activation；对象数已齐，但artifact阶段和实际解析未齐。概率物化不再写成 RKPK-only，应以 target SSOT 的实际 stage/站点覆盖为准。
- 45站P0/P0-M/P1三阶段各已生成45×target/reserve/cadence=135个exact-stage no-live候选，合计405个；只读scorecard已覆盖family-specific strict-OOS、LOSO、跨季节及3/7/15/30窗口。当前1,384条v2 facts、405条冻结label只支持P1的35站观测/5站冻结标签，P0/P0-M无v2 score rows，三阶段×三族均为`BLOCKED_EVIDENCE`。候选不是promotion或activation，formal/live仍为0。
- cash phase authority仍是候选，未观察HEAVY不得猜值；ΔBasketScore v2未来日时序观察不属于OOS、不阻断no-live运行，但继续阻断参数权重和live晋级。

- 2026-08-01T22:41:27Z：统一 OOS 账本固定为 45 站/1,704 rows；utility 合同缺失已清零，剩余 45 条为“utility 证据绑定缺失”。P1 180/180 仍无 strict-as-of/root-bound artifact；AIFS、ICON、TAF 缺口继续按官方 native lineage 管理，不能用 Open-Meteo 或模拟成交越过正式门。

## 9. 开发顺序

1. 先取得至少两套原生集合系统的逐成员strict-as-of事实，完成集合内校准与跨集合exact-mask OOS，再物化45站P0；条件不足的站点保持`NO_P0`。
2. 对P0、P0-M、P1分别生成exact stage activation；若共享同一参数artifact，必须由显式stage mask授权，不得把P0-M隐式映射为P0或让P1裸用global fallback。
3. 对target/reserve/cadence三族分别生成family-specific strict OOS、LOSO、跨季节、3/7/15/30天和极端盘口scorecard。
4. 物化 `BasketPolicyTrainingFact`，每个决策点冻结概率、WS、库存、订单状态、策略和ΔBasketScore双SHA；不得反写概率。
5. 训练并冻结 `GlobalBasketPriorArtifact` 与站点收缩artifact，append-only晋级和activation解析器继续覆盖缺失、过期、歧义及低→高→低回绕。
6. cash phase保持独立；HEAVY未观察即fail closed，不能并入目标库存或用源行为模板直接授权。
7. 逐站证明`registry → calibration → P0 → P0-M/P1 → WS → inventory/order-state → ΔBasketScore → decision → training fact → label`，保持`orders_submitted=0`。
8. 未通过正式参数与live门前，`live_order_authorized=false`。
