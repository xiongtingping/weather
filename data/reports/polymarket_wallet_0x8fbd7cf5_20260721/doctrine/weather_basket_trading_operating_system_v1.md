---
document_id: weather-basket-trading-operating-system-v1
version: 1.3.15
status: TARGET_PRODUCTION_NO_LIVE
live_order_authorized: false
effective_date: 2026-08-26
---

# 最高温篮子交易操作系统

> **2026-08-05 发布闭环当前有效覆盖**：`PR16-A 静态 runtime bundle → PR04 durable manifest/CAS → fresh WS/AWC → PR16-B 三服务 no-live → PR17 controlled-live allowlist` 是唯一顺序。PR04 不收实时 P0/P1/WS 快照。至少 3 个合格站点可先进入 allowlist；其余 45 站 scope 以唯一 READY/BLOCKED terminal 继续扩展。历史 OOS archive proof 与 forward/live arrival authority 严格分离。

报告对照审计、交易哲学、策略语言、数学定义、系统架构、宪章、边界与状态流程

- 版本：1.3.13
- 状态：TARGET_PRODUCTION_NO_LIVE

> **2026-08-26 canonical-day 口径换代（当前有效）**：唯一事实日口径由市场挂牌自身声明的 `settlement_timezone` 决定，逐站逐事件日取自 `basket_target_market_registry`，覆盖OOS、P0/P0-M/P1、TAF、策略、WS与篮子。2026-08-05 的 `America/New_York`（ET）统一口径**仅对该代已停产事实继续有效**——`model`(12,600)、`republisher_oos_artifact`(4)、`native_ensemble_oos_event`(280)、`p0_calibrated_distribution`(3) 均于 2026-08-03 至 08-05 之间停止写入——不再适用于现行链路。现行链路本就按各站市场时区产出（近一周 164,939 份概率信封覆盖 30 种时区，ET 仅占 7.9%），与市场赔付口径一致；45 站中仅 KATL/KLGA/KMIA 三站由市场声明为 ET。`calibration_station_registry.timezone_name` 仍非权威且 45 站中 33 站错标，禁止用它推导、补写或改写 `settlement_timezone`；权威仅两处：挂牌侧 `basket_target_market_registry.settlement_timezone` 与其绑定表 `calibration_station_market_timezone_binding`。旧 ET 代事实原样保留为历史，不改名、不回填、不与新代共用 lineage；跨代比较逐 scope `BLOCKED`。

> **2026-08-05 canonical-day / release override（已由 2026-08-26 换代取代；仅适用该代已停产事实）**：唯一事实日口径是`America/New_York`（ET），覆盖OOS、P0/P0-M/P1、TAF、策略、WS与篮子。`market_registry`中的站点地理/历史市场时区仅保留映射lineage，不能再推导、默认补齐或改写canonical `settlement_timezone`；旧非ET事实没有逐字段同义与同代lineage时必须逐scope `BLOCKED`。PR04继续冻结为最终一次manifest/CAS；PR05–PR16未发布结果不得被服务、篮子或live读取。

> **2026-08-02 当前生产覆盖**：所有 OOS/样本门/参数晋级只接受闭日历史 strict-as-of 数据；未来/forward 仅是 `observational_candidate_not_oos`，不计 OOS、不阻断 `production_no_live`，也不成为未来 OOS 硬门。NWP 采用 native-first；同站原生缺失、无效、过期或迟到时才使用带 `aggregator_republisher` lineage 的 Open-Meteo no-live P0/P0-M fallback，禁止进入 native arrival、P1 或 formal/live。既有 ECMWF/DWD/AIFS/ICON raw 优先复用，不重复抓取；篮子继续纯 YES，生产仅保留三个用户域服务 `com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service`。

> **2026-08-04 PR04运行根（当前有效）**：唯一账本指针为`output/audits/oos_gap_ledger_45station_current.json`，pointer SHA=`35777f0d3dc8aa8542ea039d118e55bf3f248b56efe007fb6eefcb557294e9c1`，release=`fa3e301bceaa82b57bd74fda5c7825348cb230f2c1001b00f66c7d421cfc9032`，ledger SHA=`b6eda076f0d6c9eef673978a7ce7b0a4ed57c1612c6b78309e2face9e8a961e6`，as-of=`2026-08-03T21:08:00Z`，baseline identity=`a95ae2ef6d2cd9128b78b76d0e204c22fc6dd63b08ea547866cd8ce23b35c7e4`。当前0 activation、P0仅9站、native linkage仅41站，TAF_P0M/TRAJECTORY_P1/STRATEGY_UTILITY/MARKET_WS/RUNTIME均缺root，因此系统保持production-no-live、纯YES、零订单；旧2,503条混合账本不再是current authority。

> **2026-08-04 W3c运行边界补充**：active V9 canonical SSOT已新增CYYZ/NZWN两条Gen1静态republisher no-live activation，迁移与首次提交/幂等重放均有内容寻址回执。该变化没有切换PR04 current root、没有生成forward P0/P0-M/P1、没有启动三个服务、没有解除live pause或调用gateway。下一运行出口是独立forward applicability/native-unavailable receipt与no-live P0 snapshot；静态activation不得作为持续有效许可或formal/live authority。

> 2026-08-02T10:26Z 生产验收刷新：三个用户域服务 `com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service` 已探针确认；纯 YES、AWC/P1、官方 CLOB WS、live pause、gateway read-only、zero orders 均保持。Open-Meteo fallback 仅在 native 缺失且历史 strict-as-of activation 通过时进入 no-live P0/P0-M；forward 永不计 OOS。
- 权威：SUBORDINATE_TO_BAYES_BASKET_TARGET_ARCHITECTURE_V1
- 生产下单授权：False
- 来源报告SHA256：`486e826d32f2cd16773a5452c74b88b5560b8dd23db1f3ff7332904faa20502a`

## 核心命题

唯一生产表述是：校准后的至少两套原生集合系统跨集合组合生成P0，官方 AWC TAF TX与物理证据原子修订为P0-M，官方 AWC 同站`:00` METAR/有效 SPECI生成P1；组合概率决定核心支持集与方向，模型分歧限制候选搜索边界和集中度，严格`ΔBasketScore v2`在预算、真实WS深度、库存和风险可行域内联合求唯一目标库存与完整Maker bundle。确定性模型加残差扩散退出P0权威，仅保留离线benchmark和ablation。

当前唯一篮子驻留服务名为`com.weather.weather_basket_yes`，与`com.weather.market_ws`、`com.weather.clob_order_service`同在`gui/$(id -u)`用户域；旧`com.weather.reported_trajectory_no`服务已退役。最近一次完整no-live worker只生成证据不足下的纯YES阻断决策，未生成任何NO或YES订单意图，gateway保持只读。

2026-08-01T12:17Z再验收：三服务单一用户域、AWC-only、hard-NO removed、live pause、只读gateway、官方WS与持仓registry分类检查全部通过。全量worker状态`COMPLETED_PRODUCTION_NO_LIVE`，只新增HOLD/blocked证据，intent计数保持685不变、order event仍为0、gateway未调用。历史非零YES仓位若官方registry确认已关闭/不接单，必须保留余额并显式标记不可交易；不得伪造WS book、改用REST盘口或把仓位静默删除。

`ΔBasketScore v2`最新target-owned未来日时序观察候选状态为`ACCUMULATING`：425条fact-bound观测均完成P1 envelope、官方WS快照与结算label内容哈希复核，其中168条具备合法候选价格，425条具备结算归因；RKPK为298条观测、168条价格、298条归因。该观察不是OOS；正式晋级仍因HEAVY缺30、STANDARD缺13个独立历史闭日事件而阻断；不阻断production-no-live的HOLD/blocked证据链。

2026-08-02全局链路快照：最新完整45站worker已跑通只读编译与回绕；P0 27条已提交，P0-M产生90条受控忽略回执，P1覆盖27个scope并有24个因正式轨迹或COR/AMD复核缺口继续fail closed。AIFS-Single复用179个retained raw完成45/45曲线并生成14条合法closed-truth facts；TAF exact join最高10/24门、0个合格scope。所有步骤保持纯YES、官方AWC、gateway read-only、production_no_live和零订单。

2026-08-02T02:00Z运行复核：原生activation→strict-OOS linkage为55/139可消费、84条fail closed；45站统一缺口账本已刷新。no-live runner使用WAL-safe只读连接后实跑45站全量阻断，SQLite `PRAGMA quick_check(1)=ok`，生产验收通过，三用户域服务和纯YES边界不变。任何未闭合P0/P0-M/P1、TAF revision、策略utility或ΔBasketScore自然样本仍不得绕过证据门。

2026-08-02T21:05Z全链路刷新（历史记录）：多时区calibration曾以16个strict packet聚合到45站，后续2,503条混合ledger现仅保留历史引用；当前authority见文首2026-08-04 PR04运行根。

2026-08-01T22:54Z运行回绕：全局 ledger 更新为 1,797 条并显式记录 TAF revision-chain 歧义（最新内容 SHA `6c54591a0f1d045ec8de2d2975d6fb676e18d9ad0e7a089d62290c1c53a469ac`）；P0-M 最高 exact-OOS=9/24、P1 strict-as-of/root-bound artifact=0、utility score rows=0。ICON f015 原生 receipt 缺失、AIFS 仍不足 20+8，故系统继续以官方 AWC、纯 YES、HOLD/fail-closed 路径运行；三用户域服务、live pause、gateway read-only 与零订单边界未改变。

resident replay 审计补充：17 站 stale P1 必须绑定新 P0 根后重放，6 站存在同一决策时刻歧义，FORMAL_PROMOTED trajectory activation=0；运行链保持 fail closed，不以过期 P1 或模拟 receipt 继续执行。

接入 resident replay binding 后的统一账本内容 SHA：`9f962a61ed3e87e260361661d92d6ca8aba7ae543163cc1b5d9fd84380f23f4f`。

本轮 no-live worker 回执纳入后，统一账本最新内容 SHA：`9067504ce7c0dd131bf6f111d85131630cff88376ea9a24cb65e0185f35d56fa`。

worker 同一 as-of 的 resident replay 回执刷新后，统一账本最新内容 SHA：`cc48eb1344b76e2a184f9685934d3cdccb0dfe42c96246dae56480005c5f979f`。

账本对缺失 replay receipt 的 clean-checkout 可运行性修补后，最新内容 SHA：`6b53ac80525e2810240287268b646fd57be1710e754cea5b9b2ffff9abf0ae80`。

2026-08-02T00:45:38Z运行边界回绕（历史记录）：当时以 `output/audits/oos_gap_ledger_45station_p1_atomic_20260802.json`（1,797条）作为缺口索引；当前唯一指针见文首生产覆盖。TAF/P0-M exact-join、P0/P1、utility和Delta自然样本门继续逐项fail closed，三服务与官方AWC、纯YES、gateway read-only、零订单约束保持。

## 0. 生产宪章与优先级

本节是本文唯一可执行规范。后续“报告对照审计、历史公式和证据映射”只保留研究lineage；任何冲突均以本节、`bayes_basket_target_architecture_v1.md`和集中式契约测试为准。

1. 阶段唯一为`NWP→P0`、`TAF TX校准确认/有限修正＋云量/降水/风物理二次确认→原子P0-M`、`METAR/SPECI→P1`；禁止P0-F或第二概率中心。
2. 正式P0必须来自至少两个正权重官方原生集合系统：先保存逐成员并数桶，再分别校准偏差、离散度与可靠度，最后跨集合严格历史OOS组合。跨模型门只按唯一`independence_family_id`计数；ECMWF ENS的51个成员合计仍为一个模型家族，ECMWF ENS、GEFS、ECCC GEPS、DWD ICON Global/EPS及区域/加拿大集合模型按独立家族治理。官方原生不足时，仅允许至少两个独立家族的 Open-Meteo `aggregator_republisher` 历史同日 OOS exact activation 形成显式`REPUBLISHER_NO_LIVE_P0`；它不是正式P0，不进入 native arrival、P1 或 formal/live。原始成员频率不是最终概率；单套集合、确定性模型（含AIFS Single）、确定性残差扩散或它们的混凑都不得获得正式P0权威。条件不足即`FAIL_CLOSED_NO_FORMAL_P0`。
3. 市场结算目标使用`market_registry.settlement_timezone`；ET仅作运营报表日界。未来日可以保留有界到达/接口观察或 no-live candidate，但不得把未来样本计入任何OOS、20+8或TAF-24门；未来日truth和label禁止提前写入，正式OOS只认闭日历史AWC truth。
4. 正式实时来源唯一为官方 AWC 全球泳道：45站（含中国及台湾站）均消费同站`:00` METAR及有效 SPECI，以推进 realtime truth、P1 与纯 YES 篮子硬事实排除集合；生产不再运行 hard-NO/买-NO 策略。官方 AWC TAF 只能在 strict-as-of、原始修订链、有效窗、目标峰值覆盖与 exact-OOS join 完整时进入P0-M。AMSC状态固定为`retired_history_only`，只保留离线历史审计/对照，不得进入 production truth、P1、篮子或伪装为AWC lineage；AWC truth lane 的缺失、陈旧、重复、更正未复核、TAF证据不完整、同站混源或跨源 fallback 一律 fail closed；这不限制 NWP model lane 在官方原生缺失时使用显式 Open-Meteo `aggregator_republisher` 历史 fallback。
5. 模型极值只决定分歧诊断；候选边界只决定最多搜索到哪里；最终支持集由组合概率和联合目标决定。支持集可稀疏，端点之间不得自动填满，单个离群模型不得强制扩桶。
6. 中心桶领先差低于经OOS晋级阈值时，中心不稳定且方向为NEUTRAL；模型分歧边界仍独立保留。方向由组合概率左右侧质量决定，不能由极值或regime平移概率中心。
7. `ΔBasketScore v2`是唯一目标函数。价格只通过真实逐档WS、完整结算利润曲线、覆盖、集中度和执行成本影响目标；禁止`1/ask`、低价、高赔率或reservation value改写概率和支持集。
8. 目标库存按全情景联合求解，不固定桶数、不等份、不4→3→2、不把base/bias/edge分别配仓后相加。每个正库存缺口必须进入同一原子完整Maker bundle，禁止先买单桶。
9. Maker优先；每腿允许确定性部分成交，任一回报后重算全篮。仅在Maker撤销确认、只剩单桶紧迫缺口且价格上限通过时允许FOK；未知、迟到或冲突回报立即`reconcile_required`。
10. 立即执行或已成交部分必须满足逐档`sum(all_in_yes_ask)<1`；未成交staged GTC不计入已实现地板。预算、深度、lineage、新鲜度、订单状态或资源门任一失败即fail closed。
11. 主链固定为`registry → calibration → P0 → P0-M/P1 → 官方WS → inventory/order-state → ΔBasketScore → decision → training fact → label`。canonical 45站按有界轮转逐站物化，不以RKPK或decision壳冒充全站闭环。
12. 运行只保留用户域三个同域服务，保持`production_no_live`、live pause、零gateway写和零订单。训练与原生跨集合严格OOS立即使用已结算历史同日事实及native run-init滚动完成，不等待未来日；未来日/forward一律为`observational_candidate_not_oos`观察，不属于任何OOS、不计入历史样本门、不阻断`production_no_live`，只承担独立时序验证并阻断formal/live晋级。原生跨集合组合自身`20 train + 8 historical outer holdout`门不放宽，条件不足即不物化P0；Open-Meteo仅可作为显式`aggregator_republisher`历史同日OOS/calibration fallback，绝不进入native arrival或native P0 gate。
13. 所有采集、解码和回放必须设RSS、响应、文件数、临时落盘、DB/WAL和最低剩余磁盘硬门；超限或探针不可用立即停止。只保留lineage、解码、审计或历史重建所需最小对象。

## 直接回答

- 交易对象不是一个温度点，而是一个带结算规则、离散桶和盘口成本的条件收益分布。
- 多桶的第一功能是覆盖模型分歧与结算误差，第二功能才是让低价边缘桶提供非线性收益；多桶本身不等于保本。
- 重仓必须拆成成本重仓与份额重仓：中心/高价桶可承载更多本金，低价边缘桶可用较少本金获得更多结算份额。
- 方向偏置不是把所有桶整体平移，而是改变带宽内的目标库存、补仓顺序和边缘凸性预算，并且必须有证据、置信度和失效时间。
- 建仓不是一次性静态最优化，而是证据、价格、库存和未决订单共同驱动的状态机；没有新桶也可能发生主成本桶切换。
- 三个天气阶段都可以触发概率与篮子重算：P0负责NWP早期建仓，P0-M负责TAF TX与物理证据原子确认/有限修正，P1负责METAR/SPECI接管；不存在P0-F。后阶段只追加新版本，不改写历史成交事实。
- 官方报文硬实况优先于模型概率和盘口：报文确认当前实况为30℃后，29℃及更低桶立即从有效剩余篮子删除，剩余概率重新归一化；已成交旧仓位保留为历史事实并单独对账，不再计入新篮子的概率、成本、覆盖和收益地板。
- 报文达到30℃不代表给30℃桶加权：30℃仅是硬下界。P1软似然必须以剩余结算时间、温度轨迹动量/加速度、距高点回撤、露点、云量、降水、风、气压、P0/P0-M最终温度概率根，以及同次原生解码保留并按至少两个正式独立家族权重组合的逐小时未来温度曲线，共同预测未来剩余升温路径；曲线必须给出未来峰值/峰时、下一点和未来斜率，内容寻址、strict-as-of且绑定明确P0动力根。P0-M继承P0动力曲线但以自身TAF后验约束最终温度，再将条件路径分布映射到最终结算桶并从不可变概率根重算；历史缺曲线不得重抓或伪造，旧静态增量artifact、alpha-zero、晚到/错根曲线、缺物理字段或少于可识别轨迹前缀均不得执行。
- 正式报文沿用官方有效时间并统一按官方 AWC 全球正式泳道消费：45站（含中国及台湾站）均以同站`:00` METAR/有效 SPECI进入 realtime truth/P1/纯 YES 篮子硬事实排除集合，AWC TAF只以完整 strict-as-of lineage进入P0-M；AMSC固定为`retired_history_only`，禁止进入正式链路。AWC truth lane 禁止交叉 fallback、同站混源或旧 AMSC lineage 混入；缺失、陈旧、重复、未复核更正或不完整TAF均 fail closed。NWP model lane 的 Open-Meteo fallback 仍须保留 `aggregator_republisher` lineage、闭日 AWC truth 和独立历史 OOS activation。
- 动态篮子不是无条件保本策略；但立即执行或已成交部分的逐档`sum(all_in_yes_ask)<1`仍是硬门，与概率质量、资金、集中度、盘口深度、证据新鲜度和订单状态同时成立。
- 3/7/15/30天嵌套窗口只能描述观察长度，真实演进必须看互不重叠分段；近期是更窄、更连续、更主动成交，但方向至少两次折返。
- 最终重仓更像离散现金clip、填单率和补单次数共同形成的库存结果，而不是一笔更大的概率押注。
- 10分钟参考价只能用于事后描述成交偏离，不能替代当时可执行bid/ask/depth或证明挂单上限。
- 任何公式都必须标记为数据定义、候选决策模型或硬约束；报告无法支持把候选模型写成对手精确私有算法。

## 本轮报告对照审计

18项对照中，3项保留、14项加强、1项纠错；未发现可把研究候选直接晋级为生产规则的证据。

| 审计 | 报告事实 | 原方案 | 结论 | 修订 | 条款 | 证据 |
|---|---|---|---|---|---|---|
| A01 | 多桶同时承担覆盖与低价边缘凸性，但多桶本身不等于保本。 | 覆盖底座+偏置+边缘凸性三本预算。 | 保留 | 继续使用非严格保本语言，并要求逐情景地板。 | P02, P09, P10, M15, M18-M20 | E06, E10 |
| A02 | 30天严格条件保本约11.9%，聚合完整买入路径ROI约2.86%。 | E06误把79.1%写成路径ROI。 | 纠错 | 将79.1%只保留给模型包络极端选择，路径ROI改为2.86%，并加入数值锁定校验。 | M18-M20, E01, E06 | E01, E06 |
| A03 | 3/7/15/30天是嵌套窗口，不能直接当成真实时间演进。 | 只在治理门提到多窗口，未定义重叠偏差。 | 加强 | 新增嵌套窗与互斥窗定义、M35-M36及完整窗口表。 | M35, M36, G3 | E19 |
| A04 | 互斥分段中方向至少两次折返，不支持从冷偏永久切换为暖偏。 | 偏置有TTL，但未把非单调演进写成迁移禁令。 | 加强 | 禁止从账户级窗口机械迁移方向，增加站点迁移闸门M42。 | P03, M12, M42 | E14, E19 |
| A05 | TAF TX仅覆盖44/306事件，且有效窗滚动可让日内TX不再代表全天日高。 | TAF作为二级锚。 | 加强 | TAF必须绑定issue、validity、目标峰值覆盖和修订链，缺一则alpha_taf归零。 | M07, 宪章5, F1 | E02 |
| A06 | 严格前推只有46.3%选择此前MAE最低模型。 | 历史MAE形成模型可靠度权重。 | 保留 | 明确历史表现只是先验；当日模型聚类和离群闸门可覆盖先验排序。 | P04, M04-M06 | E01, E13 |
| A07 | 站点差异明显，八站GFS冷差与钱包偏移相关仅0.063，不能跨站复制单因子方向。 | 站点化中心，但跨站迁移边界不够硬。 | 加强 | 新增M42站点迁移闸门，跨站信号只可做弱日期regime候选。 | 宪章3, M11, M42 | E14 |
| A08 | 模型跨度从不超过1C到大于2C时，主桶成本占比53.1%降至45.1%，桶数3.74升至4.37。 | 模型分歧驱动带宽。 | 保留 | 将跨度只用作带宽/集中度候选，不写成方向或因果公式。 | M08-M10, M26 | E11, E16 |
| A09 | 成本重仓与份额重仓显著分离；首价解释成本占比远强于份额占比。 | M16默认先给资本再换算份额。 | 加强 | M16显式支持CAPITAL_FIRST与SHARE_FIRST两种目标模式，M32要求成本和份额双残差闭合。 | P06, M13, M16, M32 | E09, E10 |
| A10 | 20,694个匹配订单族呈离散现金clip；最常见9.05 USDC，占YES BUY订单19.6%。 | 只有目标库存和限价，没有订单族/clip层。 | 加强 | 新增CLIP原语与M37，目标库存通过多个离散订单族实现。 | CLIP, M37, F4 | E12, E17 |
| A11 | 同价、上涨、下跌加仓成本分别约37.6%/30.5%/26.1%。 | 价格与库存驱动，但容易被误读成抄底。 | 加强 | 明确价格方向不是充分条件；只有缺口、有效价、地板和硬门共同通过才补。 | P05, M23-M25, M29 | E18 |
| A12 | 10分钟prices-history是参考成交序列，不是历史可执行bid/ask/depth或队列。 | M24使用真实深度，但未定义参考价禁用边界。 | 加强 | 新增参考价定义、M39，并禁止它进入可执行价格或限价证明。 | M24, M39, 宪章17 | E20 |
| A13 | 237个历史事件中97.5%在D-1 20:30后仍买，72.2%新增桶，但低/高/内部缺口方向接近均衡。 | 形成成熟度只有概念定义。 | 加强 | 新增成熟度向量M33、同成熟度对照M34和CHECKPOINT_REVIEW。 | M33, M34, F4-F6 | E03, E04, E21 |
| A14 | Maker加权填单率39.1%，Taker 99.2%；成都42C临近截止从Maker转Taker。 | 未量化Maker等待与时间紧迫。 | 加强 | 新增M38紧迫度，只有缺口、截止时间和主动成交溢价共同满足才切换。 | M38, F4-F5 | E17, E23 |
| A15 | 7/23新篮子在快照间未新增桶但仍补现有桶；当前构建尚未结算。 | 有“补足已有桶”状态，但未硬化未成熟快照边界。 | 加强 | 所有形成中快照必须带as_of、成熟度与unresolved，不得赋赢家或PnL。 | M33-M34, 状态机, F5-F7 | E07, E08, E22 |
| A16 | 候选中心与不确定度尚未被严格投影为每个离散桶的概率。 | M22直接使用未定义来源的p_i。 | 加强 | 新增M31连续分布到桶区间积分，并要求全桶归一与开口桶闭合。 | M08-M10, M22, M31 | E15 |
| A17 | 成都7/21的39C晚到由价格急跌Maker成交解释更强；7/22的42C重仓由低价份额杠杆和持续补单形成。 | 个案逻辑未写入可泛化边界。 | 加强 | 个案仅校验价格/库存/紧迫机制，不允许导出固定+2C、固定暖尾或固定限价。 | P05, M23-M25, M38-M39 | E23 |
| A18 | 模型、TAF、实况、盘口、订单和截止时间在不同检查点改变目标或执行。 | 触发条件散落在状态转换中。 | 加强 | 新增M41检查点触发向量；无变化不重算，任一更新也不自动等于加仓。 | CHECKPOINT, M41, F5-F6 | E03, E05, E21 |

## 3/7/15/30天与互斥分段

3/7/15/30天共享最近日期，只能比较不同观察长度，不能把差值解释为按时间发生的策略迁移。

16-30天前、8-15天前、4-7天前、最近3天互不重叠，用于描述时间演进；仍受城市组合、天气难度、流动性和未结算选择影响。

| 互斥分段 | 事件 | 均桶数 | 连续带 | 严格条件保本 | 偏上/中性/偏下 | 路径ROI | Maker成本 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 16-30天前 | 658 | 6.04 | 59.9% | 16.9% | 31.3%/36.6%/32.1% | -4.1% | 99.8% |
| 8-15天前 | 360 | 3.90 | 89.7% | 8.3% | 30.8%/51.7%/17.5% | 4.4% | 81.1% |
| 4-7天前 | 180 | 4.10 | 92.2% | 3.3% | 34.4%/44.4%/21.1% | 25.3% | 73.0% |
| 最近3天 | 135 | 3.89 | 89.6% | 8.9% | 28.9%/49.6%/21.5% | 19.5% | 70.0% |

从最早分段到最近3天，均桶数6.03降至3.89、连续带59.9%升至89.6%、Maker成本99.8%降至70.0%，但严格条件保本16.9%降至8.9%，方向并未单调转暖；因此应升级形成与执行机制，不应固化方向。


## 证据等级

### F - 可复算事实

公开成交、桶、时间、价格、份额、订单哈希、已冻结模型/TAF/METAR、结算结果和快照lineage可独立复算。

允许：发生了、观测到、复算为；禁止：因此私有算法必然是。

### I - 机制推断

由多事件、跨时间或外样本证据支持的最小充分行为解释，但无法识别精确权重、阈值、未成交挂单或私有模型。

允许：支持、符合、最小充分解释、候选驱动；禁止：已经证明因果、精确公式就是。

### N - 规范性设计

为了可实施、可审计与风险可控而提出的系统合同、公式、阈值接口和状态机。

允许：应当、必须、建议定义为、候选公式；禁止：对手实际使用。

## 交易哲学

### P01 交易分布，不交易单点预报

中心只是候选温标；实际决策同时需要宽度、偏置、价格、库存和结算合同。

系统含义：任何单模型最高温不得直接等同于唯一重仓桶。

### P02 先覆盖，再表达观点

覆盖底座处理模型分歧与结算离散化，方向预算只在底座之上制造可控凸起。

系统含义：方向偏置不能吞噬使篮子保持可承受情景损失所需的底座资本。

### P03 偏置是有期限的叠加层

偏上、偏下或中性必须绑定站点、日期、证据快照、置信度和TTL。

系统含义：昨日暖向或站点长期冷差不能自动复制到下一目标日。

### P04 模型没有永久王者

模型选择必须按站点、季节、运行时次和提前量校准，并允许当日离群闸门改变有效模型集合。

系统含义：最低历史MAE只影响先验权重，不直接决定当日主桶。

### P05 价格决定表达效率，不决定天气真值

模型、TAF和实况决定哪些桶可被允许；盘口与深度决定用多少成本、多少份额以及何时成交。

系统含义：低价只能提高凸性倍数，不能单独把一个低概率桶变成高概率桶。

### P06 成本与份额必须双账本

美元成本衡量资本暴露，结算份额衡量情景收益；二者在低价桶上会显著分离。

系统含义：‘重仓’若不说明按成本还是按份额，属于无效策略语言。

### P07 时间既产生信息，也提供成交选择权

分批建仓保留对模型、TAF、实况和盘口变化的响应空间，同时通过提前挂单争取更低有效价格。

系统含义：不是第一时间建满并非犹豫，而是保留信息与执行期权。

### P08 主桶是状态，不是身份

主成本桶会因同桶补仓、价格成交和目标库存变化而切换，即使已选温度带完全不变。

系统含义：监控必须同时追踪扩桶和带内资金排序。

### P09 彩票性来自收益曲线，不来自标签

低价边缘桶提供高每美元结算倍数，但只有在预算上限和情景损失可控时才是条件凸性。

系统含义：边缘桶不能用‘彩票’一词豁免概率、价格、预算和证据审查。

### P10 保本必须逐情景证明

只有对明确结果集合、有效成交价、费用和份额计算出的最低利润不小于零，才能称为条件保本。

系统含义：覆盖多个桶或YES价格和小于1都不能自动证明全市场保本。

动态篮子不承诺无条件保本；但立即执行或已成交部分必须满足逐档`sum(all_in_yes_ask)<1`。该门不替代概率、预算、深度、集中度和`ΔBasketScore v2`，未成交Maker也不能计入已实现地板。

### P11 未知订单状态优先于新观点

未决、部分成交或结果不明时，库存真值不闭合，系统应先对账而不是继续解释天气。

系统含义：执行状态不确定必须fail closed。

### P12 每个结论都要可过期

数据截面、模型运行、TAF有效窗、盘口快照和仓位状态都有时间边界。

系统含义：没有as-of和lineage的结论不能进入决策。

## 术语定义

| 术语 | 符号 | 定义 |
|---|---|---|
| 目标日 | `d` | 市场resolution contract指定的站点本地结算日期；不得与交易值班日混用。 |
| 站点 | `s` | 市场结算规则所绑定的气象站或明确地点，是模型、TAF、实况和历史误差对齐的最小空间单位。 |
| 温度桶 | `b_i` | 互斥的离散结算结果，带下界、上界、单位和边界包含规则。 |
| 篮子 | `S` | 同一站点与目标日中由组合概率和联合目标选中的YES温度桶集合；允许稀疏，不因端点或模型极值自动填满中间桶。 |
| 候选中心 | `C` | 经站点化模型组合、历史误差修正和可选TAF确认得到的连续温度中心，不等于最终主桶。 |
| 模型包络 | `E_model` | 同一as-of下有效模型预报最高温的最冷到最暖区间及其内部聚类。 |
| 站点长期误差 | `bias(s,m,k,r,h)` | 站点×模型×季节/天气型×运行时次×提前量下，历史实际日最高温减历史预报日最高温的误差分布；不是历史最高温的简单平均。 |
| 方向偏置 | `D` | 在候选中心和基础带宽确定后，对更高或更低结果的临时库存倾斜；取UP、DOWN或NEUTRAL并绑定强度与TTL。 |
| 覆盖/偏置/边缘角色 | `role(b)` | 仅用于约束、解释和归因；三类角色不能分别配仓后相加，目标库存始终由同一个联合优化求解。 |
| 主成本桶 | `b_cost*` | 累计买入成本最大的桶。 |
| 主份额桶 | `b_share*` | 累计YES份额最大的桶；可能与主成本桶不同。 |
| 扩桶 | `EXTEND` | 相对上一闭合快照首次增加一个温度桶。 |
| 同桶追补 | 补足已有桶 | 不改变已选桶集合，只减少目标份额或目标成本缺口。 |
| 补内部缺口 | 补齐中间缺口 | 在已选低端和高端之间增加此前未持有的中间桶，使温度带连续。 |
| 官方实况排除 | `A_t` | 根据官方报文、结算定义和桶映射生成仍可能发生的温度桶集合；不可能桶不得进入剩余篮子。 |
| 形成成熟度 | `M` | 从首笔成交起，结合相对时间、累计成本比例、已用检查点和桶数衡量篮子形成进度。 |
| 有效成交价 | `a_i_eff` | 按真实深度、费用和滑点计算的加权平均每份成本，不是单一最优卖一价。 |
| 条件结算地板 | `Floor(S)` | 只在明确结果集合S内计算的最低净利润；若赢家落在S外，完整篮子仍可能损失全部成本。 |
| 证据新鲜度 | `Fresh` | 模型、TAF、实况、盘口、持仓和订单状态相对各自TTL是否仍可用于当前决策。 |
| 桶概率 | `p_i` | 同一strict-as-of连续温度分布在温度桶区间上的概率质量；必须覆盖开口桶并使所有互斥桶概率和为1。 |
| 目标库存 | `q*` | 在概率、全情景PnL、覆盖、集中度、预算、真实深度和执行成本下由唯一联合目标求得；同时输出成本与份额双账本。 |
| 订单clip | `c_ij` | 桶i第j个离散现金或份额订单族的原始目标规模；它与实际成交比例共同决定已成交成本。 |
| 填单率 | `rho_ij` | 某订单族已成交规模除以其可识别原始目标规模；只对至少部分成交且能匹配的订单可计算。 |
| 历史参考价 | `p_ref` | prices-history等成交/参考序列在指定回看窗内的描述性价格；不是当时可执行bid/ask/depth、排队位置或对手限价。 |
| 可执行盘口 | `Book_t` | 同一时点官方订单簿的bid/ask逐档深度及新鲜度，是计算a_i_eff的唯一市场价格输入。 |
| 形成检查点 | `X_t` | 模型运行、TAF修订、实况推进、盘口变化、订单更新或截止时间跨阈值中的至少一项发生变化并触发复核的时点。 |
| 嵌套窗口 | `W_nested` | 共享同一结束日的3/7/15/30天累计窗口；用于比较观察长度，不用于直接判断按时间发生的策略迁移。 |
| 互斥窗口 | `W_disjoint` | 彼此无重叠的时间分段；用于描述演进，但仍需控制城市组合、天气难度、流动性与未结算选择。 |

## 系统架构层级

### L0 宪章与授权层

职责：不可变原则、风险偏好、生产授权、禁止事项和变更治理
输入：本文件的研究结论与独立审批
输出：允许的策略族、资本上限、fail-closed规则
禁止：直接消费盘口或生成订单

### L1 本体与合同层

职责：站点、目标日、结算来源、温度桶、单位、边界、时区和数据lineage
输入：市场合同与官方来源登记
输出：唯一market contract和bucket map
禁止：把预测值冒充结算真值

### L2 证据层

职责：模型运行、TAF修订、官方实况、站点历史误差、盘口和深度的各自事实
输入：带as-of的不可变原始数据
输出：标准化证据快照与质量标记
禁止：跨源无lineage覆盖或用未来数据回填当时决策

### L3 信念与候选层

职责：站点化中心、模型分歧、总不确定度、方向偏置对象和有效期
输入：L2天气证据，不含盘口价格作为天气真值
输出：C、sigma_total、D、confidence、TTL
禁止：直接生成下单数量

### L4 组合与配仓层

职责：选桶带、覆盖/偏置/边缘预算、成本目标、份额目标和情景PnL
输入：L3候选分布、L1桶合同、风险预算与只读可执行价格
输出：版本化target inventory plan
禁止：把目标计划当作已成交持仓

### L5 执行与订单层

职责：真实深度、限价、分批、订单ID、成交、撤单、部分成交和对账
输入：L4计划与官方CLOB证据
输出：intent、order、fill和working inventory事实
禁止：重算天气中心或静默重试未知订单

### L6 风险与状态机层

职责：资本预留、情景损失、集中度、新鲜度、状态转换、停牌和reconcile
输入：L1-L5的原子事实
输出：允许执行、保持现状、停止新增、必须对账
禁止：在事实不闭合时降级放行

### L7 审计与研究层

职责：不可变快照、指标、归因、回放、验证、版本晋级和退役
输入：全链路lineage与结算结果
输出：可复算报告、参数候选与晋级证据
禁止：以离线结论绕过生产授权

## 宪章

1. **结算合同先于预测**：未冻结站点、目标日、单位、桶边界和resolution source，不得选桶或计算PnL。
2. **天气事实与市场事实分域**：天气中心不得读取盘口或钱包；执行价格不得反向改写天气证据。
3. **站点化而非全球固定模型**：模型权重必须至少可按站点、季节、运行时次和提前量区分；跨站方向只有通过M42迁移闸门才能作为弱先验，禁止机械复制。
4. **离群闸门必须显式**：当日模型分裂若触发离群处理，必须保存触发原因、被降权模型和反事实中心。
5. **TAF只能在有效窗内使用**：TAF TX必须绑定issue、原始修订链、validity和目标峰值覆盖；有效窗已越过日峰或覆盖缺失时alpha_taf必须为0。
6. **偏置必须是对象**：每个方向偏置必须包含方向、强度、证据、置信度、创建时点、TTL和撤销条件。
7. **角色只约束、不分开配仓**：覆盖、偏置、边缘只作为同一可行域中的上限、覆盖和归因标签，禁止分别归一、分别配仓再相加。
8. **成本与份额双账本**：每桶必须同时保存成本、份额、均价、成本占比和份额占比，并由同一`q*`与实际成交事实闭合。
9. **保本逐情景证明**：只有费用后最小情景利润非负且结果集合明确，才可使用‘条件保本’；不得使用无条件‘保本’标签。
10. **分阶段建仓**：首建、检查点复核、扩腿、同桶追补和停止加仓必须是不同动作；形成成熟度不得压缩成单一成本百分比。
11. **加仓来自目标缺口**：价格上涨、下跌、同价，模型更新或新报文本身都不是充分条件；只有新计划下的正库存缺口、目标一致性和正边际得分才能加仓。
12. **未知订单先对账**：提交结果未知、部分成交或订单事实冲突时，状态进入RECONCILE_REQUIRED并阻断新增。
13. **只用当时可见信息**：任何回测、归因或参数晋级必须满足strict-as-of，禁止未来模型运行、修订TAF或最终盘口泄漏；历史角色回补只修正归类，不计为时间新增。
14. **推断不等于生产授权**：对手行为的强关联推断只能产生研究候选，必须经过独立回放、风控和审批才能成为生产规则。
15. **全部动作可追溯**：计划、intent、订单、成交、持仓和结算必须绑定同一决策版本与证据快照。
16. **变更原子化**：模型、公式、阈值、资本上限和状态机版本不得热拼接；晋级和回滚必须按完整版本执行。
17. **沉默失败禁止**：证据过期、可执行盘口缺失、深度不足、预算冲突和PnL无法闭合都必须产生明确阻断原因；历史参考价不得替代盘口放行。
18. **研究与生产隔离**：本研究宪章不修改现有L0生产宪章、不启动服务、不提交订单，也不构成复制对手策略的授权。

## 策略语言

用少量受控原语表达选桶、偏置、配仓和执行，而不是让自然语言直接驱动下单。

### 原语

- `CONTRACT`：冻结站点、目标日、resolution source和bucket map
- `CENTER`：声明候选中心及其lineage
- `DISTRIBUTION`：声明连续温度分布、桶概率和归一校验
- `CANDIDATE_BOUNDARY`：声明经分歧封顶的最远候选搜索范围
- `SUPPORT`：声明由组合概率与联合目标选择的稀疏最终支持集
- `BIAS`：声明UP/DOWN/NEUTRAL、强度、置信度与TTL
- `BUDGET`：声明总资本、站点/事件上限和角色约束
- `TARGET`：声明联合最优目标份额、对应成本和最大有效价格
- `CHECKPOINT`：冻结触发向量、形成成熟度和复核时点
- `QUOTE`：冻结官方可执行bid/ask/depth及TTL
- `BUNDLE`：声明全部正库存缺口对应的完整Maker腿、期限和原子资本预留
- 初始建仓：首次建立核心腿
- 向一侧补仓：增加低端或高端桶
- 补齐中间缺口：增加已选低端与高端之间的缺失桶
- 补足已有桶：对已存在桶补目标库存
- `REVIEW`：在检查点重新计算目标但不自动生成加仓
- 保持现状：保持持仓且不新增
- 停止新增：停止本轮新增，不等于卖出
- `RECONCILE`：闭合未知订单、成交和库存事实
- `SETTLE`：按官方结果计算逐情景与实际PnL

### 语法

- `PLAN := CONTRACT EVIDENCE CENTER DISTRIBUTION CANDIDATE_BOUNDARY SUPPORT BIAS BUDGET TARGET*`
- `EVIDENCE := MODEL_SET [TAF] [OBSERVATION] HISTORY BOOK`
- `BIAS := UP(strength, confidence, ttl) | DOWN(strength, confidence, ttl) | NEUTRAL`
- `CHECKPOINT := delta(model, taf, observation, book, order, deadline)`
- `ACTION := REVIEW | 初始建仓 | 向低温侧补仓 | 向高温侧补仓 | 补齐中间缺口 | 补足已有桶 | 保持现状 | 停止新增 | RECONCILE`
- `ORDER := ACTION complete_bundle quantity_by_bucket limit_by_bucket order_mode evidence_id plan_version`
- `TRANSITION := state + trigger + guards -> action + next_state`

### 示例

```text
CONTRACT ZUUU 2026-07-23 C buckets[40,41,42]
CENTER 40.3 source=station_calibrated_model_cluster
DISTRIBUTION p40=P40 p41=P41 p42=P42 normalization=PASS
CANDIDATE_BOUNDARY low=39 high=43 max_extension_layers=2
SUPPORT buckets[40,42] sparse=true
BIAS UP strength=0.62 confidence=MEDIUM ttl=6h
BUDGET total<=K_max concentration_caps=PASS
TARGET 40C cost=k40 shares=q40 max_price=a40_max dual_residual=PASS
CHECKPOINT delta_book=1 delta_deadline=0 maturity=M quote_id=Q
BUNDLE buckets[40,42] maker ttl=15m atomic_reservation=PASS
REVIEW then REPLACE_COMPLETE_BUNDLE evidence_id=EVID plan_version=V
```

## 数学公式

### M01 站点模型单日误差 [数据定义]

`e(s,m,d,r,h) = T_actual(s,d) - T_forecast(s,m,d,r,h)`

- 变量：s站点；m模型；d目标日；r运行时次；h提前量；正值表示模型偏冷。
- 用途：构造站点×模型×季节×时次×提前量误差分布。
- 边界：T_actual必须是冻结resolution口径下的历史日最高温，不得用预报页面最高温代替。

### M02 带时间衰减的历史误差 [候选估计式]

`bias_raw = sum_t[exp(-age_t/tau) * e_t] / sum_t[exp(-age_t/tau)]`

- 变量：tau为半衰尺度接口；age_t为样本距目标日的时间。
- 用途：让近期同季节误差比远期样本权重更高。
- 边界：tau必须离线验证；短样本不能称为长期误差。

### M03 站点误差收缩 [候选估计式]

`bias_shrunk = n/(n+kappa) * bias_raw + kappa/(n+kappa) * bias_parent`

- 变量：n为有效样本量；kappa为收缩强度；bias_parent为城市群/气候区/全局先验。
- 用途：避免小样本站点产生极端固定上修或下修。
- 边界：kappa属于版本化参数，不能按单日盈亏临时改动。

### M04 模型可靠度权重 [候选估计式]

`w_m = rel_m * exp(-lambda * MAE_m) / sum_j[rel_j * exp(-lambda * MAE_j)]`

- 变量：rel_m为数据完整性/新鲜度系数；lambda控制MAE差异的放大程度。
- 用途：把历史表现转化为先验权重而不是永久赢家。
- 边界：权重必须按站点、季节、时次和提前量分层；不能只用全局MAE。

### M05 当日模型离群闸门 [候选决策式]

`g_m = 1 if abs(F_m - median(F)) <= q_outlier else gamma_outlier`

- 变量：gamma_outlier在[0,1]；q_outlier为当日分裂阈值。
- 用途：允许双模型聚类在特定日期降低离群模型影响。
- 边界：报告只支持‘候选离群闸门’，不支持已识别精确q_outlier。

### M06 站点化候选中心 [候选决策式]

`C_model = sum_m[w_m * g_m * (F_m + bias_shrunk_m)] / sum_m[w_m * g_m]`

- 变量：F_m为同一as-of下的目标日最高温预报。
- 用途：得到模型与站点误差共同校准的连续中心。
- 边界：不能把C_model直接四舍五入后视为唯一主桶。

### M07 TAF有效性混合 [候选决策式]

`C = (1-alpha_taf) * C_model + alpha_taf * TAF_TX`

- 变量：alpha_taf = coverage * validity * freshness * calibrated_skill，范围[0,1]。
- 用途：TAF在有效窗、覆盖完整且经校准时作为二级锚。
- 边界：TAF覆盖缺失或有效窗越过日峰时alpha_taf必须降为0。

### M08 总不确定度 [候选估计式]

`sigma_total^2 = sigma_model^2 + sigma_station^2 + sigma_obs^2 + sigma_resolution^2`

- 变量：分别代表模型分歧、站点残差、实况未成熟度和结算映射误差。
- 用途：把选桶带宽与多种不确定性绑定。
- 边界：各项相关时不得机械平方相加，应改用协方差矩阵。

### M09 连续选桶带 [候选决策式]

`S(z) = {b_i: interval(b_i) intersects [C - z*sigma_total, C + z*sigma_total]}`

- 变量：z由目标覆盖率和资本约束决定。
- 用途：把中心与总不确定度投影到离散桶。
- 边界：S默认连续；若跨桶必须单独记录内部缺口理由。

### M10 选桶集合优化 [候选优化式]

`S* = argmax_S [CoverageProb(S) - lam_width*|S| - lam_cost*Cost(S) - lam_gap*GapPenalty(S)]`

- 变量：S受连续性、预算、深度和结算合同约束。
- 用途：在覆盖、桶数、成本和内部缺口之间做显式权衡。
- 边界：lambda参数必须离线晋级；不可用单次事件反推。

### M11 方向偏置分数 [候选决策式]

`D = beta1*z_cluster + beta2*z_taf + beta3*z_station + beta4*z_cross_station - beta5*z_maturity_risk`

- 变量：各z项必须是同as-of标准化信号；D>tau_up为UP，D<tau_down为DOWN，否则NEUTRAL。
- 用途：把方向偏置从隐含形容词变成有门槛的对象。
- 边界：7/22跨站暖向只支持cross_station信号候选，不提供精确beta或tau。

### M12 偏置强度与TTL [硬接口]

`BiasObject = (direction, strength=clip(abs(D),0,1), confidence, created_at, expires_at, invalidators)`

- 变量：invalidators包括新模型聚类反转、TAF撤销、实况硬下界冲突和证据过期。
- 用途：保证偏置可撤销、可过期、可审计。
- 边界：没有expires_at的偏置对象不得进入配仓。

### M13 成本中心与份额中心 [数据定义]

`mu_cost = sum_i[q_i*a_i*t_i]/sum_i[q_i*a_i]; mu_share = sum_i[q_i*t_i]/sum_i[q_i]`

- 变量：q_i为YES份额；a_i为有效均价；t_i为桶中心。
- 用途：区分资本重心与情景收益重心。
- 边界：开口桶需采用冻结代表值或区间方法并显式标记。

### M14 中心偏移 [数据定义]

`offset_share = mu_share - C; offset_cost = mu_cost - C`

- 变量：正值为相对候选中心偏上。
- 用途：描述方向与配仓形态。
- 边界：若C不是同时点中心，只能称桶内偏移，不能称天气模型偏差。

### M15 联合资本与角色约束 [硬约束]

`Capital(q) <= K_max; RoleCaps(q, role) <= promoted_caps`

- 变量：`role`只标识覆盖、偏置或边缘约束；资本只预留一次。
- 用途：在同一联合优化中防止方向或外围桶突破总预算和集中度。
- 边界：禁止把角色分别归一、分别求目标后相加。

### M16 联合目标库存 [硬优化式]

`q* = argmax_q J(p_stage,q,Book_t,inventory,risk,budget,role_caps)`

- 变量：`J`是唯一`ΔBasketScore v2`状态价值；成本由`q*×all_in_price`派生。
- 用途：同一次求解闭合成本、份额、全情景PnL、覆盖和集中度。
- 边界：历史`CAPITAL_FIRST/SHARE_FIRST`与三本预算相加公式已退役，不得进入生产候选闭包。

### M17 每美元凸性倍数 [数据定义]

`convexity_i = (1 - a_i_eff) / a_i_eff`

- 变量：若投入1美元且桶i获胜，忽略费用时净利润倍数。
- 用途：仅用于事后解释单桶命中收益曲线。
- 边界：禁止进入概率、支持集或按`1/a_i_eff`排序；只能由完整联合PnL间接体现。

### M18 逐情景篮子利润 [硬核算式]

`PnL(j) = q_j - sum_i[q_i*a_i_eff] - fees if j in S; else -sum_i[q_i*a_i_eff] - fees`

- 变量：j为最终赢家桶；S为已选YES桶集合。
- 用途：计算每个结算情景的净利润。
- 边界：必须使用真实份额和all-in成本，不能用平均价和总份额的错误乘积。

### M19 条件结算地板 [硬约束]

`Floor(S) = min_j_in_S[PnL(j)]; conditionally_protected iff Floor(S) >= 0`

- 变量：只对赢家位于S内的情景成立。
- 用途：严格定义何时可以说条件保本。
- 边界：若赢家落在S外，损失仍可能等于全部成本与费用。

### M20 等份篮子价差 [硬核算式]

`For q_i=q: PnL(j in S) = q*(1 - sum_i[a_i_eff]) - fees`

- 变量：仅在每桶购买相同份额q时成立。
- 用途：解释为何sum(all-in YES ask)<1可产生选中集合内地板。
- 边界：这是可选诊断公式，不是动态篮子准入条件；不同份额、深度滑点或集合外赢家会破坏该简式。

### M21 库存缺口 [硬接口]

`gap_i = max(0, q_i_target - q_i_filled - q_i_working)`

- 变量：working必须只计可确认仍有效的挂单数量。
- 用途：把加仓定义为目标库存的未满足部分。
- 边界：订单状态未知时q_i_working不可信，必须先RECONCILE。

### M22 边际加仓期望值 [候选诊断式]

`DeltaEV_i(dq) = dq*(p_i - a_i_eff) - fees_i(dq) - slippage_i(dq)`

- 变量：p_i由M31从不含盘口反馈的连续温度分布投影；dq为拟新增份额。
- 用途：衡量新增一小批份额的概率价值。
- 边界：覆盖底座可能接受局部负DeltaEV以改善情景地板，因此不能只看EV。

### M23 边际加仓综合得分 [候选决策式]

`Score_i = w_ev*DeltaEV_i + w_gap*GapNorm_i + w_floor*DeltaFloor_i + w_bias*Align_i - w_stale*Stale_i - w_conc*DeltaConcentration_i`

- 变量：各项必须标准化且绑定版本。
- 用途：统一回答什么时候补、加仓或不加。
- 边界：Score_i>tau只是候选门槛，还必须通过预算、深度、新鲜度和订单闭合硬门。

### M24 真实深度有效价格 [硬核算式]

`a_i_eff(Q) = [sum_l price_l * fill_l(Q) + fees(Q)] / Q`

- 变量：沿卖盘逐档吃到目标份额Q。
- 用途：替代仅用best ask的虚假可执行成本。
- 边界：盘口快照过期或深度不足时不得输出可执行计划。

### M25 限价上限 [候选执行式]

`limit_i_max = min(fair_i - edge_min, floor_preserving_price_i, budget_affordable_price_i)`

- 变量：fair_i来自天气概率；另外两项来自组合地板和预算。
- 用途：把证据、组合和资本约束转化为挂单价格上限。
- 边界：报告只能观察成交价，不能识别对手真实edge_min或挂单队列策略。

### M26 成本与份额集中度 [诊断定义]

`HHI_cost = sum_i[(cost_i/Cost)^2]; HHI_share = sum_i[(q_i/Q)^2]`

- 变量：范围(0,1]，越高越集中。
- 用途：监控首建集中、后续扩腿和带内主仓切换。
- 边界：不同桶数的HHI需与1/|S|基线比较。

### M27 最大情景损失 [硬风险式]

`L_max = max_j[-PnL(j)] over all contract outcomes j`

- 变量：包括S外赢家、费用和未决订单保守占用。
- 用途：给总资本和边缘桶设置上限。
- 边界：若未覆盖全部市场桶，S外情景必须进入计算。

### M28 路径PnL分解 [诊断定义]

`PnL_total = PnL_base + PnL_bias + PnL_edge - execution_cost - fees`

- 变量：各分量用冻结的反事实基线计划计算。
- 用途：区分覆盖底座、方向观点、边缘凸性与执行质量。
- 边界：分解依赖反事实定义，不等于账户会计已实现收益。

### M29 停止加仓判定 [硬状态式]

`ADD_i = 1 iff CheckpointChanged=1 and gap_i>0 and Score_i>tau and TargetConsistent=1 and Fresh=1 and BudgetOK=1 and DepthOK=1 and OrdersClosed=1`

- 变量：任一硬门失败即ADD_i=0。
- 用途：明确什么时候会不加仓。
- 边界：停止新增只停止新增，不自动意味着卖出或策略反向。

### M30 偏置归因增益 [验证定义]

`Gain_bias = Metric(strategy_with_bias) - Metric(same_system_neutral_bias)`

- 变量：必须保持选桶、价格、资本、时间和执行规则可比。
- 用途：检验方向偏置是否真的增加PnL、命中或风险调整收益。
- 边界：不能把全账户结果与不同时间、不同覆盖策略直接比较。

### M31 连续分布投影为桶概率 [硬接口]

`p_i = integral_[L_i,U_i] f_s,d(T | Evidence_asof) dT; p_i>=0; sum_i p_i=1`

- 变量：[L_i,U_i]按MarketContract定义；开口桶使用负/正无穷边界；f不得吸收盘口价格。
- 用途：为选桶覆盖率、fair_i和DeltaEV提供同源、归一的离散概率。
- 边界：分布族、尾部和协方差需严格前推校准；概率不是从成交价反推。

### M32 成本与份额双目标一致性 [硬约束]

`R_k=max_i|k_i_target-q_i_target*a_i_eff|; R_q=max_i|q_i_target-k_i_target/a_i_eff|; TargetConsistent=1 iff R_k<=eps_k and R_q<=eps_q and sum_i k_i_target<=K_max`

- 变量：eps_k、eps_q为版本化数值容差；有效价必须来自同一盘口快照。
- 用途：防止资本优先和份额优先在不同价格或版本上各自成立却彼此矛盾。
- 边界：任何一项残差超限时只能重算计划，不得通过四舍五入静默放行。

### M33 形成成熟度向量 [数据定义]

`M_t=(elapsed_since_first_fill, cumulative_cost/final_or_target_cost, selected_bucket_count/target_bucket_count, checkpoint_count, unresolved_order_count)`

- 变量：历史事件可用最终成本；形成中事件只能用当时目标成本并标记unresolved。
- 用途：区分形成早晚、资本进度、桶覆盖、信息检查点和订单闭合程度。
- 边界：M_t是向量而非单一百分比；形成中事件不得使用未来最终成本或赢家。

### M34 同成熟度历史基线 [验证定义]

`Baseline_s(M_t)=distribution{metric_e(cutoff=first_fill_e+elapsed_s,t) | station=s, event_date<d}; percentile_t=ECDF_Baseline(metric_t)`

- 变量：按站点使用与当前事件相同的形成经过时长裁切历史事件。
- 用途：判断当前偏置或扩桶是否只是形成尚早，避免把未完成篮子与历史最终仓位直接比较。
- 边界：只提供描述性异常度；当前未结算事件不赋赢家或PnL。

### M35 嵌套窗口统计 [诊断定义]

`theta_nested(W)=A({e: d-W<date_e<=d}); W in {3,7,15,30}`

- 变量：A为固定定义的事件级聚合；较长窗口包含较短窗口。
- 用途：比较不同观察长度下的策略形态和经济性。
- 边界：theta_nested(W1)-theta_nested(W2)不是时间变化量，不得据此宣称策略版本切换。

### M36 互斥分段演进 [验证定义]

`theta_r=A(E_r), E_r intersect E_k=empty for r!=k; Delta_r=theta_r-theta_(r-1)`

- 变量：本报告E_r依次为16-30天前、8-15天前、4-7天前、最近3天。
- 用途：描述真实时间段之间的策略演进与方向折返。
- 边界：Delta仍是描述性差异，需控制城市组合、天气难度、流动性和未结算选择后才能归因代码变更。

### M37 离散clip与填单成本 [数据定义]

`filled_cost_i=sum_j(c_ij*rho_ij); rho_ij=filled_notional_ij/identified_original_notional_ij; cash_core=argmax_i(filled_cost_i)`

- 变量：c_ij为订单族原始现金clip；rho_ij范围[0,1]。
- 用途：解释主成本桶由更多订单尝试、填单率和后续补单累积形成，而非单笔规模更大。
- 边界：从未成交或已撤销的链下订单不可见，不能把已成交订单数等同全部下单意图。

### M38 Maker转受限单桶FOK紧迫度 [候选执行式]

`U_i(t)=w_d*DeadlinePressure(t)+w_g*GapNorm_i+w_f*(1-FillRate_i)-w_p*AggressivePremium_i; use_FOK iff maker_cancel_confirmed and one_gap_left and U_i>tau_U and a_i_eff_FOK<=limit_i_max`

- 变量：DeadlinePressure随剩余时间缩短上升；AggressivePremium是主动成交相对Maker目标价的成本。
- 用途：把等待低价与及时补足目标库存之间的权衡显式化。
- 边界：报告只支持候选机制，不识别精确权重或tau_U；Maker未确认撤销、仍有多桶缺口或不存在正缺口时不得使用FOK。

### M39 成交相对参考价偏离 [诊断定义]

`delta_ref_i(t)=p_fill_i(t)-p_ref_i(t;lookback); executable=false unless contemporaneous Book_t exists`

- 变量：p_ref来自成交/参考序列；Book_t是同一时点官方逐档盘口。
- 用途：描述Maker成交发生在参考序列上方或下方，并与个案时间线对齐。
- 边界：delta_ref不能证明挂单时点、最大限价、队列位置或可执行滑点。

### M40 形成后新订单族占比 [数据定义]

`R_new=late_cost_from_first_seen_order_hashes/late_total_cost`

- 变量：first_seen仅指此前成交记录中未出现的订单哈希。
- 用途：区分旧订单继续漏斗式成交与形成后新订单族贡献。
- 边界：新哈希不必然等于新观点，可能是补单、改单、过期重挂或执行重试。

### M41 检查点触发向量 [硬接口]

`X_t=(Delta_model,Delta_taf,Delta_observation,Delta_book,Delta_order,Delta_deadline); CheckpointChanged=1 iff any component crosses its versioned materiality threshold`

- 变量：每个分量保存旧值、新值、as_of、lineage、阈值和触发原因。
- 用途：统一触发REVIEW，随后才重算中心、目标、缺口和执行。
- 边界：检查点变化只是复核条件，不是加仓充分条件；无变化时禁止定时器制造虚假新计划。

### M42 跨站方向迁移闸门 [硬约束]

`Transfer(s0->s,d)=1 iff same_regime=1 and station_calibration_s_valid=1 and sign_consistency>=tau_sign and effect_stability>=tau_stability; otherwise cross_station_signal=0`

- 变量：same_regime需在当时可见证据上定义；站点自身校准始终优先。
- 用途：防止把成都、八站组合或账户级近期方向直接复制到另一站点。
- 边界：当前证据不足以标定tau_sign和tau_stability；7/22跨站暖向只能作为弱日期regime候选。

## 公式证据索引

- **M01-M06** <- E01, E13, E14, E16：这些是已退役的三确定性模型行为研究基线，只保留为 benchmark/ablation，不能授权 P0、候选中心或生产选桶。
- **M07-M12** <- E02, E11, E14, E22：TAF、模型分歧和跨站信号约束宽度与临时偏置。
- **M13-M17** <- E09, E10, E16：成本与份额分离，支持双目标和三本预算但不识别比例。
- **M18-M20** <- E06, E23：逐情景核算锁定条件保本边界。
- **M21-M30** <- E03, E04, E07, E08, E18, E23：目标缺口、价格、风险和归因共同控制增量行为。
- **M31-M32** <- E09, E10, E15：概率投影和双目标一致性是规范性补强。
- **M33-M36** <- E03, E04, E19, E21, E22：成熟度与双窗口方法约束演进结论。
- **M37-M40** <- E05, E12, E17, E20, E23：订单族、clip、填单率、紧迫度和参考价边界。
- **M41-M42** <- E14, E19, E21, E22：检查点复核与跨站迁移防止错误泛化。

## 状态机

- `UNBOUND`：市场合同或来源未冻结；允许：补合同；禁止：计算中心、下单。
- `EVIDENCE_READY`：证据快照完整且新鲜；允许：计算C、sigma、D；禁止：跳过L3直接下单。
- `PLAN_READY`：桶、预算、目标库存和情景PnL闭合；允许：生成intent；禁止：把计划计为持仓。
- `核心建仓中`：首次建立核心覆盖腿；允许：按批次初始建仓；禁止：无上限一次建满。
- `覆盖扩展中`：增加低/高/内部缺口桶；允许：向一侧补仓或补齐中间缺口；禁止：不重算情景PnL扩桶。
- `库存补足中`：温度带不变但目标缺口为正；允许：补足已有桶；禁止：把补库存描述为重新选带。
- `成熟保持`：无正缺口或新增边际不足；允许：保持现状、监控；禁止：因无成交自动推断观点消失。
- `已停止新增`：预算、新鲜度、深度、成熟度或风险门阻断新增；允许：等待新检查点；禁止：静默绕过阻断。
- `必须对账`：订单、成交或库存事实不闭合；允许：只做对账；禁止：任何新增intent。
- `已结算`：官方结果锁定并完成PnL归因；允许：审计、复盘、研究晋级；禁止：用结算后信息改写当时证据。

## 状态转换

- `UNBOUND` + 合同与来源冻结 [bucket map完整] -> 建立L1合同 -> `EVIDENCE_READY`
- `EVIDENCE_READY` + 证据齐备 [as-of、lineage、新鲜度、桶概率归一通过] -> 计算中心、宽度、偏置、目标模式、预算和情景PnL -> `PLAN_READY`
- `PLAN_READY` + 存在核心桶正缺口 [目标双残差/预算/真实深度/订单闭合] -> 按离散CLIP执行初始建仓 -> `核心建仓中`
- `核心建仓中` + X_t检查点变化 [先REVIEW且带宽应扩大、边际风险通过] -> 向低温侧/高温侧补仓或补齐中间缺口 -> `覆盖扩展中`
- `核心建仓中` + X_t变化且温度带不变 [REVIEW后缺口、目标一致性、Score与硬门通过] -> 补足已有桶 -> `库存补足中`
- `覆盖扩展中` + 全部当前目标缺口归零 [无working/未知订单] -> 保持现状 -> `成熟保持`
- `库存补足中` + 目标缺口归零或边际得分失效 [库存闭合] -> 保持现状 -> `成熟保持`
- `成熟保持` + X_t变化后产生新目标缺口 [REVIEW、计划版本更新且全部硬门通过] -> 向一侧补仓或补足已有桶 -> `覆盖扩展中`
- `ANY_ACTIVE` + 证据过期、预算冲突、深度不足或风险超限 [任一硬门失败] -> 停止新增 -> `已停止新增`
- `ANY_ACTIVE` + 订单结果未知或库存冲突 [事实不闭合] -> 进入对账 -> `必须对账`
- `必须对账` + 订单和库存唯一闭合 [审计通过] -> 重算计划 -> `PLAN_READY`
- `ANY_NON_SETTLED` + 官方结算锁定 [resolution source确认] -> 结算与归因 -> `已结算`

## 端到端流程

### F0 合同绑定 - 先确定交易的真值和离散化规则

- 登记站点和本地目标日
- 冻结resolution source与时区
- 冻结温度单位、桶边界和开口桶表示
- 生成contract_id与bucket_map_hash
- 退出门：合同完整且唯一

### F1 证据快照 - 形成严格as-of的天气和市场事实

- 读取已保留、同一 `decision_at` 可见且与已激活 exact mask 一致的至少两个独立家族官方原生集合运行；已成功的内容寻址 IFS/GEFS/ECCC 原始对象不得为日常编译重复抓取
- 读取TAF issue、原始修订链、validity和目标峰值覆盖
- 读取官方实况和已报硬下界
- 加载站点×模型×季节×时次×提前量误差
- 冻结官方可执行盘口深度、持仓和working orders
- 将历史参考价单独标记为non_executable
- 退出门：每个输入有lineage、新鲜度、角色归类和缺失原因

### F2 中心、宽度与偏置 - 把天气证据变成候选分布对象

- 计算历史可靠度权重
- 执行当日模型离群闸门
- 计算C与sigma_total
- 将TAF作为有条件二级锚
- 计算BiasObject并设置TTL与撤销条件
- 退出门：C、sigma、D、confidence和TTL闭合

### F3 支持集与联合目标库存

- 将连续分布积分为互斥桶概率并归一
- 计算中心领先差与概率侧质量方向
- 用模型分歧确定最多两层的候选边界
- 以组合概率、可靠性和联合经济价值选择稀疏支持集
- 用唯一`ΔBasketScore v2`状态价值联合求目标份额与对应成本
- 计算所有结算情景PnL、Floor(S)和L_max
- 标记主成本/主份额候选桶
- 退出门：`S⊆C⊆A`、概率、预算、情景风险和双账本全部通过

### F4 完整Maker bundle首建

- 记录形成成熟度向量
- 计算真实深度有效价格
- 为全部正库存缺口同时生成Maker腿
- 原子校验bundle完整性、资本预留和`ΔBasketScore v2`
- 禁止先买单桶；紧迫单桶FOK必须先确认撤销Maker
- 原子更新成交、库存、working order和资本预留
- 退出门：完整bundle进入等待、部分成交后重算或整篮HOLD

### F5 扩桶与追补 - 根据新检查点调整覆盖范围或库存排序

- 冻结M41触发向量并执行CHECKPOINT REVIEW
- 更新成熟度和可执行盘口
- 先读取正式报文生成有效桶集合`A_t`；报文确认30℃时删除29℃及更低桶
- 从`A_t`中重新归一化概率，重算计划版本和双目标残差
- 比较新旧桶集合和目标库存
- 对全部正缺口重新生成完整Maker bundle；被排除桶不再进入新篮子
- 联合计算`ΔBasketScore v2`、情景PnL、紧迫度和集中度变化
- 监控主成本桶与主份额桶切换
- 被排除桶的历史成交不删除，单独进入对账，不计入新篮子的概率、成本、覆盖和收益地板
- 退出门：缺口归零、边际不足、预算用尽或证据过期

### F6 停止、持有与对账 - 在不确定状态下保护事实一致性

- 无检查点变化或无缺口时保持现状
- 证据/风险/真实深度失败时停止新增
- working order等待时不重复占用库存
- 未知订单时RECONCILE_REQUIRED
- 新检查点到来后从闭合库存重算
- 退出门：恢复PLAN_READY或进入SETTLED

### F7 结算与归因 - 把结果拆成覆盖、偏置、凸性和执行质量

- 锁定官方赢家桶
- 计算逐情景与实际PnL
- 冻结中性偏置反事实
- 分解PnL_base/PnL_bias/PnL_edge/execution_cost
- 更新研究样本但不自动晋级生产
- 退出门：报告、数据、验证和版本审计完成

## 边界

- **可识别**：边界内=成交时间、桶、价格、份额、订单哈希、已成交配仓演进、主桶切换、模型/TAF/实况冻结数据、路径PnL和已匹配订单族填单率；边界外=无。
- **只能推断**：边界内=校准后的模型组合概率给出候选中心与核心支持集、分歧只给出经策略封顶的外扩层数和集中度压力、价格和库存塑造目标、边缘桶提供条件凸性、Maker到受限FOK紧迫机制；边界外=精确权重、阈值、目标库存函数和下单队列规则。模型极值只作分歧诊断，不直接生成候选边界或最终支持集。
- **不可识别**：边界内=无；边界外=私有模型、从未成交挂单、撤单原因、完整历史可执行盘口与队列、真实风险函数、账户会计收益。
- **天气边界**：边界内=带as-of的模型、TAF、官方实况和历史误差；边界外=用盘口价格反推并覆盖天气概率。
- **执行边界**：边界内=同一时点官方真实深度、限价、库存缺口、订单状态、clip和资本预留；边界外=在执行层重算天气中心或用历史参考价替代可执行盘口。
- **保本边界**：边界内=对明确S内情景计算的费用后Floor(S)；边界外=把多桶、低均价或sum ask<1泛化为全市场保本。
- **部署边界**：边界内=研究文档、离线模拟、shadow和dry-run候选；边界外=直接修改生产L0/L1、启动服务或提交订单。

## 不变量

- contract_id、bucket_map_hash和resolution source缺一不可。
- 所有天气输入必须满足同目标日、同站点和可证明as-of。
- 桶概率必须由同一连续分布投影，覆盖开口桶且总和为1。
- BiasObject缺direction、confidence、TTL或invalidators任一字段即无效。
- 联合目标资本不得超过K_max，角色约束不得重复计算资本，预留不得跨事件复用。
- 每桶cost、shares、filled、working和reserved必须同一版本闭合。
- 每个InventoryPlan必须引用唯一联合目标与`ΔBasketScore v2`权重SHA，并通过成本与份额双账本闭合。
- PnL必须覆盖全部合同结果，包括已选集合外赢家。
- 任何‘保本’结论必须引用公式M18-M20的逐情景输出。
- 主成本桶与主份额桶分列保存，禁止用单一main_bucket字段混合。
- 未知订单、部分成交或库存冲突时只允许RECONCILE。
- 盘口深度过期或不足时不输出可执行配仓。
- 10分钟参考价或任何prices-history不得替代同一时点官方bid/ask/depth。
- 形成中事件必须标记as_of、成熟度和unresolved，禁止提前赋赢家或PnL。
- 嵌套窗口差异不得被表述为时间演进；方向迁移必须使用互斥窗口和站点闸门。
- 模型、TAF或实况更新只生成新计划版本，不直接修改已成交历史。
- 离线验证失败只阻止候选版本晋级，不得偷偷fallback到未登记公式。
- 研究结论不得自动变成生产授权。

## 治理门

- **G0 语义审查**：定义、单位、时区、结算和证据等级是否唯一？（责任：策略/数据治理）
- **G1 数据审查**：原始数据、as-of、lineage、缺失和去重是否可信？（责任：数据工程）
- **G2 数学审查**：公式、约束、数值稳定性和边界情景是否通过？（责任：量化研究）
- **G3 回放审查**：跨站点、跨季节、3/7/15/30天与严格前推表现是否稳定？（责任：研究验证）
- **G4 执行审查**：深度、限价、滑点、部分成交、幂等和对账是否闭合？（责任：交易工程）
- **G5 风险审查**：L_max、集中度、预算、停牌和未知订单是否受控？（责任：风险控制）
- **G6 Shadow/Dry-run**：不下单条件下能否稳定生成同一决策和审计轨迹？（责任：运行治理）
- **G7 原子晋级**：完整版本是否被批准且可整体回滚？（责任：生产授权人）

## 实现合同

| 合同 | 必填字段 |
|---|---|
| `MarketContract` | `contract_id, station_id, resolution_local_date, timezone, unit, resolution_source, bucket_map_hash` |
| `EvidenceSnapshot` | `evidence_id, as_of, source_ids, raw_hashes, freshness, missing_reasons, model_runs, model_visibility, taf_issue, taf_revision_chain, taf_validity, observations` |
| `DistributionObject` | `distribution_id, evidence_id, family, parameters, bucket_probabilities, normalization_residual, tail_method` |
| `BiasObject` | `direction, strength, confidence, created_at, expires_at, invalidators, evidence_id` |
| `InventoryPlan` | `plan_version, distribution_id, selected_buckets, candidate_boundary, target_shares, target_cost, role_caps, residual_cost, residual_shares, max_price, scenario_pnl, delta_policy_sha256` |
| `CheckpointSnapshot` | `checkpoint_id, trigger_vector, maturity_vector, old_values, new_values, thresholds, as_of, evidence_id` |
| `ExecutableQuote` | `quote_id, token_id, as_of, bid_depth, ask_depth, ttl, source, effective_prices` |
| `OrderIntent` | `intent_id, bundle_id, plan_version, checkpoint_id, quote_id, bucket, action, order_mode, quantity, limit_price, expected_order_id, capital_reservation` |
| `InventoryFact` | `filled_shares, filled_cost, working_shares, reserved_capital, main_cost_bucket, main_share_bucket, version` |
| `DecisionFact` | `state, trigger, guards, decision, block_reason, evidence_id, plan_version, timestamp` |
| `SettlementAttribution` | `winner_bucket, pnl_actual, pnl_by_scenario, pnl_base, pnl_bias, pnl_edge, execution_cost, fees` |

## 证据映射

- **E01 [F, RETIRED_BEHAVIORAL_BENCHMARK]** 306个事件中79.1%的早期核心选择靠近三确定性模型包络的最暖或最冷端。该证据只保留为历史行为基线，不是当前 P0 权威。来源：原报告第1-2节；derived/model_selection_behavior.csv。
- **E02 [F]** TAF数值TX覆盖约14.4%。 支持：TAF只能是条件二级锚，不能解释全局选桶。 来源：原报告第1、3节；derived/taf_model_comparison.csv。
- **E03 [F]** 74.1%的事件在25%成本点桶数少于最终桶数。 支持：首建集中、后续扩腿是稳定形成特征。 来源：原报告第6节；formation stage outputs。
- **E04 [F]** 40.4%的事件最终主桶相对早期发生变化。 支持：主桶是动态状态而非静态身份。 来源：原报告第6节；formation stage outputs。
- **E05 [F]** 形成后新增成本99.51%来自新订单哈希。 支持：后续成交主要是新意图/新订单，不是单一旧单被动漏斗。 来源：原报告第1、6节；order hash analysis。
- **E06 [F]** 30天严格条件保本事件占比约11.9%，聚合完整买入路径ROI约2.86%。 支持：多桶不等于普遍严格保本，且窗口聚合收益不能与模型包络极端选择率混用。 来源：原报告第1、8节；derived/settled_window_economics.csv；derived/strategy_window_evolution.csv。
- **E07 [F]** 7/23的29个新篮子中27个为多桶，均桶数2.03->2.66，主桶平均成本占比81.9%->71.4%。 支持：外样本继续支持先集中、后扩覆盖。 来源：原报告第9E节；derived/wallet_latest_24h_headline.json。
- **E08 [F]** 卡拉奇未扩桶，但同桶补入后主成本桶由34C切至33C。 支持：不扩桶不等于主仓排序不变。 来源：原报告第9E节；derived/wallet_current_build_capture_chain.csv。
- **E09 [F]** 首价与最终成本占比相关约+0.409，与最终份额占比约+0.015。 支持：成本重仓和份额重仓必须分离。 来源：原报告第9D节；derived/formation_maturity_entry_relationships.csv。
- **E10 [F]** 距修正中心越远，成本占比相关约-0.346，份额占比仅约-0.093。 支持：中心桶承载资本、远端低价桶仍可承载份额凸性。 来源：原报告第9D节；derived/formation_maturity_entry_relationships.csv。
- **E11 [F]** 成都30事件中模型分裂与桶数相关约0.542。 支持：模型分歧更适合作为带宽驱动而非单一方向公式。 来源：原报告第9B节；derived/chengdu_long_behavior_headline.json。
- **E12 [F]** 主桶平均已成交订单哈希20.29个，其他桶9.85个。 支持：资金凸起与更多订单尝试、成交和目标库存追补一致。 来源：原报告第1、6节；order hash analysis。
- **E13 [F]** 严格前推仅46.3%选择站点此前MAE最低模型。 支持：历史最优模型不能直接等于当日主桶。 来源：原报告第1、3节；derived/station_historical_error_walkforward.csv。
- **E14 [F]** 八站GFS冷差与钱包偏移相关0.063，至少6小时子样本0.003。 支持：不存在‘GFS越冷就越向上选桶’的跨站固定公式。 来源：原报告第9C节；derived/china8_formation_behavior_headline.json。
- **E15 [I]** 可识别的最小充分行为链是站点化中心、分歧给带宽、方向闸门、价格/库存决定成交、边缘腿提供凸性。 支持：本操作系统的层级与原语。 来源：原报告第9D结论与第9E外样本验证。
- **E16 [F]** 模型跨度从不超过1C到大于2C时，主桶成本占比53.1%降至45.1%，最终桶数3.74升至4.37，中位总投入升至约164.9 USDC。 支持：模型分歧更适合作为带宽和集中度驱动，不代表不确定时必然少投入。 来源：原报告第5节；derived/deep_model_taf_strategy_headline.json#allocation_by_model_spread。
- **E17 [F]** 20,694个匹配订单族中，最常见原始clip为9.05 USDC并占YES BUY订单19.6%；Maker/Taker加权填单率约39.1%/99.2%。 支持：最终重仓由离散clip、填单率和重复订单族累积形成，并存在Maker等待与主动成交的执行差异。 来源：原报告第7节；derived/deep_model_taf_strategy_headline.json#order_execution。
- **E18 [F]** 30天同价、上涨、下跌加仓成本占比分别约37.6%、30.5%、26.1%。 支持：价格方向本身不是加仓底层驱动，正库存缺口和可接受有效价才是共同条件。 来源：原报告第6、8节；derived/execution_window_evolution.csv。
- **E19 [F]** 互斥分段从平均6.03桶、59.9%连续带、99.8% Maker成本转为3.89桶、89.6%连续带、70.0% Maker成本；严格条件保本16.9%降至8.9%，方向至少两次折返。 支持：近期变化是更窄、更连续、更主动执行，不是统一转暖或加厚保险。 来源：原报告第8、9C节；derived/strategy_window_evolution.csv；derived/execution_window_evolution.csv；derived/china8_formation_behavior_segments.csv。
- **E20 [F]** CLOB prices-history仅是成交/参考价格序列，不包含历史可执行bid/ask/depth、排队位置或未成交挂单。 支持：参考价只能用于事后偏离诊断，不能成为可执行计划输入。 来源：原报告第7节；derived/data_quality.json#public_limitations。
- **E21 [F]** 237个历史事件中97.5%在D-1 20:30后继续买，72.2%新增桶；低桶、高桶、内部缺口新增率约27.0%/25.3%/27.8%。 支持：分阶段形成是常态，但后续扩桶方向不固定，成熟度必须与方向分离。 来源：原报告第9D节；derived/formation_maturity_headline.json。
- **E22 [F]** 2026-07-22形成中八站快照仍未结算，未赋赢家或PnL；同成熟度控制后暖向仍异常，但不能作因果结论。 支持：形成中快照必须标记unresolved并进行同成熟度对照。 来源：原报告第9D节；derived/formation_maturity_headline.json；derived/formation_maturity_validation.json。
- **E23 [F]** 成都7/21的39C在价格急跌后以Maker 0.30/0.21晚成交；7/22的42C均价约0.097，以71.9%成本取得83.2%份额，并在临近截止出现Taker补仓。 支持：成交顺序不同于概率排序，低价份额杠杆、库存追补与紧迫度共同塑造重仓。 来源：原报告第9B节；derived/highest-temperature-in-chengdu-on-july-21-2026_timeline.csv；derived/highest-temperature-in-chengdu-on-july-22-2026_timeline.csv。

## 待验证问题

- 需要多少严格as-of逐档盘口深度与working-order历史，才能识别各桶限价上限、Maker等待和撤单函数？
- 偏置预算对中性反事实的真实增益，在跨站点、跨季节和不同流动性分层中是否稳定？
- 联合目标中的角色约束上限如何随目标日成熟度变化，且不会演化为三套独立配仓？
- 使用站点×季节×时次误差收缩后，是否优于简单模型聚类中心？
- 扩桶与同桶追补的触发更依赖天气检查点、盘口价格还是库存缺口，需要逐秒订单簿才能区分到什么程度？
- 何种停止加仓规则能在不损害覆盖地板的前提下降低尾部资本占用？
- 唯一联合目标在严格前推、相同预算和相同深度下，能否同时闭合成本/份额双账本并优于已退役的两种目标模式？
- 完整Maker bundle、受限单桶FOK紧迫度和新订单族占比能否在逐秒订单簿上稳定外样本复现？
- 跨站方向迁移闸门在跨季节样本中能否优于始终清零cross_station信号的基线？

> 2026-08-01T22:41:27Z SSOT 落地回执：45站 utility v2 已固化完整原始证据合同；当前真实 execution receipt/order event 为 0，故不发 score、不发 BUY NO、不晋级 live。生产仅保留 `com.weather.weather_basket_yes`、`com.weather.market_ws`、`com.weather.clob_order_service` 用户域服务，gateway read-only、live pause 和零订单边界保持。

## 验证

结构化校验：119/119通过。
