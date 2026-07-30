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
verification_gates:
  - python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  - python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation config/production_probability_runtime_activation.json --event-date $(TZ=Asia/Shanghai date +%F)
  - python3 scripts/target_realization_v6_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v6-generation-2-current
---

# 系统宪章

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
31. 赔率曲线必须以官方 CLOB WebSocket 逐档 YES 深度计算真实 `all_in_yes_ask`，以篮子级增量目标优化：`BasketScore=P1ExpectedValue+λ_shortfall×ShortfallCoverage+λ_breakthrough×BreakthroughCoverage−λ_depth×SlippageRisk−λ_concentration×ConcentrationRisk−λ_revision×RevisionRisk−TotalCost`。coverage 是情景覆盖价值而非新概率；任何桶只有在边际组合价值为正、证据/深度/预算闭合且立即执行/已成交部分满足 `sum(all_in_yes_ask)<1` 时才可进入阶段篮子；未成交 staged GTC 受独立挂单规则约束，不计入已实现地板。
32. 动态候选集最多向上下任一方向扩展两层：`BaseSet(P1) ∪ {predecessor(M),predecessor²(M)}(underreach) ∪ {successor(R),successor²(R)}(breakthrough)`，所有桶关系由 bucket contract 解析，禁止硬编码绝对温度或固定桶宽；每次新事实、风险阈值、模型组合或 WS 快照变更后重算。两类风险同时升高时必须降低单桶集中度并收紧门槛；不能以无限扩桶、赔率曲线加权或动态排序绕过 lineage、hard fact、证据新鲜度、预算、逐腿 FOK、成交后重算或 `reconcile_required`。
33. 双风险层只属于目标篮子编译/决策面，不能成为 hard-NO lane 的输入、不能调用订单 gateway，也不能改变 `production_no_live` 或 `live_submit_authorized=false`。旧任务 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已退役归档，不再是运行、切换或 readiness 依赖；不得重新启动其独立跟单/结算地板执行线。system 域三个共用服务只承载 target `production_no_live` 主线。
34. 极端赔率重塑结算曲线只能作为篮子组合优化：对新增 `x` 份、全包价格 `a_b` 的桶 `b`，最终结果命中该桶时增量利润为 `x(1-a_b)`，未命中时为 `-x·a_b`；只有尾部覆盖带来的 `ΔBasketScore>0` 且 P1/lineage、官方 WS 逐档深度、集中度、预算及 production/no-live 立即执行/已成交部分的 `sum(all_in_yes_ask)<1` 门全部闭合时才可纳入。不得按赔率倒数、极端低价或高 payout ratio 单独买入；`reservation_value` 不是概率，不得修改 P0/P1、regime、hard-NO 或事实下界。
