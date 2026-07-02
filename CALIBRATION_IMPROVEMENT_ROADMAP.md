# 校准层改进路线图

## 当前状态（2026-04-24）

### 现有校准层
- **方法**: Platt Scaling（Logistic Regression）
- **样本数**: 3,159（60天窗口）
- **ECE**: 0.0117（优秀）
- **Brier Score**: 0.0762（可接受）
- **分段**: d0/d1/d2p（按提前期）

### 评估结论
✅ **当前 Platt 校准效果已经很好，暂时不需要更换**

---

## 改进路线图

### 阶段1：监控和数据收集（1-2周）

**目标**: 验证当前配置的实际效果

**任务**:
1. **监控 PnL 表现**
   ```bash
   # 每天检查
   sqlite3 data/archive.db "
   SELECT
     COUNT(*) as trades,
     SUM(realized_pnl) as total_pnl,
     AVG(realized_pnl) as avg_pnl,
     ROUND(AVG(realized_pnl) / AVG(ABS(realized_pnl)) * 100, 1) as win_rate
   FROM edge_trades
   WHERE updated_at > datetime('now', '-7 days')
     AND realized_pnl IS NOT NULL
   "
   ```

2. **监控通过率**
   ```bash
   # 每小时检查
   sqlite3 data/archive.db "
   SELECT
     COUNT(*) as total,
     SUM(CASE WHEN json_extract(details_json, '$.edge_signal_lcb') >= 0.35 THEN 1 ELSE 0 END) as passed,
     ROUND(100.0 * SUM(CASE WHEN json_extract(details_json, '$.edge_signal_lcb') >= 0.35 THEN 1 ELSE 0 END) / COUNT(*), 1) as pass_rate
   FROM auto_trade_decisions
   WHERE created_at > datetime('now', '-1 hour')
   "
   ```

3. **收集更多结算数据**
   - 目标: 5000+ 样本
   - 当前: 3159 样本
   - 增长率: ~200-300 样本/周

**成功标准**:
- 通过率稳定在 55-60%
- PnL 为正或持平
- 无明显系统性偏差

**如果失败**: 进入阶段2

---

### 阶段2：诊断和分析（2-4周）

**触发条件**（满足任一）:
1. PnL 持续为负（> 2周）
2. 通过率 < 50% 或 > 70%
3. 发现特定城市/季节的系统性偏差
4. ECE 在某些分层中 > 0.05

**任务**:

#### 2.1 分层分析
```python
# 按城市分析
SELECT
  city,
  COUNT(*) as samples,
  ROUND(AVG(q_calibrated - q_raw), 3) as avg_shift,
  ROUND(AVG(ABS(q_calibrated - settlement_hit)), 3) as avg_error
FROM calibration_samples cs
JOIN event_runs er ON cs.slug = er.slug
WHERE settlement_actual IS NOT NULL
GROUP BY city
ORDER BY avg_error DESC
```

#### 2.2 季节分析
```python
# 按季节分析
SELECT
  CASE
    WHEN CAST(strftime('%m', event_date) AS INTEGER) IN (12,1,2) THEN 'winter'
    WHEN CAST(strftime('%m', event_date) AS INTEGER) IN (3,4,5) THEN 'spring'
    WHEN CAST(strftime('%m', event_date) AS INTEGER) IN (6,7,8) THEN 'summer'
    ELSE 'fall'
  END as season,
  COUNT(*) as samples,
  ROUND(AVG(q_calibrated - q_raw), 3) as avg_shift
FROM calibration_samples
GROUP BY season
```

#### 2.3 Reliability Diagram
```python
# 生成 Reliability Diagram 数据
import matplotlib.pyplot as plt
import numpy as np

# 从数据库读取
q_calibrated = [...]  # 校准后概率
actual_hit = [...]    # 实际是否命中

# 分桶
bins = np.linspace(0, 1, 11)
bin_centers = (bins[:-1] + bins[1:]) / 2
bin_counts = []
bin_accuracies = []

for i in range(len(bins)-1):
    mask = (q_calibrated >= bins[i]) & (q_calibrated < bins[i+1])
    if mask.sum() > 0:
        bin_counts.append(mask.sum())
        bin_accuracies.append(actual_hit[mask].mean())
    else:
        bin_counts.append(0)
        bin_accuracies.append(0)

# 绘图
plt.plot([0, 1], [0, 1], 'k--', label='Perfect calibration')
plt.scatter(bin_centers, bin_accuracies, s=bin_counts, alpha=0.5)
plt.xlabel('Predicted probability')
plt.ylabel('Actual frequency')
plt.title('Reliability Diagram')
plt.legend()
plt.savefig('reliability_diagram.png')
```

**输出**: 诊断报告，识别问题区域

**如果发现问题**: 进入阶段3

---

### 阶段3：实施改进（4-8周）

**方案选择**（按优先级）:

#### 方案A：分层 Platt 校准（推荐首选）

**适用场景**: 发现特定城市/季节有系统性偏差

**实施**:
```python
# 按城市 + 提前期 + 季节分层训练
calibrators = {}

for city in ['atlanta', 'seoul', 'london', ...]:
    for dte in ['d0', 'd1', 'd2p']:
        for season in ['winter', 'spring', 'summer', 'fall']:
            key = f"{city}_{dte}_{season}"
            samples = filter_samples(city, dte, season)

            if len(samples) >= 50:  # 最小样本数
                calibrators[key] = train_platt(samples)
            else:
                # Fallback 到更粗粒度
                calibrators[key] = calibrators.get(f"{city}_{dte}", global_calibrator)
```

**优点**:
- 简单，基于现有 Platt 框架
- 可解释性强
- 样本需求适中（50+/层）

**缺点**:
- 需要更多样本
- 可能过拟合

---

#### 方案B：Isotonic Regression（推荐次选）

**适用场景**: Platt 校准不够灵活，Reliability Diagram 显示非单调关系

**实施**:
```python
from sklearn.isotonic import IsotonicRegression

def train_isotonic_calibrator(q_raw_samples, actual_labels):
    """
    非参数校准，更灵活
    """
    iso_reg = IsotonicRegression(out_of_bounds='clip')
    iso_reg.fit(q_raw_samples, actual_labels)
    return iso_reg

def calibrate_with_isotonic(q_raw, iso_reg):
    return iso_reg.predict([q_raw])[0]
```

**优点**:
- 非参数，更灵活
- 不假设特定函数形式
- 保证单调性

**缺点**:
- 需要更多样本（建议 1000+）
- 可能过拟合
- 不可解释

**对比 Platt**:
```python
# Platt: 假设 logistic 关系
q_cal = sigmoid(a * logit(q_raw) + b)

# Isotonic: 学习任意单调关系
q_cal = learned_monotonic_function(q_raw)
```

---

#### 方案C：EMOS（长期方案）

**适用场景**: 样本数 > 5000，想利用 ensemble 分布信息

**实施**:
```python
from scipy.stats import norm
from scipy.optimize import minimize

def emos_calibration(ensemble_members, actual_temp):
    """
    Ensemble Model Output Statistics
    拟合: P(T) ~ N(a + b*ensemble_mean, c + d*ensemble_spread)
    """
    ensemble_mean = np.mean(ensemble_members, axis=1)
    ensemble_spread = np.std(ensemble_members, axis=1)

    def negative_log_likelihood(params):
        a, b, c, d = params
        mu = a + b * ensemble_mean
        sigma = c + d * ensemble_spread
        sigma = np.maximum(sigma, 0.1)
        return -np.sum(norm.logpdf(actual_temp, mu, sigma))

    result = minimize(
        negative_log_likelihood,
        x0=[0, 1, 1, 1],
        method='L-BFGS-B',
        bounds=[(-10, 10), (0, 2), (0.1, 10), (0, 2)]
    )

    return result.x  # [a, b, c, d]

def predict_with_emos(ensemble_members, emos_params, outcome_spec):
    """
    使用 EMOS 预测概率
    """
    a, b, c, d = emos_params
    ensemble_mean = np.mean(ensemble_members)
    ensemble_spread = np.std(ensemble_members)

    mu = a + b * ensemble_mean
    sigma = c + d * ensemble_spread

    # 计算 outcome 的概率
    if outcome_spec.kind == "le":
        return norm.cdf(outcome_spec.k, mu, sigma)
    elif outcome_spec.kind == "range":
        return norm.cdf(outcome_spec.k2, mu, sigma) - norm.cdf(outcome_spec.k, mu, sigma)
    # ...
```

**优点**:
- 利用 ensemble 分布信息
- 理论基础扎实
- 适合温度预测

**缺点**:
- 需要大量样本（5000+）
- 实施复杂
- 需要 ensemble 成员数据（不只是 q_raw）

---

#### 方案D：BMA（长期方案）

**适用场景**: 样本数 > 10000，想考虑每个模型的权重

**实施**:
```python
def bma_calibration(model_forecasts, actual_outcomes):
    """
    Bayesian Model Averaging
    P(outcome) = Σ w_i * P_i(outcome)
    其中 w_i 是模型 i 的权重（基于历史表现）
    """
    n_models = len(model_forecasts[0])

    # 初始化权重（均匀）
    weights = np.ones(n_models) / n_models

    # EM 算法优化权重
    for iteration in range(100):
        # E-step: 计算每个模型对每个样本的贡献
        responsibilities = []
        for forecast, actual in zip(model_forecasts, actual_outcomes):
            likelihood = [model_likelihood(f, actual) for f in forecast]
            posterior = weights * likelihood
            posterior /= posterior.sum()
            responsibilities.append(posterior)

        # M-step: 更新权重
        weights = np.mean(responsibilities, axis=0)

    return weights
```

**优点**:
- 考虑模型权重
- 理论基础扎实
- 可以识别最优模型组合

**缺点**:
- 需要大量样本（10000+）
- 计算复杂
- 需要每个模型的独立预测

---

### 阶段4：评估和迭代（持续）

**评估指标**:
1. **ECE** (Expected Calibration Error) - 目标 < 0.05
2. **Brier Score** - 越低越好
3. **Log Loss** - 越低越好
4. **实际 PnL** - 最终目标

**A/B 测试**:
```python
# 同时运行新旧校准器
if random.random() < 0.5:
    q_calibrated = old_calibrator(q_raw)
    variant = "control"
else:
    q_calibrated = new_calibrator(q_raw)
    variant = "treatment"

# 记录 variant 用于后续分析
```

**回滚标准**:
- 新校准器 ECE > 旧校准器 ECE + 0.01
- 新校准器 PnL < 旧校准器 PnL（持续 1周）

---

## 决策树

```
开始
  ↓
当前 ECE < 0.05？
  ├─ 是 → 保持现状，进入阶段1（监控）
  └─ 否 → 进入阶段2（诊断）
       ↓
     发现系统性偏差？
       ├─ 是，按城市/季节 → 方案A（分层 Platt）
       ├─ 是，非线性关系 → 方案B（Isotonic）
       └─ 否，但 ECE 仍高 → 收集更多数据
            ↓
          样本数 > 5000？
            ├─ 是 → 方案C（EMOS）或 方案D（BMA）
            └─ 否 → 继续收集数据
```

---

## 当前建议（2026-04-24）

### 立即行动
✅ **保持现状**
- 当前 Platt 校准效果优秀（ECE=0.0117）
- 继续每日自动训练
- 样本数持续增长

### 本周任务
1. 监控通过率（目标 55-60%）
2. 监控 PnL 表现
3. 收集更多结算数据

### 2周后评估
- 如果 PnL 为正 → 继续保持
- 如果 PnL 为负 → 进入阶段2（诊断）

### 1个月后考虑
- 如果样本数 > 5000 → 考虑分层校准
- 如果发现系统性偏差 → 考虑 Isotonic Regression

---

## 总结

**当前不需要引入更强力的校准层**，因为：
1. ✅ ECE = 0.0117（优秀）
2. ✅ 样本数充足且增长中
3. ✅ 已有分段校准
4. ⏳ 需要先验证 PnL

**但要做好准备**：
- 监控系统，识别问题
- 收集数据，为未来改进做准备
- 了解各种校准方法，知道何时使用

**记住**：过早优化是万恶之源。先让当前配置运行一段时间，用数据说话。
