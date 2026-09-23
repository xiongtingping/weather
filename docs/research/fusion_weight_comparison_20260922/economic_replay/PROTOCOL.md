# 冻结三候选的执行条件经济回放：计算前协议（2026-09-23）

目的：补上 `pareto_metrics` 报告里「可兑现 EV / Log-growth 未识别」一格，但只在证据真实存在的范围内补，不冒充完整生产回放。

## 固定输入（全部在本轮之前已冻结）

- 概率：`joint_objective/scores.json` 的逐站日权重，四臂 current / blend25 / blend50 / blend75（另列 center_stack21、crps_hier21 两端点作参照）；分布为后处理前的单系统线性池，与 `pareto_metrics` 完全相同。
- 盘口：`pareto_metrics/execution_window_quotes.json` 的 152 个站日——发布后 300 秒内第一份完整官方 CLOB WS 快照，规则在上一轮看经济结果之前已固定。四臂读同一份盘口、同一个执行时刻，不挑价。
- 费用：生产策略自己的费用合同 `bayes_basket_target/runtime.py` 的 `fee_rate = 0.0`（成本 = 价格 ×(1+fee_rate)，strict_delta / maker_reservation / delta_v2_runtime 同一写法）。另跑 fee_rate=0.02 作敏感性，不据此选臂。
- 真值：样本自带的 canonical 真值及其桶（与概率评分同一 hit）。

## 两种策略（都在算之前定死）

**S1 门槛吃单（生产不等式的单桶版）。** 宪章 130：`Σ q·n ≥ (1+h)·C`，h=0.05（`delta_v2.ECONOMIC_HURDLE_V1`）。逐桶沿真实卖单档位往上吃，某档价格 π 满足 q_b ≥ (1+h)·π·(1+fee) 才买这一档；每站日预算 20 USDC，按 q/π 从高到低分配；单桶股数低于盘口 `min_order_size` 则整桶不下。这是生产不等式在单桶上的应用，**不是**生产的篮子/席位规则。输出：成本、实现盈亏（Σ n_y − C）、自报 EV（Σ q·n − C）、ROI。

**S2 Kelly 对数增长。** 用卖一价 a_b 做完整桶梯的带现金 Kelly（Smoczynski–Tomkins）：按 q_b/a_b 降序入选，保留率 R=(1−Σ_S q)/(1−Σ_S a)，真值落在已买桶时财富乘子 q_y/a_y，否则 R。全 Kelly 与 1/4 Kelly（乘子 0.75+0.25·m）两档。组合口径：每个事件日的资金均分给当天所有站日，逐事件日复利，log(W_T/W_0)=Σ_d log(当日平均乘子)。另报一个恒等式参照：按 q 比例全仓买整条梯子，乘子=q_y/a_y，因此两臂的对数增长差**恰好等于** LogScore 差，与价格无关。

## 市场参照

同 152 站日上市场自身的 LogScore：卖一价归一、(买一+卖一)/2 归一两种。它回答「模型比市场差多少 nat」，是 EV 符号的决定项。

## 统计与资格

- 与 current 配对差值；连续 3 日块 bootstrap（4000 次，种子 230923），保留同日跨站相关。
- 资格标为 `execution_conditioned_replay_pool_stage`：线性池入口 + 真实盘口 + 生产费用合同；**不是**完整服务 P0/P0-M/P1，**不是**实际成交；吃单按快照深度计，未模拟队列、部分成交和他人抢单。152/515 的覆盖与选择偏差照报。
- 不按结果改预算、门槛、Kelly 分数或样本；任何臂任何指标都不作晋级依据。

只读冻结文件，不连生产库，不下单，不改生产。
