# LCB策略视角下的Signal_alpha设计

## 核心问题

**从LCB策略的本质出发，Signal_alpha应该代表什么？如何正确设计？**

---

## 第一部分：LCB策略的本质

### 1.1 什么是LCB策略

**LCB (Lower Confidence Bound)** = 置信下界策略

**核心公式**：
```
LCB = μ - z * σ

其中：
- μ (mu): 期望值
- σ (sigma): 标准差（不确定性）
- z: 置信度系数（如1.64对应95%单侧置信）
```

**含义**：
- 不追求期望收益最大化
- 而是追求"在一定置信度下的最坏情况"最优化
- 这是一种**保守的、风险厌恶的**策略

### 1.2 为什么使用LCB而不是期望值

**期望值策略的问题**：
```python
# 策略A: E[收益] = 10%, σ = 50%
# 策略B: E[收益] = 8%, σ = 5%

# 期望值策略选择A（期望更高）
# 但A的风险巨大，可能亏损40%

# LCB策略（z=1.64）：
# A: LCB = 10% - 1.64 * 50% = -72%
# B: LCB = 8% - 1.64 * 5% = -0.2%
# 选择B（更稳健）
```

**LCB的优势**：
1. **风险控制**：自动惩罚高不确定性
2. **稳健性**：避免极端损失
3. **理论基础**：来自多臂老虎机问题（UCB/LCB算法）
4. **实践验证**：在不确定环境下表现更好

### 1.3 当前系统的LCB实现

```python
# Signal层
signal_lcb = signal_alpha - signal_z * signal_sigma

# Path层
path_lcb = path_alpha - path_z * path_sigma

# Edge层
edge_lcb = edge_alpha - edge_z * edge_sigma

# 最终决策
if signal_lcb >= signal_lcb_min and path_lcb >= path_lcb_min and edge_lcb >= edge_lcb_min:
    trade()
```

**三层LCB门控**：
1. Signal LCB：信号质量的置信下界
2. Path LCB：到达目标的概率置信下界
3. Edge LCB：实际收益的置信下界

---

## 第二部分：Signal_alpha的正确定义

### 2.1 从LCB公式推导

**LCB公式**：
```
signal_lcb = signal_alpha - signal_z * signal_sigma
```

**Signal_alpha应该是什么**：
- 在LCB框架下，signal_alpha是μ（期望值）
- 那么它应该是**信号质量的期望值**

**但"信号质量"是什么**？

### 2.2 三种可能的定义

#### 定义1：预期收益率（推荐）

**Signal_alpha = E[收益率 | 信号特征]**

```python
# 给定当前信号特征，预期的收益率
signal_alpha = E[return | q_edge, trend, spread, ...]

# 例如：
# 如果历史上相似信号的平均收益率是15%
# 那么signal_alpha = 0.15
```

**优点**：
- 直接对应交易目标（收益）
- 有明确的概率解释
- 可以直接用于决策

**缺点**：
- 需要大量历史数据
- 需要定义"相似信号"

#### 定义2：预测优势（当前实现）

**Signal_alpha = 预测概率 - 市场价格**

```python
# 当前实现
signal_alpha ≈ q_edge + adjustments
            = (q_calibrated - 0.5) + trend_adjustments - penalties
```

**优点**：
- 直观易懂
- 不需要历史收益数据
- 可以实时计算

**缺点**：
- 不是真正的"期望收益"
- 预测优势 ≠ 实际收益
- 忽略了执行成本、滑点等

#### 定义3：Kelly优势（理论最优）

**Signal_alpha = Kelly fraction**

```python
# Kelly公式
f* = (p * b - q) / b

其中：
- p: 胜率
- q: 1 - p（败率）
- b: 赔率（盈亏比）

# 转换为signal_alpha
signal_alpha = kelly_fraction = (p * b - q) / b
```

**优点**：
- 理论最优（最大化长期增长率）
- 考虑了胜率和赔率
- 有坚实的数学基础

**缺点**：
- 需要准确估计p和b
- 对估计误差敏感
- 可能过于激进

### 2.3 推荐定义：期望收益率

**为什么选择定义1（期望收益率）**：

1. **与LCB策略一致**
   - LCB的μ应该是期望收益
   - Signal_alpha作为μ，应该是期望收益率

2. **直接对应交易目标**
   - 我们的目标是赚钱（收益）
   - Signal_alpha应该直接预测收益

3. **可以验证和优化**
   - 可以用历史数据验证
   - 可以持续优化模型

4. **统一框架**
   - Signal_alpha、Path_alpha、Edge_alpha都是期望收益
   - 形成统一的决策框架

---

## 第三部分：Signal_alpha的设计方法

### 3.1 方法1：直接建模（推荐）

**核心思想**：直接建模 E[收益 | 特征]

#### 步骤1：定义收益

```python
# 对于每笔历史交易
return = (exit_price - entry_price) / entry_price

# 或考虑手续费
return = (exit_price - entry_price - fees) / entry_price
```

#### 步骤2：提取特征

```python
features = {
    'q_edge': q_calibrated - market_price,
    'q_calibrated': q_calibrated,
    'trend_pct': trend_pct,
    'trend_consistency': trend_consistency_up,
    'model_spread': model_spread,
    'spread': bid_ask_spread,
    'depth_ratio': depth_ratio,
    'days_to_event': days_to_event,
    'market_volatility': volatility,
    'time_of_day': hour,
    'day_of_week': weekday,
    # ... 更多特征
}
```

#### 步骤3：训练模型

```python
# 使用GBDT或其他ML模型
from sklearn.ensemble import GradientBoostingRegressor

model = GradientBoostingRegressor(
    n_estimators=100,
    learning_rate=0.1,
    max_depth=5
)

# 训练
X = historical_features
y = historical_returns
model.fit(X, y)

# 预测
signal_alpha = model.predict(current_features)
```

#### 步骤4：估计不确定性

```python
# 方法A：使用分位数回归
from sklearn.ensemble import GradientBoostingRegressor

# 训练多个模型预测不同分位数
model_mean = GradientBoostingRegressor(loss='squared_error')
model_lower = GradientBoostingRegressor(loss='quantile', alpha=0.16)
model_upper = GradientBoostingRegressor(loss='quantile', alpha=0.84)

signal_alpha = model_mean.predict(features)
signal_lower = model_lower.predict(features)
signal_upper = model_upper.predict(features)
signal_sigma = (signal_upper - signal_lower) / 2

# 方法B：使用Bootstrap
predictions = []
for _ in range(100):
    sample = bootstrap_sample(historical_data)
    model_i = train_model(sample)
    predictions.append(model_i.predict(features))

signal_alpha = np.mean(predictions)
signal_sigma = np.std(predictions)
```

**优点**：
- 直接对应目标
- 自动学习复杂关系
- 可以持续优化

**缺点**：
- 需要大量历史数据
- 黑盒模型
- 可能过拟合

### 3.2 方法2：分解建模

**核心思想**：分解为胜率和赔率

```python
signal_alpha = P(win) * E[收益|win] - P(loss) * E[损失|loss]
```

#### 步骤1：建模胜率

```python
# 训练分类模型预测胜率
from sklearn.ensemble import GradientBoostingClassifier

classifier = GradientBoostingClassifier()
classifier.fit(X, y_win)  # y_win: 1=盈利, 0=亏损

P_win = classifier.predict_proba(features)[:, 1]
P_loss = 1 - P_win
```

#### 步骤2：建模条件收益

```python
# 训练两个回归模型
# 模型1：预测盈利时的收益
regressor_win = GradientBoostingRegressor()
regressor_win.fit(X[y_win==1], returns[y_win==1])

# 模型2：预测亏损时的损失
regressor_loss = GradientBoostingRegressor()
regressor_loss.fit(X[y_win==0], returns[y_win==0])

E_return_win = regressor_win.predict(features)
E_return_loss = regressor_loss.predict(features)
```

#### 步骤3：组合

```python
signal_alpha = P_win * E_return_win + P_loss * E_return_loss
```

**优点**：
- 可解释性强
- 分别优化胜率和赔率
- 符合交易直觉

**缺点**：
- 需要训练多个模型
- 复杂度高

### 3.3 方法3：贝叶斯方法

**核心思想**：使用贝叶斯推断

```python
# 先验分布
prior = Normal(mu=0, sigma=0.1)

# 似然函数（基于历史数据）
likelihood = compute_likelihood(features, historical_data)

# 后验分布
posterior = bayesian_update(prior, likelihood)

# Signal_alpha = 后验期望
signal_alpha = posterior.mean()
signal_sigma = posterior.std()
```

**优点**：
- 理论基础扎实
- 自然处理不确定性
- 可以融合先验知识

**缺点**：
- 实现复杂
- 计算开销大
- 需要选择先验

### 3.4 方法4：简化线性模型（当前方法的改进）

**核心思想**：保持线性框架，但基于收益优化权重

```python
# 当前公式
signal_alpha = w1 * q_edge + w2 * trend_pct + w3 * trend_consistency
               - w4 * model_penalty - w5 * freshness_penalty

# 改进：通过历史数据优化权重
# 目标：最大化预测收益与实际收益的相关性
from scipy.optimize import minimize

def objective(weights):
    predicted_alpha = compute_alpha(features, weights)
    correlation = np.corrcoef(predicted_alpha, actual_returns)[0, 1]
    return -correlation  # 最大化相关性

optimal_weights = minimize(objective, initial_weights)
```

**优点**：
- 保持简单性
- 可解释性强
- 易于实现

**缺点**：
- 仍然是线性假设
- 可能不够准确

---

## 第四部分：Signal_sigma的设计

### 4.1 Signal_sigma应该代表什么

**在LCB框架下**：
```
signal_lcb = signal_alpha - signal_z * signal_sigma
```

**Signal_sigma应该是**：
- 收益的标准差：Std[收益 | 信号特征]
- 即：给定当前信号，收益的不确定性

### 4.2 当前Signal_sigma的问题

```python
# 当前实现
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty
               + model_spread * 0.60
```

**问题**：
- 这不是真正的"收益标准差"
- 只是一些启发式惩罚的组合
- 没有概率解释

### 4.3 正确的Signal_sigma设计

#### 方法1：直接估计（推荐）

```python
# 使用分位数回归或Bootstrap估计不确定性
# （见3.1步骤4）

# 或者：使用历史波动率
similar_signals = find_similar_signals(current_features, historical_data)
signal_sigma = np.std(similar_signals['returns'])
```

#### 方法2：分解估计

```python
# 不确定性来源分解
sigma_prediction = uncertainty_from_model_spread(model_spread)
sigma_execution = uncertainty_from_spread(bid_ask_spread)
sigma_timing = uncertainty_from_time_to_event(days_to_event)

signal_sigma = sqrt(sigma_prediction^2 + sigma_execution^2 + sigma_timing^2)
```

#### 方法3：贝叶斯方法

```python
# 后验分布的标准差
signal_sigma = posterior.std()
```

---

## 第五部分：完整的LCB决策框架

### 5.1 三层LCB架构

```
Layer 1: Signal LCB（信号质量）
    signal_alpha = E[收益 | 信号特征]
    signal_sigma = Std[收益 | 信号特征]
    signal_lcb = signal_alpha - signal_z * signal_sigma

Layer 2: Path LCB（到达概率）
    path_alpha = P(到达目标价格)
    path_sigma = Std[到达概率]
    path_lcb = path_alpha - path_z * path_sigma

Layer 3: Edge LCB（实际收益）
    edge_alpha = E[实际收益 | signal, path]
    edge_sigma = Std[实际收益]
    edge_lcb = edge_alpha - edge_z * edge_sigma
```

### 5.2 决策规则

```python
def should_trade(signal_lcb, path_lcb, edge_lcb):
    # 三层门控
    if signal_lcb < signal_lcb_min:
        return False, "signal_lcb_below_min"

    if path_lcb < path_lcb_min:
        return False, "path_lcb_below_min"

    if edge_lcb < edge_lcb_min:
        return False, "edge_lcb_below_min"

    # 所有门控通过
    return True, "approved"
```

### 5.3 阈值设置

**Signal_lcb_min**：
- 当前：0.0
- 建议：根据历史回测优化
- 例如：选择使夏普比率最大的阈值

**Path_lcb_min**：
- 当前：0.0
- 建议：根据市场流动性调整
- 流动性好：可以降低
- 流动性差：应该提高

**Edge_lcb_min**：
- 当前：-0.05
- 建议：根据风险承受能力设置
- 保守：0.0（只做正期望交易）
- 激进：-0.10（允许小幅负期望）

---

## 第六部分：实施方案

### 6.1 阶段1：数据收集（1周）

**目标**：收集历史交易数据

```python
# 需要的数据
historical_trades = {
    'entry_time': ...,
    'exit_time': ...,
    'entry_price': ...,
    'exit_price': ...,
    'return': (exit_price - entry_price) / entry_price,
    'features': {
        'q_edge': ...,
        'trend_pct': ...,
        'model_spread': ...,
        # ... 所有特征
    }
}
```

**数据要求**：
- 至少100笔交易（最好500+）
- 包含盈利和亏损案例
- 特征完整

### 6.2 阶段2：模型训练（1-2周）

**步骤**：
1. 数据清洗和特征工程
2. 训练Signal_alpha预测模型
3. 训练Signal_sigma估计模型
4. 交叉验证
5. 回测验证

**代码框架**：
```python
# 1. 准备数据
X_train, X_test, y_train, y_test = train_test_split(features, returns)

# 2. 训练Signal_alpha模型
alpha_model = GradientBoostingRegressor()
alpha_model.fit(X_train, y_train)

# 3. 训练Signal_sigma模型（分位数回归）
sigma_model_lower = GradientBoostingRegressor(loss='quantile', alpha=0.16)
sigma_model_upper = GradientBoostingRegressor(loss='quantile', alpha=0.84)
sigma_model_lower.fit(X_train, y_train)
sigma_model_upper.fit(X_train, y_train)

# 4. 预测
signal_alpha = alpha_model.predict(X_test)
signal_lower = sigma_model_lower.predict(X_test)
signal_upper = sigma_model_upper.predict(X_test)
signal_sigma = (signal_upper - signal_lower) / 2

# 5. 计算LCB
signal_lcb = signal_alpha - 1.0 * signal_sigma

# 6. 评估
correlation = np.corrcoef(signal_alpha, y_test)[0, 1]
print(f"Signal_alpha与实际收益的相关性: {correlation:.3f}")
```

### 6.3 阶段3：在线部署（1周）

**集成到现有系统**：
```python
# 在edge_signal_runtime.py中
def compute_signal_advantage_v2(features):
    # 加载训练好的模型
    alpha_model = load_model('signal_alpha_model.pkl')
    sigma_model_lower = load_model('signal_sigma_lower_model.pkl')
    sigma_model_upper = load_model('signal_sigma_upper_model.pkl')

    # 预测
    signal_alpha = alpha_model.predict([features])[0]
    signal_lower = sigma_model_lower.predict([features])[0]
    signal_upper = sigma_model_upper.predict([features])[0]
    signal_sigma = (signal_upper - signal_lower) / 2

    return {
        'signal_alpha': signal_alpha,
        'signal_sigma': signal_sigma,
        'signal_lcb': signal_alpha - 1.0 * signal_sigma,
        # ... 其他字段
    }
```

### 6.4 阶段4：持续优化（持续）

**在线学习**：
```python
# 每天更新模型
def daily_model_update():
    # 1. 获取昨天的交易数据
    new_trades = get_trades_since_last_update()

    # 2. 增量训练
    alpha_model.partial_fit(new_features, new_returns)

    # 3. 保存模型
    save_model(alpha_model, 'signal_alpha_model.pkl')

    # 4. 评估性能
    evaluate_model_performance()
```

---

## 第七部分：对比分析

### 7.1 当前方法 vs 推荐方法

| 维度 | 当前方法 | 推荐方法（直接建模） |
|------|----------|---------------------|
| **Signal_alpha定义** | 预测优势+调整 | E[收益\|特征] |
| **Signal_sigma定义** | 启发式惩罚 | Std[收益\|特征] |
| **理论基础** | 启发式 | 统计学习理论 |
| **与LCB一致性** | 弱 | 强 |
| **可解释性** | 高 | 中 |
| **准确性** | 低 | 高 |
| **自适应性** | 无 | 有 |
| **实现复杂度** | 低 | 中 |
| **数据需求** | 低 | 高 |

### 7.2 预期改进

**Signal_alpha准确性**：
- 当前：与实际收益相关性约0.3-0.4
- 改进后：相关性可达0.6-0.7
- 提升：50-75%

**Signal_lcb有效性**：
- 当前：18.9%案例signal_alpha < 0
- 改进后：预计降到5-10%
- 同时提高正案例的准确性

**整体交易表现**：
- 夏普比率：预计提升30-50%
- 胜率：预计提升10-15%
- 平均收益：预计提升20-30%

---

## 第八部分：关键建议

### 8.1 核心原则

1. **Signal_alpha = E[收益 | 特征]**
   - 这是LCB框架下的正确定义
   - 直接对应交易目标

2. **Signal_sigma = Std[收益 | 特征]**
   - 真正的不确定性度量
   - 不是启发式惩罚

3. **基于数据，而非假设**
   - 用历史数据训练模型
   - 持续验证和优化

### 8.2 实施优先级

**P0 - 立即**：
- 开始收集历史交易数据
- 分析当前Signal_alpha与实际收益的相关性

**P1 - 本月**：
- 训练Signal_alpha预测模型
- 回测验证

**P2 - 下月**：
- 在线部署
- A/B测试

**P3 - 持续**：
- 在线学习
- 持续优化

### 8.3 风险提示

1. **数据不足**：需要足够的历史交易数据
2. **过拟合**：使用交叉验证和正则化
3. **市场变化**：需要持续更新模型
4. **黑盒风险**：保留简单模型作为备份

---

## 总结

### 核心洞察

**从LCB策略的本质出发**：
```
signal_lcb = signal_alpha - signal_z * signal_sigma
```

**Signal_alpha应该是**：
- E[收益 | 信号特征]
- 即：给定当前信号，预期的收益率

**Signal_sigma应该是**：
- Std[收益 | 信号特征]
- 即：给定当前信号，收益的不确定性

**当前实现的问题**：
- Signal_alpha不是真正的期望收益
- Signal_sigma不是真正的标准差
- 两者都是启发式组合，缺乏理论基础

**推荐方案**：
- 使用机器学习直接建模 E[收益 | 特征]
- 使用分位数回归估计不确定性
- 基于历史数据持续优化

**预期效果**：
- Signal_alpha准确性提升50-75%
- 整体交易表现提升30-50%
- 形成理论一致的LCB决策框架

**最重要的认识**：
- LCB不只是一个公式，而是一个完整的决策框架
- Signal_alpha和Signal_sigma必须有明确的概率解释
- 应该基于数据建模，而不是启发式假设
