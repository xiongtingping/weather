---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-12
deployment_state: v6-pre-event-state-space-production-enabled-v5-retired-no-fallback
supersedes: [legacy-additive-posterior, legacy-v2-v3-probability-fallback]
implementation_symbols:
  - config/production_probability_runtime_activation.json
  - production_probability_v6_activation_contract.py
  - production_probability_v6_event_runtime.py
  - production_probability_v6_public_ssot_writer.py
  - scripts/production_probability_v6_readiness.py
  - docs/adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md
  - docs/contracts/TARGET_REALIZATION_V5.md
  - docs/adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md
  - docs/contracts/TARGET_REALIZATION_V6.md
  - bayes_basket_target/basket_kernel/delta_v2.py
  - bayes_basket_target/delta_v2_runtime.py
  - bayes_basket_target/portfolio_allocator.py
  - bayes_basket_target/portfolio_inventory.py
  - config/trading/station_regional_blocks_v1.json
  - config/trading/portfolio_capital_policy_v1.json
verification_gates:
  - python3 -m pytest tests/test_bayes_basket_target_basket_doctrine_charter.py -q
  - python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  - python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation config/production_probability_runtime_activation.json --event-date $(TZ=Asia/Shanghai date +%F)
  - python3 scripts/target_realization_v6_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v6-generation-2-current
---

# 系统宪章

> **2026-08-05 weather-basket canonical override（当前有效）**：对`/Users/xiong/weather`的四大方案、canonical SSOT、P0/P0-M/P1、纯YES篮子及其三个用户域服务，唯一事实日为`America/New_York`（ET）。正式观测层由唯一优先级合同仲裁独立lineage：可识别美国ASOS站依次选择覆盖完整且新鲜的IEM/NCEI一分钟ASOS（220）、NOAA MADIS HF-METAR五分钟ASOS（210），否则选择官方AWC整点`:00` METAR/有效SPECI（100）；非ASOS站选择AWC。IEM保持`aggregator_republisher`，MADIS保持`official_republisher`，均不得冒充METAR、AWC或native；ASOS须绑定自身历史exact-match P1/OOS artifact才可进入no-live P1，禁止逐值混源取最大；AMSC仅`retired_history_only`。`market_registry`的站点地理/历史市场时区只保留为映射lineage，不能推导或回填canonical日界。weather-basket已退役hard-NO、BUY NO、旧NO服务与任何第四生产服务；PR04是冻结的最终manifest/CAS闸门，PR05–PR16只可产生`MATERIALIZED_UNPUBLISHED / PRODUCTION_NO_LIVE_ONLY / BLOCKED`，不得启动服务、进入篮子、解除live pause或下单。本override不改写本宪章下的历史对象；如有冲突，以本段及项目`AGENTS.md`为准。

> **正式 P0 定义（L0，当前有效）**：active model-product registry唯一包含`ECMWF_IFS_ENS_OPEN_PF50 / ECMWF_AIFS_ENS_GLOBAL / DWD_ICON_GLOBAL_EPS / NOAA_GEFS_05 / ECCC_GEM_GLOBAL_ENS`五套官方原生全球集合成员产品。`FORMAL_P0`只能由同一`station_id × lead_bucket × run_cycle`下至少两个正权重、不同`independence_family_id`系统组合生成；每套系统必须绑定原生成员、可验证arrival/receipt、闭日历史strict-as-of OOS及exact activation。AIFS Single及全部确定性、区域确定性和Open-Meteo republisher产品均退出active registry；其历史对象不得改名或晋级为`FORMAL_P0`、P1或formal/live authority。

> **10站ASOS最高温桶实盘支线（L0，当前有效）**：`KATL / KAUS / KDAL / KHOU / KLAX / KLGA / KMIA / KORD / KSEA / KSFO`在既有`com.weather.weather_basket_yes`服务内部使用独立lane运行，禁止新增第四个生产服务，且不得修改、阻断或消费主线预算。前述PR04与主线live pause继续约束主线，但不作为该支线的发布或下单门。该支线禁止读取P0、P0-M或天气概率；温度触发与目标桶只消费exact ASOS分钟级温度，exact尚未到达时可消费具有明确lineage的MADIS HF-METAR五分钟ASOS档。入场时间边界必须由官方NOAA/NWS逐小时预报曲线的精确`argmax`时段预测并在首个时段开始前按站点×事件日内容寻址冻结；该预报只提供时间门，不得提供温度桶、概率或进入P0/P0-M。官方AWC TAF仅在存在显式`TXnn/DDHHZ`时生成辅助时间引用，用于记录`CONFIRMS_NWS_ARGMAX`或`CONFLICTS_WITH_NWS_ARGMAX`；不得修改、扩大、缩小或阻断NWS时间窗，TAF无TX或与NWS冲突时仍以NWS为准。入场不是相对预测峰值的“峰值前120–30分钟”：仅当观测时刻、接收时刻和下单时刻均处于同一个冻结的精确`argmax`时段内，且该ASOS分钟级事实（或上述MADIS五分钟档）形成新的站点当日已观测最高温后，才立即将该硬事实映射到唯一市场YES温度桶并入场；多个不连续最高温时段之间的空档不属于入场窗口。窗口前后只更新日高基线，不生成交易fact；常规METAR发布时间不定义该支线的入场窗口。同温、旧温、迟到或更正事实不触发。唯一策略参数为支线总预算上限`20 USDC`与每站累计投入上限`2 USDC`；不得增加概率阈值、桶领先幅度、最低概率优势、最高买价或其他策略条件参数。事实lineage、目标市场身份、官方CLOB WS真实卖盘、余额/预算、订单幂等、未知成交reconcile及纯YES禁买NO仍属于非参数执行安全合同。该支线与主线状态、仓位、预算、intent及receipt必须分区记录；任一分区身份不闭合只阻断支线，不得影响主线。

1. 目标类型必须严格区分：物理最高温 `X`、resolution source 结算标量 `S`、市场离散桶 `B`；唯一可交易概率对象是经版本化 `X->S->B` bridge 得到的 `p(B)`。ZUUU 的来源权威已固定：`X` 是目标 resolution-local-date 内 AMSC primary `METAR` 按冻结的本地 44 槽发报时刻表（00:00–03:00 整点；04:00–23:30 每半小时）取得的报文温度最大值；`S` 是同日 Wunderground ZUUU 站点历史观测序列在 `Asia/Shanghai` 日窗内的最高 metric 温度；`B` 只能由该 `S` 和冻结的该市场 bucket contract 导出。唯一正确的 shadow revision 为 `config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json`。v3 的渲染 HTML 会混入未来预报数组，已明确退役；禁止从 `calendarDayTemperatureMax` 等页面预报字段生成 `S`。旧全天 48 槽半点假设同样退役。
2. 天气概率不得读取盘口、价格、成交量、钱包、持仓或 P&L。
3. AMSC/AWC/NCEI/WU 若非本条已登记的 resolution source，只能是观测或训练代理，不能冒充结算真值。上述 `X` 是**报文定义的物理日高**，不是未经观测的连续站点潜在最高温；AMSC `SPECI`、AWC、TWC、WU current 均不能替代该 `X` 或该 `S`。AMSC `SPECI` 与常规 `METAR` 同等级进入实时强观测，但只由固定常规 `METAR` 44 槽在日终定义 `X`。
4. 唯一生产概率模型只能由 `config/production_probability_runtime_activation.json` 冻结，文档、环境变量和调用方不得另行声明“当前模型”。2026-07-16 受管切换后，ADR-0003 的 v6 `pre-event Predict → current-event joint emission → hard condition → normalize → reported-X projection` 是唯一现役 production，composer 固定为 `physical_state_bayes_v6_pre_event_predict_update`；reported-X v5 composer/writer 已退役且禁止 fallback。activation、runtime manifest、artifact、P0 或前驱 checkpoint 身份不闭合时必须 fail closed，并保留 raw/source/canonical 事件供身份修复后重放；禁止旧 joint composer、v5 post-transition、v3.1/v3/v2、加性模型或热替换。
5. 常规 AMSC `METAR` 的整数报文温度只可单调抬升 `X` 的精确已报下界；其测量区间同时约束内部连续状态 `Z`。`SPECI` 与常规 METAR 同等级进入强似然软更新，但永远不能改变 `X` 的硬下界。只有已关闭且可回放的 resolution bridge 才能据此排除市场桶 `B`；弱预测源不得覆盖硬下界或最终结算锁。
6. 先验、弱信息与强观测先在连续的“计划常规 METAR 槽位潜在日高”状态 `Z` 上更新；唯一 posterior 随后以确定的整度报文量化规则投影为离散 `p(X)`，再由版本化 `X→S→B` bridge 投影为 `p(B)`。不得把连续 `p(Z)` 标成 `p(X)`，也不得让 bridge、SSOT、UI 或策略消费含 0.25/0.5℃ 格点的伪 `X`。
7. 温度轨迹、动量、云雨、露点和风压属于观测模型内部特征，不得在策略层再次形成第二天气判断链。
   - 目标序贯强观测中，当前报文 `o_t` 只与 `O_<t` 派生的 pre-event 轨迹共同评分；`O_<=t` 轨迹只能在本次更新后写入 checkpoint，供后续事件使用。静态温度/状态表若未实际消费该受约束输入，不得命名为强似然。
   - `termination_risk` 只能是同一强似然剩余升温/降温状态混合的边际读数（`probability_no_material_new_daily_high`），不得作为第二 likelihood、hard mask 或策略层重复扣分。
8. 唯一概率事实是 `posterior_ssot_fact`；UI、决策与执行不得重算概率。
9. 唯一事实存储是 `data/archive.db` 的 `event_fact_history/event_fact_latest`；前端只读。
10. 可执行入场仅允许 posterior settlement、SSOT target realization、hard-source direct SSOT target。
11. 当前 Target Realization v6 的真实 SELL 只允许 v6 精确闭集中的 `target_realization` 或四个 0.999 `market_alpha_take_profit` code；主 YES staged/invalidation SELL 必须先释放并 reconcile standing coverage。v5 的 0.90 code 已原子退役且禁止 fallback。Edge/LCB/exec_edge/signal_edge、repricing、stop-loss、trail-stop 永久不可执行。
12. 动态角色与持仓批次必须分离：角色随每个 posterior 重选，lot 以 `(entry_strategy_leg,bucket,side,artifact,intent)` 保留成交来源。`edge_trades` 不得充当新交易环持仓 SSOT；partial/full fill、lot version、standing coverage、`main_yes_position_cycle_id`、stage watermark 与 intent 必须同事务更新。水位只在同一持仓周期内单调；exact hard lock，或连续两条 canonical 强观测低风险确认且无 invalidation，完成旧 SELL/coverage reconcile 后才可新建周期再入场。主 YES 降为次 YES 时已有仓位可在窗口外保留至 4.99 USDC，但窗口外不得新增或补仓。任何冲突 SELL/撤单必须 reconcile 后才能按 latest posterior 重建 BUY，不能等待下一报文或把 continuation 伪装成天气证据。
13. CLI 默认强制 dry-run；Live 必须显式 `--live` 加人工确认短语。
14. 任何 CLOB POST 前必须持久化 `order_intent_id`、请求和确定性 `expected_order_id`。
15. 未知提交结果只能按确定性订单 ID reconcile；禁止猜测匹配或直接重试。
16. 决策落库失败必须 fail closed；任何重试只能通过 durable idempotency 收敛。
17. Live 执行只读数据库 SSOT，不得读取 UI HTTP 或 snapshot 文件作为执行计划。
18. 当前事件的 freshness/causal-as-of 缺失等于该项**实时证据**不可用，不得无惩罚放行；这与离线 strict-as-of 训练样本完整性不同，后者不得阻断已激活交易链。
    - forecast strict-as-of Tier A 的唯一到达事实是 immutable raw envelope 中由 transport 在读取 body 前写入的 `response_headers_observed=true`，并同时绑定 request、header-start、complete、raw hash、source 与 target。`response_completed_at`、请求前本地时钟、provider 自报可用时间、derived parser、文件 mtime 或调用方字段均不得提升 Tier A；缺失时只能按 Tier B/C 使用，永不进入 strict 训练或动态权重 artifact。
19. production LaunchDaemon/LaunchAgent 不得注入 `PM_EDGE_REALIZABLE_V2_ENABLED`、`PM_EDGE_REALIZABLE_V2_GATE_ENTRY` 或任何旧 EV/Edge/LCB/stop-loss/repricing 下单开关。旧字段仅可留在离线研究、诊断或展示；真实 entry/exit 只能遵守当前 Bayes target 与 L1 family/code 闭集。
20. 事实写入只允许 canonical writer；payload 冲突只 merge-fill，不得弱源覆盖强源。
21. 明确退役的代码直接删除，不保留兼容路径；安全阻断不是 fallback。
22. 默认交付必须完成实现、测试和审计闭环，不以 review-only 作为终点。
23. `operations_date_et` 只用于交易、统计和值班；物理与结算目标日使用 resolution contract 指定的 `resolution_local_date`，两者不得互相替代。
24. 固定 source authority 不等于自动授权 bridge。生产 soft market projection 还必须绑定独立的 operational projection authority、固定零样本 artifact 与冻结 bucket contract；该基线允许未校准 `p(B)` 和经独立策略批准的 YES/trajectory-NO 候选，samples/strict-as-of/OOS 不得成为 live gate。真实 X/S 样本只进入 calibrated successor，验证后原子整体替换 baseline。任何概率 PMF 的有限支持都不是 authority hard envelope：trajectory NO 只能按独立桶 `q_NO=1-p_yes` 的 70/80/90/95/98% 分档获得 10/25/50/75/100 USDC；soft unreachable 与 `q_NO<70%` 不一致时 fail closed。hard-exclusion NO 必须逐组合证明完整 `H_X×H_D` 均落在目标桶外并绑定 exact checkpoint/bridge/target；200 USDC 只是绝对上限，允许统一资本比例缩量，不要求足额、不得因不足 200 阻断该腿；软不可达、低概率或核外截断不能升级为硬证书。
25. strict-as-of 训练数据、OOS、回放评分与 artifact promotion 只属于离线研究/版本准入面，**不得**成为交易 live 的实时阻断条件。实时执行只检查已提交 posterior SSOT、当前事实与市场/风险/签名/幂等/时限安全；离线证据不足只能禁止新版本 promotion，不能让已激活的交易链因该类条件停摆。
26. D0 第一条报文前只允许一个 P0：`Normalize[p_prior(X|A) * L_F(F*|X,A) * p_prior(Z_0+|X,A,F*)]`。P0 只能消费连续先验与所有预测源形成的联合弱似然，绝不消费当前报文、轨迹、动量、云雨、市场、策略、订单或当前 joint composer 输出；其持久化必须绑定 immutable prior/weak/strong-artifact lineage 与冻结 resolution contract。未关闭的 `X→S→B` bridge 只能使 P0-B 标为不可用（固定 NA 原因），绝不能阻断 P0-X 的持久化、后续强观测更新或每日物理校正。P0 仅写 append-only initial checkpoint/lineage，不得冒充一次 strong update、不得产生 realization/outbox/decision/order；同 target 的不同 P0 fork 必须 fail closed。
27. 当前唯一生产概率主线固定为 ADR-0003 的 v6 `pre-event Predict → current-event joint emission → continuous-Y/reported-X hard condition → normalize → clocks/history`。Predict 只能读取 `O_<t`、上一 checkpoint、冻结候选条件化未来强迫、持续时钟和 `Δt`；当前报文不得提前进入 Predict，NWP 小时曲线不得再次形成 likelihood，Update 后不得再执行 event-conditioned state transition。常规 METAR 报 `k℃` 时公共 `X` 下界为 `k`、连续 `Y` 编码下界为 `k-0.5℃`，必须分列保存；SPECI 只做联合 emission。系统不建立第二个 live latest；v5 已退役且禁止 fallback。
28. 动态三腿当前唯一权威是 ADR-0006/Target Realization v6 generation 2：固定主 YES 四态、`min(risk_target_ratio,time_cap_ratio)`、position-cycle 再入场、trajectory NO≤100、hard NO≤200 可统一资本比例缩量与默认 0.999 standing exit。ADR-0005/v5 只作历史审计；混代、热改、重复 cutover 改写 pause 或任何 v5 fallback 一律 fail closed。
29. 篮子动态风险必须拆分为 `underreach_risk` 与 `upside_breakthrough_risk`。前者表示 P1 主桶未达成：以 `M=argmax(P1)` 为锚，结合官方已报最高温 `R`、ET 剩余时间、校准后的剩余升温包络、升温速度、模型一致性和证据新鲜度判断；下方桶只能由 bucket contract 的 `predecessor(M)`、`predecessor²(M)` 解析。后者只表示官方实况形成新高后的下一桶突破，使用 `successor(R)`、`successor²(R)`；`P1=30℃、R=25℃` 到 29℃、28℃以及“不是26℃”均为说明性示例，不能硬编码任何绝对温度、桶宽或边界；没有新高事件不得伪造上行突破。
30. 风险 overlay 绝不得修改 P0/P1、regime、hard-NO 证明、事实下界或 resolution bridge。overlay 只能改变候选桶、排序、预算和组合覆盖价值；官方硬事实先过滤数学排除桶，软不可达、低概率、模型分歧或赔率昂贵均不能升级为 hard NO。
31. 赔率曲线必须以官方 CLOB WebSocket 逐档 YES 深度计算真实 `all_in_yes_ask`。篮子级 argmax 的目标函数由第 35 条定义；本条的线性 `BasketScore=P1ExpectedValue+λ_shortfall×ShortfallCoverage+λ_breakthrough×BreakthroughCoverage−λ_depth×SlippageRisk−λ_concentration×ConcentrationRisk−λ_revision×RevisionRisk−TotalCost`（`value_v2`）自 2026-08-13 起**只作为审计读数**保留在解身份中，不再是 argmax 判据；文档、脚本或调用方不得据它声明"当前目标函数"。coverage 是情景覆盖价值而非新概率；任何桶只有在边际组合价值为正、证据/深度/预算闭合且立即执行/已成交部分满足 `sum(all_in_yes_ask)<1` 时才可进入阶段篮子；未成交 staged GTC 受独立挂单规则约束，不计入已实现地板。
32. 动态候选集最多向上下任一方向扩展两层：`BaseSet(P1) ∪ {predecessor(M),predecessor²(M)}(underreach) ∪ {successor(R),successor²(R)}(breakthrough)`，所有桶关系由 bucket contract 解析，禁止硬编码绝对温度或固定桶宽；每次新事实、风险阈值、模型组合或 WS 快照变更后重算。两类风险同时升高时必须降低单桶集中度并收紧门槛；不能以无限扩桶、赔率曲线加权或动态排序绕过 lineage、hard fact、证据新鲜度、预算、逐腿 FOK、成交后重算或 `reconcile_required`。
33. 双风险层只属于目标篮子编译/决策面，不能成为 hard-NO lane 的输入、不能调用订单 gateway，也不能改变 `production_no_live` 或 `live_submit_authorized=false`。旧任务 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已退役归档，不再是运行、切换或 readiness 依赖；不得重新启动其独立跟单/结算地板执行线。system 域三个共用服务只承载 target `production_no_live` 主线。
34. 极端赔率重塑结算曲线只能作为篮子组合优化：对新增 `x` 份、全包价格 `a_b` 的桶 `b`，最终结果命中该桶时增量利润为 `x(1-a_b)`，未命中时为 `-x·a_b`；只有尾部覆盖带来的 `ΔBasketScore>0` 且 P1/lineage、官方 WS 逐档深度、集中度、预算及 production/no-live 立即执行/已成交部分的 `sum(all_in_yes_ask)<1` 门全部闭合时才可纳入。不得按赔率倒数、极端低价或高 payout ratio 单独买入；`reservation_value` 不是概率，不得修改 P0/P1、regime、hard-NO 或事实下界。
35. 篮子 argmax 的唯一目标函数是分数对数财富曲线分 `CurveScore=Σ_b p_b·log(1+f·s_b/W)`：`f=0.25`、`W=basket_policy.total_budget_usdc`（稳定风险资本，不得用剩余预算——预算见底时 `W→0` 会让罚项发散并使零预算站 fail closed）、`s_b` 是该桶结算时的整篮情景损益（未持仓桶为 `−篮子成本`，故情景空间是完整划分）。破产解 `1+f·s/W≤0` 得 `−∞`。形态 `objective_form` 只能取 `fractional_log_wealth_curve_v1`（live）或 `value_v2`（审计读数），且必须写入解身份与 plan。凹性即注码约束：它管"押多大"，不管"什么形状"。**argmax 的决策空间是（支持集, 数量）联合**，不得先按概率质量定死支持集再优化数量——该目标在最优注码下的取值即我们信念与市场价的 KL 散度（实测相关 0.833），本就是边际度量，把盘口喂给它即可，禁止另加边际项、赔率项或胜率项。两段式实测代价：37 站日支持集平均概率 0.688 对市价 0.883，EV −0.195。候选窗口的定义域是**当日证据仍可达的全部桶**（`probability > EPS`，即未被硬下界清零者）中长度 ≤ `maximum_support_buckets` 的连续子窗，加上在任窗口（恒可选、恒合法）。**定义域不得收窄为概率核**——概率核按质量选取，质量正是市场定价最贵之处，以核为域会让最优化沦为形式：实测 40 份 live plan 中 34 份只有一个可行窗口即其自身在任窗口，ZUUU 2026-08-14 就座的四桶市价 0.930 对我方 0.585（edge −0.345），而核外的 33°C（+0.092）、32°C（+0.052）、38°C（+0.048）从未被考虑；放开定义域后 53 站日中 24 日可找到过双轨窗口，平均 edge +0.318，ZUUU 当日为 `{33,34,35,36}` 的 +0.154。安全性由证据层守：被硬下界排除的桶概率为零，本就不是候选。两道轨 `MIN_SUPPORT_WIN_PROBABILITY`（0.45）与 `MIN_SUPPORT_EDGE_FRACTION`（0.10）用于筛选入围名单；选窗回执必须落 plan。
36. 正 EV 判据只在**整篮对不新增动作基线**（`NO_NEW_ACTION_BASELINE`，携带当前已成交与在途仓位，并非空仓）层面成立。单桶 EV 永不构成买入门槛：第 32 条的原子性要求完整篮子给每个支持桶正增量，负边际腿照买是特性；任何按单桶 EV、赔率倒数、极端低价或 payout ratio 过滤桶的实现都违宪。除"整篮曲线分高于空仓基线"外，不得增设入场价格门槛（含"支持集市场总价须低于某值"或"赔率须高于某倍数"）——实盘支持集常价高于 0.5，此类门会杀掉正常交易。
37. 篮子的合法性由三条**结构**硬约束定义，且只有这三条：支持集连续且至多 4 桶；完整篮子对每个支持桶给正增量；支持集内最差情景损益 `profit_floor ≥ SUPPORT_SCENARIO_FLOOR_FRACTION × 篮子成本`（现值 `−0.10`）。三条在打分之前作为候选合法性裁定（`_floor_legal`），违反者无论分数多高一律出局；不新增动作基线**恒合法且豁免地板**：已成交股份不可退，用地板判继承状态非法会把唯一永远正确的答案（持有、不加仓）也判非法并使编译硬失败——地板约束的是本次决策**新增**的部分，花钱的候选必须满足，全都不满足时赢家回落为基线即 HOLD。形态本身（平坦篮、尾桶倾斜、单桶集中）一律不受分类禁止。
38. 地板的语义是**封下尾、不封上凸**。上方尖峰不设任何上限；便宜尾桶只要改善收益/赔率曲线即可买入，买多少由曲线分定价，不另立规则。地板只禁止"为了某个桶把其余支持桶裸奔到底"。地板的作用域是**证据认定的存活桶集**，绝不是优化器选中的窗口——优化器既选窗口又定作用域等于自己写自己的约束，缩到单桶即架空地板，而被放弃的存活桶结算时照样吃掉全部权利金。地板随存活集收缩自动放松：4 桶时强制护住全部，**硬下界**杀到 1 桶时自动失效——全仓该桶完全合法，因为那不再是赌，是收割确定性。选桶动作本身即带彩票性质（押一个 ≤4 桶窗口对抗整个分布），这是策略底色，不是待消除的缺陷。
39. 动态调仓不是独立机制，就是第 35 条目标函数随 `p(t)` 更新反复求解。`p(t)` 的活性来源是硬下界逐时清零、择优模型组合、TAF 修订与 METAR 物理预测器；`p` 变尖则最优解自动集中，桶被硬排除则仓位自动停喂。禁止为"建仓/调仓/收割"设立并行的第二目标函数、第二判据或人工阶段开关。
40. 收益曲线由三个组件合成且各司其职：篮子底仓挣胜率、尾桶凸性挣赔率、确定性集中挣收割。策略指标是可证伪的每日读数而非意图：解身份与 plan 必须携带 `target_win_probability`/`basket_win_probability`、`target_odds_ratio`/`basket_odds_ratio`、`target_support_floor_pnl`、`floor_rejected_candidate_count`。当前设计目标为胜率 `≈0.55`；`55%` 胜率下盈亏平衡赔率为 `0.45/0.55≈0.82`。偏离目标时只允许调两个常量：地板分数 `SUPPORT_SCENARIO_FLOOR_FRACTION` 与 Kelly 分数 `f`；不得改为按形态分类准入。
41. 尾部风险由四道彼此独立的墙承担，缺一不可：单笔由第 37 条地板界定下尾（最差 `−110%` 成本＝支持集内 `−10%`、集外 `−100%`）；规模由分数 Kelly `f=0.25` 的凹性约束；相关性由 `config/trading/station_regional_blocks_v1.json` 的气候块**块内先求和再平方**承担（同气团城市按一个仓位计罚，不估相关矩阵）；组合层由 Tier-1 三维上限与滚动回撤断路器承担，断路器 action 只能是 `STOP_NEW_INTENTS_ONLY`——平仓会把它正在反应的亏损按当时盘口实现掉，与硬界"只约束新增"同哲学。
42. 选桶三层边界（2026-08-13 实测定界，防漂移）：**模型包络与概率核是纯证据层，永不读价格**——包络 = 校准后模型中心极值 ∪ TAF TX（union 只扩不缩），概率核只按概率质量到覆盖目标选取；同概率不同盘口必须产出相同概率核（已由宪章测试钉死）。外围扩展层的价格门（`edge_max_ask`、`price_coverage_value`）**仅限第 29/31/34 条的风险覆盖成本语义**——判断"这份尾部保险贵不贵"，不判断"哪个窗口值得买"，不得扩权为席位判据。**席位唯一权威是第 35 条的联合 argmax**：候选窗口在"证据仍可达"的全域内枚举、按曲线分发座——概率核决定的是"信念堆在哪"，不是"能买什么"，二者在实现上必须解耦（席位定义域收窄回概率核即违宪，见第 35 条实测）。任何"按概率最大选支持集""按包络中心定桶"或其他绕过第 35 条的选择器均违宪——按概率最大选窗在 37 站日实测 EV −0.195，因为我们评分最高的桶正是市场定价最贵的桶。
43. 方向偏置的唯一合法归宿是 `p(t)` 内部（重申第 7 条）。**禁止策略层任何天气条件分支或桶平移**（"有雨往下偏、晴空往上偏"式规则）：实测 173 站日订正后残余偏差雨日 +0.63/偏高 52% 对无雨 +0.55/偏高 49%，统计无差别——降水证据已被模型与订正完全消化，策略层再偏置即双重计数，且以二值粗规则覆盖已校准的连续概率（策略文件不含天气特征标识符已由宪章测试钉死）。已获准的后继改进是**环流条件化的分布宽度与偏度**（锋前日加宽下尾、高压脊日收窄），动机是实测条件厚尾（`|z|>3` 达正态 13 倍，无条件校准但逐日盲）；它属于概率层，必须走 OOS 三门＋容差通道准入，成功与否以 `|z|>3` 频率回归 ~0.3% 与逐日 CRPS 为判据，不得以任何形式落在选桶或执行层。
44. 结算真值的修复与排除（2026-08-13 立，防历史被静默改写）：**任何 truth join 必须取每 `(station_id, local_date)` 的最新 `revision_no`**——`calibration_station_daily_high_truth` 是 append-only 多修订表（现有 134 个站日含多修订），不带修订过滤的读取会取到已被取代的值，并使据此得出的一切校准结论失效（2026-08-13 即因此误判"条件厚尾 13 倍"，正确口径下为 4.2× 且样本内无统计意义）。发现错误真值时：**有 canonical 事实的走 `materialize_settled_station_truth` 追加修订**（append，永不改写历史行；已修 10 条，最新值与当日完整观测最大值逐条一致）；**无 canonical 事实的孤儿投影禁止补写**——`canonical_awc_truth_projection_scope_content_conflict` 是正确阻断，为一个无源头的投影追加修订等于伪造血缘。孤儿只能登记进 `config/trading/retired_ingest_truth_exclusions_v1.json` 由消费端跳过（原行保持逐字节不变、仍可作为历史读取），且必须在 OOS 评分端与训练 fold 端同时生效。判定错误真值的口径固定为：当日完整观测记录的最大值高出记录真值 ≥3°C。已排除的 6 条全部来自退役的 `CANONICAL_AMSC_AWC_INGRESS`，其共同特征是在当地日高温出现之前发布（凌晨至中午）、血缘仅 2–12 条观测而档案有 25–48 条。
