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
verification_gates:
  - python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  - python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation config/production_probability_runtime_activation.json --event-date $(TZ=America/New_York date +%F)
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-v6-production-cutover
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
11. 可执行退出只允许两个互斥原因族：天气/结算目标变化使用 `target_realization_*`；以 `top_yes_wave` 成交且该桶在 latest posterior 仍是当前最高概率 YES 时，才可按固定 0.90 limit 使用 `market_alpha_take_profit_*`。后者不是天气目标失效，不得通过改名前缀混淆归因；观察到的更高 bid 只证明 0.90 可执行，不得动态改写限价。当前 top 身份随 posterior 重选，prepared intent 必须绑定 exact bucket/run，角色变化后旧 market-alpha intent 失效并由 target-realization 优先。其 runtime 即使已实现也必须默认关闭，只有 additive schema、不可变部署 manifest 与显式 activation 同时闭合才可 live。Edge/LCB/exec_edge/signal_edge、动态 repricing、stop-loss、trail-stop 仍仅可诊断。
12. 动态角色与持仓批次必须分离：角色随每个 posterior 重选，lot 以 `(entry_strategy_leg,bucket,side,artifact,intent)` 保留成交来源。`edge_trades` 不得充当新交易环持仓 SSOT；partial/full fill、lot version 与 intent 必须同事务更新。SELL_OLD 完成后必须以独立 continuation 立即重建同一 latest posterior 下的 BUY_NEW；不得等待下一报文，也不得把 continuation 伪装成新天气证据。
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
24. 固定来源不等于关闭 bridge：在 strict raw-object-backed 的 final `X/S` 标签、`p(S|X,C)` 校准、冻结 bucket contract 和离线 OOS 证明完成前，任何实现不得把该登记提升为可执行 market projection 或产生该 bridge 驱动的订单；v6 reported-X SSOT 是唯一生产概率链。这是 bridge/新 artifact 的 promotion 边界，不是对已批准物理 X 基线、AMSC 入库或日常训练通道的运行时阻断。
25. strict-as-of 训练数据、OOS、回放评分与 artifact promotion 只属于离线研究/版本准入面，**不得**成为交易 live 的实时阻断条件。实时执行只检查已提交 posterior SSOT、当前事实与市场/风险/签名/幂等/时限安全；离线证据不足只能禁止新版本 promotion，不能让已激活的交易链因该类条件停摆。
26. D0 第一条报文前只允许一个 P0：`Normalize[p_prior(X|A) * L_F(F*|X,A) * p_prior(Z_0+|X,A,F*)]`。P0 只能消费连续先验与所有预测源形成的联合弱似然，绝不消费当前报文、轨迹、动量、云雨、市场、策略、订单或当前 joint composer 输出；其持久化必须绑定 immutable prior/weak/strong-artifact lineage 与冻结 resolution contract。未关闭的 `X→S→B` bridge 只能使 P0-B 标为不可用（固定 NA 原因），绝不能阻断 P0-X 的持久化、后续强观测更新或每日物理校正。P0 仅写 append-only initial checkpoint/lineage，不得冒充一次 strong update、不得产生 realization/outbox/decision/order；同 target 的不同 P0 fork 必须 fail closed。
27. 当前唯一生产概率主线固定为 ADR-0003 的 v6 `pre-event Predict → current-event joint emission → continuous-Y/reported-X hard condition → normalize → clocks/history`。Predict 只能读取 `O_<t`、上一 checkpoint、冻结候选条件化未来强迫、持续时钟和 `Δt`；当前报文不得提前进入 Predict，NWP 小时曲线不得再次形成 likelihood，Update 后不得再执行 event-conditioned state transition。常规 METAR 报 `k℃` 时公共 `X` 下界为 `k`、连续 `Y` 编码下界为 `k-0.5℃`，必须分列保存；SPECI 只做联合 emission。系统不建立第二个 live latest；v5 已退役且禁止 fallback。
