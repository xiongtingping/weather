---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
supersedes: [legacy-weather-probability-terminology]
implementation_symbols:
  - temperature_prior_runtime.py
  - forecast_weak_likelihood_runtime.py
  - metar_strong_likelihood_runtime.py
  - sequential_bayes_filter_shadow_runtime.py
  - production_reported_x_public_ssot_writer.py
verification_gates:
  - python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
last_verified_commit: working-tree-2026-07-16-production-dynamic-three-leg-v5
---

# 术语表

| 术语 | 唯一定义 | 不允许的含义 |
|---|---|---|
| 物理最高温 `X` | ZUUU 在 `resolution_local_date` 内 AMSC scheduled regular METAR 所报整数温度的最大值 | 连续潜在站点最高温、SPECI 最大值、市场桶或未经声明的结算真值 |
| 结算温度 `S` | resolution source 按版本化来源、日界、单位、舍入和修订规则解析出的标量 | 任一实时 METAR/current 温度 |
| 市场桶 `B` | market bucket contract 对 `S` 映射出的离散结算 outcome | 物理连续温度或 UI 自行推导桶 |
| `operations_date_et` | 交易、统计、值班和资金报表的 ET 日口径 | 成都物理/结算当地日 |
| `resolution_local_date` | resolution contract 指定的物理与结算目标当地日 | 固定 ET 日或 runtime 猜测日期 |
| 盆地/气候先验 | 仅由历史气候、日期、站点、地形和天气型构造的连续温度先验 | 当日预测、实况或市场价格 |
| 弱信息 | 模型/TAF 等预测误差条件信息 | 硬下界或最终锁 |
| 强观测 | 有有效时、来源血缘和 freshness 的规范站点观测 | 无时间戳 observation inputs |
| reported-X 后验 | production v6 pre-event state-space Bayes 对报文定义 `X` 的整数桶概率；market bridge 未关闭时仍可独立发布 | UI/策略层复算、连续潜变量或市场桶概率 |
| market posterior | approved `p(S|X,C)` 与冻结 bucket contract 从 reported-X 后验导出的 `p(B)` | 当前报文温度直接映射桶或价格隐含概率回写天气 |
| 温度轨迹 | 同一规范观测序列随有效时变化的温度路径 | 独立策略信号 |
| 温度动量 | 轨迹在明确时间窗内的变化率特征 | 可绕过 posterior 的买卖触发器 |
| `Z_t^-` | 当前 canonical event 被消费前的预测联合状态 | 使用当前事件更新后的自确认状态 |
| `Z_t^+` | 当前事件消费后的联合状态，只供后续事件使用 | 回灌当前事件评分 |
| `raw_log_score` | observation model 对当前事件在 `X,Z_t^-` 上的未中心化分数 | 已归一化 posterior 或可直接交易信号 |
| `posterior_delta` | composer 针对指定 previous posterior 计算的 centered 增量 | 可脱离 previous posterior 重用的 source likelihood |
| `posterior_ssot_fact` | 可被决策层消费的唯一概率事实 | 任意 capital plan 或 path probability |
| target realization | posterior SSOT 目标桶向结算实现过程的执行计划 | 第二套天气概率模型 |
| `target_realization_*` | posterior、hard certificate 或 settlement 改变天气/结算目标的退出原因族 | 波段止盈、资本回笼或旧 Edge 风险再平衡 |
| `market_alpha_take_profit_top_yes_wave_completed` | 当前 top YES 按批准 artifact 在 0.90 完成波段的唯一独立退出 code | 天气目标失效、冻结历史桶、动态 repricing、stop-loss 或 trail-stop |
| `strongly_excluded_no` | exact posterior/bridge/bucket 联合物理硬排除证书授权的唯一 NO 槽位，最高 100 USDC | 低 posterior、软不可达、trajectory 分档或当前报文温度直接映射 |
| `logical_exit_intent_id` | 当前 posterior、持仓 lot/version、原因 family/code 与请求卖量共同确定的唯一退出意图身份 | 可跨 posterior 或持仓版本复用的桶级锁 |
| `DecisionIntent` | 下单前持久化的决策/请求意图 | 下单后的日志补写 |
| `order_intent_id` | 决策、CLOB、订单账本和对账共享的稳定身份 | 进程内随机请求 ID |
| `expected_order_id` | 已签名订单的 EIP-712 确定性 hash | 按价格数量猜测出的候选订单 |
| fail closed | 证据、模型、持久化或身份不完整时停止产出/停止下单 | 回退到旧模型 |
| composer v6 | `physical_state_bayes_v6_pre_event_predict_update` | v5 post-transition、v3.1/joint/bucket-product fallback 或第二 composer |
| 策略 v5（退役） | `target_realization_v5`：历史 top YES 20/0.90、second YES 4.99、joint hard-excluded NO 100；只作旧订单审计 | 当前 production 或 fallback |
| 策略 v6（当前） | `target_realization_v6` generation 2：main YES 四态/风险×时段、second YES<5、trajectory NO≤100 或 hard NO≤200、默认 0.999 | 概率模型 v6；策略与概率版本没有继承关系 |
| `main_yes_position_cycle_id` | 当前主 YES 一次可管理暴露周期；stage watermark 只在周期内单调 | 冻结桶排名、当日永久禁买或旧 wave cycle |
| `capital_scale_factor` | 当前 Transaction C 对同一计划全部合格新增 BUY 使用的统一缩量比例，hard NO 200 只是缩量前绝对上限 | EV/Edge、赔率或先到先得 |
