# 如何提高Signal_alpha - 详细分析与建议

## 当前状态分析

### Signal_alpha统计（最近10分钟）
- **平均值**: 0.399
- **最小值**: -0.495
- **最大值**: 1.755
- **分布**:
  - < 0（负值）: 322个（18.4%）
  - 0-0.2（很低）: 511个（29.2%）
  - 0.2-0.5（低）: 200个（11.4%）
  - 0.5-0.8（中）: 107个（6.1%）
  - >= 0.8（高）: 635个（36.3%）

### 问题诊断
- **32.4%的案例q_edge < 0.1**（q_edge过低）
- **6.5%的案例trend_pct < -0.2**（负趋势）
- **18.9%的案例signal_alpha < 0**（负期望值）

## Signal_alpha计算公式

```python
signal_alpha = (
    q_edge                           # 预测优势（主要贡献）
    + trend_pct * 0.35              # 趋势加成（次要贡献）
    + (trend_consistency_up - 0.5) * 0.05  # 趋势一致性（微小贡献）
    - model_penalty                  # 模型分歧惩罚
    - freshness_penalty              # 数据新鲜度惩罚
)
```

### 各组成部分分析

#### 1. q_edge（主要贡献，平均0.496）
**定义**: 预测概率与市场价格的差值
```python
q_edge = q_calibrated - 0.5  # 或 q_raw - 0.5
```

**问题**: 32.4%的案例q_edge < 0.1，说明预测优势不足

**改进方向**:

##### A. 提高预测准确性（最重要）
**当前问题**: q_calibrated可能不够准确

**改进方案**:
1. **改进校准模型**
   - 当前使用Platt校准
   - 考虑使用更先进的校准方法（Isotonic Regression, Beta Calibration）
   - 增加校准数据量
   - 细化校准分段（按地区、时间、市场类型）

2. **优化模型权重**
   - 当前scenario模式使用固定权重
   - 切换到auto模式，自动学习最佳权重
   - 考虑动态权重（根据最近表现调整）

3. **改进基础预测模型**
   - 分析各气象模型的系统性偏差
   - 考虑引入机器学习模型（GBDT, Neural Network）
   - 结合历史数据和实时观测

##### B. 优化q_edge计算逻辑
**当前逻辑**: 简单的 q - 0.5

**改进方案**:
1. **考虑市场偏差**
   ```python
   # 当前
   q_edge = q_calibrated - 0.5

   # 改进：考虑市场系统性偏差
   market_bias = estimate_market_bias(slug, outcome)
   q_edge = q_calibrated - (0.5 + market_bias)
   ```

2. **动态参考点**
   ```python
   # 当前：固定0.5
   # 改进：使用市场隐含概率作为参考
   q_edge = q_calibrated - market_implied_prob
   ```

#### 2. trend_pct（次要贡献，平均-0.136）
**定义**: 价格趋势百分比变化

**问题**:
- 平均值为负（-0.136），拖累signal_alpha
- 6.5%的案例trend_pct < -0.2，严重负面影响

**改进方向**:

##### A. 改进趋势计算方法
**当前方法**: 简单的线性回归或百分比变化

**改进方案**:
1. **加权趋势**（近期数据权重更高）
   ```python
   # 当前：等权重
   trend_pct = (last - first) / first

   # 改进：指数加权
   weights = np.exp(np.linspace(-1, 0, len(series)))
   trend_pct = weighted_regression(series, weights)
   ```

2. **去噪处理**
   ```python
   # 使用移动平均或Kalman滤波去除噪声
   smoothed_series = moving_average(series, window=3)
   trend_pct = calculate_trend(smoothed_series)
   ```

3. **多时间尺度趋势**
   ```python
   # 结合短期和长期趋势
   short_term_trend = calculate_trend(series[-5:])
   long_term_trend = calculate_trend(series[-20:])
   trend_pct = 0.7 * short_term_trend + 0.3 * long_term_trend
   ```

##### B. 调整趋势权重
**当前权重**: 0.35

**问题**: 负趋势的惩罚可能过重

**改进方案**:
1. **非对称权重**
   ```python
   # 当前
   signal_alpha += trend_pct * 0.35

   # 改进：正趋势权重更高
   if trend_pct > 0:
       signal_alpha += trend_pct * 0.40  # 正趋势权重更高
   else:
       signal_alpha += trend_pct * 0.25  # 负趋势权重降低
   ```

2. **动态权重**（基于趋势可靠性）
   ```python
   trend_weight = 0.35 * trend_consistency_up
   signal_alpha += trend_pct * trend_weight
   ```

##### C. 改进趋势方向判断
**当前问题**: 可能误判趋势方向

**改进方案**:
1. **使用统计显著性检验**
   ```python
   # 只有统计显著的趋势才使用
   if trend_p_value < 0.05:
       signal_alpha += trend_pct * 0.35
   ```

2. **结合成交量**
   ```python
   # 成交量确认的趋势更可靠
   volume_confirmed = check_volume_confirmation(series, volume_series)
   if volume_confirmed:
       signal_alpha += trend_pct * 0.45
   else:
       signal_alpha += trend_pct * 0.25
   ```

#### 3. trend_consistency_up（微小贡献）
**定义**: 趋势一致性（0-1）

**当前权重**: (trend_consistency_up - 0.5) * 0.05

**改进方向**:
1. **提高权重**（如果趋势一致性高，应该更信任）
   ```python
   # 当前
   signal_alpha += (trend_consistency_up - 0.5) * 0.05

   # 改进
   signal_alpha += (trend_consistency_up - 0.5) * 0.10
   ```

2. **与trend_pct联动**
   ```python
   # 一致性高时，趋势权重更大
   effective_trend_weight = 0.35 * (0.5 + trend_consistency_up)
   signal_alpha += trend_pct * effective_trend_weight
   ```

#### 4. model_penalty（负面影响）
**定义**: 模型分歧惩罚
```python
model_penalty = max(0.0, model_spread - 0.08) * 0.60
```

**改进方向**:
1. **已完成**: model_spread权重从0.40提高到0.60（在signal_sigma中）
2. **进一步优化**: 降低model_penalty的权重
   ```python
   # 当前
   model_penalty = max(0.0, model_spread - 0.08) * 0.60

   # 改进：降低惩罚
   model_penalty = max(0.0, model_spread - 0.08) * 0.40
   ```

#### 5. freshness_penalty（负面影响）
**定义**: 数据新鲜度惩罚
```python
freshness_penalty = min(0.10, freshness_sec / 14400.0 * 0.05)
```

**改进方向**:
1. **改善数据采集**（已在进行）
2. **调整惩罚曲线**
   ```python
   # 当前：线性惩罚
   freshness_penalty = min(0.10, freshness_sec / 14400.0 * 0.05)

   # 改进：指数惩罚（初期惩罚小，后期惩罚大）
   freshness_penalty = 0.10 * (1 - exp(-freshness_sec / 3600.0))
   ```

## 优先级排序

### P0 - 立即实施（高影响，低风险）

#### 1. 切换到Auto模式
**时机**: 现在（已积累足够数据）
**预期效果**: model_spread降低10-20%，signal_alpha提升5-10%
**风险**: 低
**实施**:
```bash
# 修改.env
PM_SCAN_WEIGHTING=auto

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
```

#### 2. 降低model_penalty权重
**当前**: 0.60
**建议**: 0.40
**预期效果**: signal_alpha平均提升0.01-0.02
**风险**: 低
**实施**: 修改edge_signal_runtime.py Line 52-54

#### 3. 调整趋势权重（非对称）
**当前**: 固定0.35
**建议**: 正趋势0.40，负趋势0.25
**预期效果**: signal_alpha平均提升0.02-0.03
**风险**: 低
**实施**: 修改edge_signal_runtime.py Line 61

### P1 - 短期实施（高影响，中风险）

#### 4. 改进趋势计算（加权+去噪）
**当前**: 简单线性趋势
**建议**: 指数加权 + 移动平均去噪
**预期效果**: trend_pct更准确，signal_alpha提升0.03-0.05
**风险**: 中（需要测试）
**实施**: 修改_trend_stats_from_series函数

#### 5. 优化q_edge计算（考虑市场偏差）
**当前**: q_calibrated - 0.5
**建议**: q_calibrated - (0.5 + market_bias)
**预期效果**: q_edge更准确，signal_alpha提升0.02-0.04
**风险**: 中（需要估计market_bias）
**实施**: 新增market_bias估计函数

### P2 - 中期实施（高影响，高风险）

#### 6. 改进校准模型
**当前**: Platt校准
**建议**: Beta Calibration或Isotonic Regression
**预期效果**: q_calibrated更准确，signal_alpha提升0.05-0.10
**风险**: 高（需要大量测试）
**实施**: 修改校准pipeline

#### 7. 引入机器学习模型
**当前**: 基于气象模型加权
**建议**: GBDT或Neural Network
**预期效果**: 预测准确性显著提升
**风险**: 高（需要训练和验证）
**实施**: 新增ML pipeline

## 快速实施方案（本周可完成）

### 方案1: 三管齐下（预期signal_alpha提升0.05-0.08）

1. **切换到Auto模式**（5分钟）
   ```bash
   echo "PM_SCAN_WEIGHTING=auto" >> /Users/xiong/weather/.env
   # 重启服务
   ```

2. **降低model_penalty权重**（10分钟）
   ```python
   # edge_signal_runtime.py Line 52-54
   # 修改前
   model_penalty = max(0.0, model_spread_f - 0.08) * 0.60

   # 修改后
   model_penalty = max(0.0, model_spread_f - 0.08) * 0.40
   ```

3. **调整趋势权重为非对称**（15分钟）
   ```python
   # edge_signal_runtime.py Line 59-65
   # 修改前
   signal_alpha = (
       float(q_edge_f)
       + float(trend_f) * 0.35
       + (float(trend_cons_f) - 0.5) * 0.05
       - model_penalty
       - freshness_penalty
   )

   # 修改后
   trend_weight = 0.40 if trend_f > 0 else 0.25
   signal_alpha = (
       float(q_edge_f)
       + float(trend_f) * trend_weight
       + (float(trend_cons_f) - 0.5) * 0.10  # 同时提高一致性权重
       - model_penalty
       - freshness_penalty
   )
   ```

### 方案2: 改进趋势计算（预期signal_alpha提升0.03-0.05）

修改_trend_stats_from_series函数，使用指数加权：
```python
# 在计算trend_pct时使用指数加权
weights = np.exp(np.linspace(-1, 0, len(points)))
weighted_points = [(t, v * w) for (t, v), w in zip(points, weights)]
# 然后计算加权趋势
```

## 预期效果总结

### 如果实施方案1（三管齐下）
- Signal_alpha平均值: 0.399 → 0.45-0.48
- Signal_alpha < 0的比例: 18.9% → 10-12%
- Signal_lcb通过率: 预计提升20-30%
- **预期每小时订单数**: 1-3个

### 如果同时实施方案1+方案2
- Signal_alpha平均值: 0.399 → 0.48-0.52
- Signal_alpha < 0的比例: 18.9% → 8-10%
- Signal_lcb通过率: 预计提升30-40%
- **预期每小时订单数**: 2-5个

## 建议实施顺序

1. **今天**: 切换到Auto模式（5分钟，立即见效）
2. **明天**: 降低model_penalty + 调整趋势权重（30分钟，测试验证）
3. **本周**: 改进趋势计算（2-3小时，需要测试）
4. **下周**: 优化q_edge计算（1天，需要分析market_bias）
5. **下月**: 改进校准模型（1-2周，需要大量测试）

## 风险提示

1. **不要一次修改太多**：逐步实施，每次修改后观察24-48小时
2. **保留回退方案**：每次修改前备份，确保可以快速回退
3. **监控关键指标**：signal_alpha分布、signal_lcb通过率、订单质量
4. **避免过度优化**：不要为了提高signal_alpha而牺牲预测准确性

## 总结

**核心问题**: Signal_alpha过低（平均0.399，18.9%为负值）

**主要原因**:
1. q_edge不够大（32.4%案例 < 0.1）
2. trend_pct平均为负（-0.136）
3. model_penalty和freshness_penalty的负面影响

**最有效的改进**:
1. 切换到Auto模式（立即，低风险）
2. 降低model_penalty权重（快速，低风险）
3. 调整趋势权重为非对称（快速，低风险）
4. 改进趋势计算（短期，中风险）
5. 优化校准模型（中期，高风险）

**预期效果**: 实施前3项后，signal_alpha平均提升0.05-0.08，预计每小时1-3个订单。
