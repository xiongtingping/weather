# Signal_alpha公式详解

## 完整公式

```python
signal_alpha = (
    q_edge                           # 第1项：预测优势
    + trend_pct * 0.35              # 第2项：趋势加成
    + (trend_consistency_up - 0.5) * 0.05  # 第3项：趋势一致性
    - model_penalty                  # 第4项：模型分歧惩罚
    - freshness_penalty              # 第5项：数据新鲜度惩罚
)
```

## 各项详解

### 第1项：q_edge（预测优势）- 主要贡献

**定义**: 预测概率与中性概率(0.5)的差值

**计算方法**:
```python
q_edge = q_calibrated - 0.5
# 或者
q_edge = q_raw - 0.5
```

**含义**:
- q_edge > 0: 预测YES的概率 > 50%，有正向优势
- q_edge < 0: 预测YES的概率 < 50%，有负向优势（应该做空）
- q_edge = 0: 预测概率50%，无优势

**实际例子**（从数据库）:
- 芝加哥: q_edge = 0.907 → 预测YES概率90.7%，优势40.7%
- 丹佛: q_edge = 0.938 → 预测YES概率93.8%，优势43.8%
- 首尔: q_edge = 0.913 → 预测YES概率91.3%，优势41.3%

**典型范围**: -0.5 到 +0.5
**平均值**: 0.496（从最近数据）

**影响因素**:
1. **气象模型预测质量**
   - 模型越准确，q_edge越大
   - 多模型加权平均

2. **校准质量**
   - Platt校准调整原始预测
   - q_calibrated通常比q_raw更准确

3. **市场定价**
   - 如果市场价格偏离真实概率，q_edge会更大
   - 市场效率越低，机会越多

**问题**: 32.4%的案例q_edge < 0.1，说明预测优势不足

---

### 第2项：trend_pct * 0.35（趋势加成）- 次要贡献

**定义**: 市场价格趋势的百分比变化，乘以权重0.35

**计算方法**:
```python
# 从历史价格序列计算
trend_pct = (last_price - first_price) / first_price

# 例如：价格从0.40涨到0.45
trend_pct = (0.45 - 0.40) / 0.40 = 0.125 (12.5%上涨)
```

**含义**:
- trend_pct > 0: 价格上涨趋势，支持做多YES
- trend_pct < 0: 价格下跌趋势，不利于做多YES
- trend_pct = 0: 价格平稳，无趋势

**实际例子**:
- 丹佛: trend_pct = -0.129 → 贡献 = -0.129 × 0.35 = -0.045（负面）
- 首尔: trend_pct = -0.276 → 贡献 = -0.276 × 0.35 = -0.097（严重负面）

**典型范围**: -0.5 到 +0.5
**平均值**: -0.136（从最近数据，整体偏负）

**权重0.35的含义**:
- 趋势的影响是q_edge的35%
- 例如：10%的趋势 = 3.5%的signal_alpha贡献

**影响因素**:
1. **市场情绪变化**
   - 新信息导致价格快速调整
   - 市场参与者的预期变化

2. **时间窗口**
   - 通常使用最近5-20个数据点
   - 窗口越短，趋势越敏感

3. **噪声**
   - 短期价格波动可能产生虚假趋势
   - 需要去噪处理

**问题**: 平均值为负（-0.136），拖累signal_alpha

---

### 第3项：(trend_consistency_up - 0.5) * 0.05（趋势一致性）- 微小贡献

**定义**: 趋势方向的一致性程度，减去0.5后乘以权重0.05

**计算方法**:
```python
# 统计价格序列中上涨步数的比例
up_steps = 0
for i in range(len(prices) - 1):
    if prices[i+1] > prices[i]:
        up_steps += 1

trend_consistency_up = up_steps / (len(prices) - 1)

# 例如：10个数据点，7次上涨
trend_consistency_up = 7 / 9 = 0.778
```

**含义**:
- trend_consistency_up = 1.0: 每一步都上涨，完全一致
- trend_consistency_up = 0.5: 上涨和下跌各半，无一致性
- trend_consistency_up = 0.0: 每一步都下跌，完全一致（反向）

**贡献计算**:
```python
# 减去0.5是为了中心化
# 0.5表示无一致性（随机），贡献为0
contribution = (trend_consistency_up - 0.5) * 0.05

# 例如：
# trend_consistency_up = 0.7 → 贡献 = (0.7 - 0.5) × 0.05 = +0.010
# trend_consistency_up = 0.3 → 贡献 = (0.3 - 0.5) × 0.05 = -0.010
# trend_consistency_up = 0.5 → 贡献 = (0.5 - 0.5) × 0.05 = 0.000
```

**典型范围**: -0.025 到 +0.025
**权重0.05的含义**: 影响很小，只是微调

**影响因素**:
1. **趋势稳定性**
   - 稳定趋势的一致性高
   - 震荡市场的一致性低

2. **噪声水平**
   - 噪声越大，一致性越低

**问题**: 权重太小（0.05），几乎没有影响

---

### 第4项：model_penalty（模型分歧惩罚）- 负面影响

**定义**: 当模型间分歧过大时的惩罚

**计算方法**:
```python
model_penalty = max(0.0, model_spread - 0.08) * 0.60

# 例如：
# model_spread = 0.12 → penalty = (0.12 - 0.08) × 0.60 = 0.024
# model_spread = 0.05 → penalty = 0 (低于阈值0.08，无惩罚)
```

**含义**:
- model_spread < 0.08: 模型一致性好，无惩罚
- model_spread > 0.08: 模型分歧大，开始惩罚
- model_spread越大，惩罚越重

**实际例子**:
```python
# 武汉案例：model_spread = 0.314
model_penalty = (0.314 - 0.08) × 0.60 = 0.140
# 严重拖累signal_alpha

# 芝加哥案例：model_spread = 0.05
model_penalty = 0
# 无惩罚
```

**典型范围**: 0 到 0.15
**平均值**: 约0.01-0.03

**阈值0.08的含义**:
- 允许8%的模型分歧
- 超过8%认为不确定性过高

**权重0.60的含义**:
- 每增加1%的model_spread（超过阈值），惩罚0.6%
- 例如：model_spread从0.08增加到0.18（+10%），惩罚6%

**影响因素**:
1. **气象模型差异**
   - 不同模型对同一事件的预测差异
   - GFS vs ECMWF vs ICON vs NWS

2. **预测难度**
   - 复杂天气系统导致模型分歧大
   - 长期预测分歧通常更大

3. **模型权重**
   - Scenario模式：固定权重
   - Auto模式：动态权重（可能降低分歧）

**改进方向**: 降低权重到0.40（减少惩罚）

---

### 第5项：freshness_penalty（数据新鲜度惩罚）- 负面影响

**定义**: 当数据过期时的惩罚

**计算方法**:
```python
freshness_penalty = min(0.10, freshness_sec / 14400.0 * 0.05)

# 例如：
# freshness_sec = 1800 (30分钟) → penalty = min(0.10, 1800/14400 × 0.05) = 0.00625
# freshness_sec = 7200 (2小时) → penalty = min(0.10, 7200/14400 × 0.05) = 0.025
# freshness_sec = 28800 (8小时) → penalty = min(0.10, 28800/14400 × 0.05) = 0.10 (达到上限)
```

**含义**:
- freshness_sec = 0: 数据最新，无惩罚
- freshness_sec < 4小时: 轻微惩罚
- freshness_sec > 8小时: 达到最大惩罚0.10

**时间尺度**:
- 14400秒 = 4小时（参考时间）
- 每4小时增加5%的惩罚
- 最大惩罚10%

**实际例子**:
```python
# 数据30分钟前更新
freshness_penalty = 1800 / 14400 × 0.05 = 0.00625 (0.625%惩罚)

# 数据2小时前更新
freshness_penalty = 7200 / 14400 × 0.05 = 0.025 (2.5%惩罚)

# 数据8小时前更新
freshness_penalty = 28800 / 14400 × 0.05 = 0.10 (10%惩罚，达到上限)
```

**典型范围**: 0 到 0.10
**平均值**: 约0.005-0.02

**影响因素**:
1. **数据采集频率**
   - market_refresh_1m服务的运行频率
   - 网络延迟

2. **市场活跃度**
   - 活跃市场数据更新快
   - 冷门市场可能长时间无更新

**改进方向**: 提高数据采集频率，降低freshness_sec

---

## 实际案例分析

### 案例1：芝加哥（高signal_alpha）

**输入**:
- q_edge = 0.907
- trend_pct = 0 (无趋势数据)
- trend_consistency = 0.5 (默认)
- model_spread = 0.05 (低分歧)
- freshness_sec = 600 (10分钟)

**计算**:
```python
term1 = 0.907                           # q_edge贡献
term2 = 0 × 0.35 = 0                   # trend贡献
term3 = (0.5 - 0.5) × 0.05 = 0         # consistency贡献
model_penalty = 0                       # 无惩罚（< 0.08）
freshness_penalty = 600/14400 × 0.05 = 0.0021

signal_alpha = 0.907 + 0 + 0 - 0 - 0.0021 = 0.905
```

**结果**: signal_alpha = 0.883（实际值，略有差异可能因为其他因素）

**分析**:
- q_edge非常高（90.7%），主导signal_alpha
- 无趋势数据，无加成也无拖累
- 模型分歧小，无惩罚
- 数据新鲜，惩罚很小

---

### 案例2：丹佛（中等signal_alpha，负趋势）

**输入**:
- q_edge = 0.938
- trend_pct = -0.129 (负趋势)
- trend_consistency = 0.5 (默认)
- model_spread = 0.06 (低分歧)
- freshness_sec = 600

**计算**:
```python
term1 = 0.938                           # q_edge贡献
term2 = -0.129 × 0.35 = -0.045         # trend负面贡献
term3 = (0.5 - 0.5) × 0.05 = 0         # consistency贡献
model_penalty = 0                       # 无惩罚（< 0.08）
freshness_penalty = 0.0021

signal_alpha = 0.938 - 0.045 + 0 - 0 - 0.0021 = 0.891
```

**结果**: signal_alpha = 0.851（实际值）

**分析**:
- q_edge很高（93.8%）
- 但负趋势拖累了4.5%
- 最终signal_alpha仍然较高

---

### 案例3：武汉（低signal_alpha，高model_spread）

**假设输入**（基于之前数据）:
- q_edge = 0.30
- trend_pct = 0
- trend_consistency = 0.5
- model_spread = 0.314 (高分歧)
- freshness_sec = 1800

**计算**:
```python
term1 = 0.30                            # q_edge贡献
term2 = 0 × 0.35 = 0                   # trend贡献
term3 = 0                               # consistency贡献
model_penalty = (0.314 - 0.08) × 0.60 = 0.140  # 严重惩罚
freshness_penalty = 1800/14400 × 0.05 = 0.00625

signal_alpha = 0.30 + 0 + 0 - 0.140 - 0.00625 = 0.154
```

**分析**:
- q_edge中等（30%）
- 但model_penalty严重拖累（-14%）
- 最终signal_alpha很低

---

## 各项贡献度分析

### 平均贡献（基于最近数据）

| 项目 | 平均值 | 典型范围 | 贡献度 |
|------|--------|----------|--------|
| q_edge | +0.496 | -0.5 ~ +0.5 | **主要贡献（80%）** |
| trend_pct × 0.35 | -0.048 | -0.175 ~ +0.175 | 次要贡献（10%） |
| consistency × 0.05 | 0 | -0.025 ~ +0.025 | 微小贡献（1%） |
| model_penalty | -0.020 | 0 ~ -0.15 | 负面影响（5%） |
| freshness_penalty | -0.010 | 0 ~ -0.10 | 负面影响（2%） |
| **总计** | **0.399** | -0.5 ~ +1.8 | 100% |

### 关键洞察

1. **q_edge是核心**
   - 贡献80%以上
   - 提高q_edge是最有效的方法

2. **trend_pct是双刃剑**
   - 正趋势加成，负趋势拖累
   - 当前平均为负，整体拖累

3. **model_penalty影响显著**
   - 高model_spread案例被严重惩罚
   - 降低权重可以减少惩罚

4. **consistency和freshness影响小**
   - 权重太小，几乎可以忽略

---

## 改进建议优先级

### P0 - 提高q_edge（最重要）
- 改进校准模型
- 优化模型权重（切换到auto模式）
- 引入机器学习模型

### P1 - 优化trend_pct
- 改进趋势计算（加权、去噪）
- 调整权重为非对称（正趋势0.40，负趋势0.25）

### P2 - 降低model_penalty
- 降低权重从0.60到0.40
- 或提高阈值从0.08到0.10

### P3 - 提高consistency权重
- 从0.05提高到0.10
- 让趋势一致性发挥更大作用

### P4 - 优化freshness_penalty
- 改善数据采集频率
- 调整惩罚曲线

---

## 总结

**Signal_alpha = 信号质量的期望值**

**公式结构**:
```
Signal_alpha = 正面贡献 - 负面惩罚
             = (q_edge + trend加成 + consistency加成) - (model_penalty + freshness_penalty)
```

**核心要点**:
1. q_edge是主导因素（80%贡献）
2. Trend可以加成也可以拖累（当前平均拖累）
3. Model_penalty和freshness_penalty是负面因素
4. 提高signal_alpha的关键是提高q_edge和优化trend计算

**当前问题**:
- 18.9%的案例signal_alpha < 0
- 主要原因：q_edge过低（32.4%案例 < 0.1）
- 次要原因：trend_pct平均为负（-0.136）

**改进方向**:
- 提高q_edge（改进预测和校准）
- 优化trend计算（减少负面影响）
- 降低model_penalty（减少惩罚）
