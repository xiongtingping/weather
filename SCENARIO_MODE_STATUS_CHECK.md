# Scenario模式状态检查报告

## 检查时间
2026-04-25 03:50 (UTC)

## 当前配置状态

### 1. Weighting模式
```bash
PM_SCAN_WEIGHTING=scenario  ✅ 已启用
```

### 2. 服务状态
- ✅ auto_trade_entry_1m服务运行中
- ✅ 代码修改已生效（阶段1+阶段2完成）

## 过去1小时运行数据

### 决策统计
- **总决策数**: 1,090
- **将要下单**: 0
- **处理事件数**: 67个独特事件

### 主要阻止原因
1. **plain_lcb_pinned_tail_no_filtered**: 282 (25.9%)
2. **market_closed**: 214 (19.6%)
3. **market_tick_stale**: 172 (15.8%)
4. **signal_lcb_below_min**: 86 (7.9%)
5. **plain_lcb_zero_prob_yes_filtered**: 68 (6.2%)
6. **spread_too_wide**: 60 (5.5%)
7. **edge_lcb_below_min**: 51 (4.7%)

### Model Spread分布（关键指标）

| 范围 | 数量 | 占比 | 评价 |
|------|------|------|------|
| < 0.05 | 340 | **56.0%** | ✅ 很好 |
| 0.05-0.10 | 77 | 12.7% | ✅ 好 |
| 0.10-0.15 | 78 | 12.9% | ⚠️ 一般 |
| >= 0.15 | 112 | 18.5% | ❌ 差 |

**平均model_spread**: 0.0729

### 被signal_lcb阻止的高价值案例

| 城市 | exec_edge_score | model_spread | 结果 |
|------|----------------|--------------|------|
| 武汉 | 8.39 | 0.314 | ❌ 被阻止 |
| 广州 | 5.83 | 0.346 | ❌ 被阻止 |
| 广州 | 5.16 | 0.073 | ❌ 被阻止 |
| 布宜诺斯艾利斯 | 5.09 | 0.055 | ❌ 被阻止 |
| 吉达 | 4.85 | 0.222 | ❌ 被阻止 |

## 分析结论

### ✅ Scenario模式已生效

**证据**:
1. 56%的案例model_spread < 0.05（非常好）
2. 平均model_spread = 0.0729（相比之前的0.10有显著改善）
3. 68.7%的案例model_spread < 0.10（好+很好）

### ❌ 但仍然0订单

**根本原因**:
1. **signal_lcb仍然为负或过低**
   - 即使model_spread降低了，signal_lcb仍然不够高
   - 86个案例被signal_lcb_below_min阻止
   - 高价值案例（exec_edge > 5）仍然被阻止

2. **其他门控仍然严格**
   - plain_lcb_pinned_tail_no_filtered: 282个
   - market_closed: 214个
   - market_tick_stale: 172个

3. **signal_lcb计算公式的问题**
   ```python
   signal_lcb = signal_alpha - signal_z * signal_sigma
   signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty + model_spread * 0.40
   ```

   即使model_spread从0.10降到0.07：
   - signal_sigma减少: 0.10 * 0.40 - 0.07 * 0.40 = 0.012
   - signal_lcb提升: 1.0 * 0.012 = 0.012

   **这个提升不够大！**

## 为什么model_spread降低但signal_lcb提升有限？

### 问题1: signal_sigma中model_spread的权重只有0.40
```python
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty + model_spread * 0.40
```

model_spread从0.10降到0.07，signal_sigma只减少0.012，signal_lcb只提升0.012。

### 问题2: signal_alpha本身就很低
从被阻止的案例来看，signal_alpha可能本身就接近0或为负，即使signal_sigma降低，signal_lcb仍然为负。

### 问题3: 其他惩罚项仍然存在
- model_penalty（基于model_spread > 0.08的惩罚）
- freshness_penalty（数据新鲜度惩罚）
- 这些惩罚项仍然在拉低signal_lcb

## 下一步建议

### 方案A: 进一步降低signal_lcb_min阈值（最直接）

**当前配置**:
```bash
PM_LCB_GLOBAL_SIGNAL_LCB_MIN=0.05
```

**建议修改**:
```bash
PM_LCB_GLOBAL_SIGNAL_LCB_MIN=0.0  # 允许signal_lcb=0的案例通过
```

**预期效果**:
- 解锁signal_lcb在[0, 0.05)范围的案例
- 可能解锁10-20%的被阻止案例

**风险**: 中等（允许更多边缘案例）

### 方案B: 降低signal_z（已经是1.0，不建议再降）

**当前配置**:
```bash
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.0
```

已经是最低合理值，不建议再降。

### 方案C: 修改signal_sigma计算公式（需要修改代码）

**当前公式**:
```python
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty + model_spread * 0.40
```

**建议修改**:
```python
signal_sigma = 0.025 + model_penalty * 0.35 + freshness_penalty + model_spread * 0.60  # 提高model_spread权重
```

**预期效果**:
- model_spread从0.10降到0.07时，signal_sigma减少0.018（而不是0.012）
- signal_lcb提升0.018（而不是0.012）
- 提升幅度增加50%

**风险**: 低（只是调整权重）

### 方案D: 切换到auto模式（需要更多数据）

**当前**: scenario模式
**建议**: 1-2周后切换到auto模式

**原因**:
- 需要积累足够的历史数据
- Auto模式可能发现更优的权重组合
- 进一步降低model_spread

## 立即行动建议

### 推荐：方案A（降低signal_lcb_min到0.0）

**理由**:
1. 最简单，无需修改代码
2. 风险可控
3. 可以立即看到效果

**操作步骤**:
```bash
# 1. 修改.env
echo "PM_LCB_GLOBAL_SIGNAL_LCB_MIN=0.0" >> /Users/xiong/weather/.env

# 2. 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist

# 3. 观察30分钟
# 查看是否有订单生成
```

### 如果方案A效果不佳，考虑方案C

修改`edge_signal_runtime.py`中的signal_sigma计算公式，提高model_spread的权重。

## 总结

**Scenario模式已成功上线并生效**:
- ✅ model_spread显著降低（平均0.0729，56%案例<0.05）
- ✅ 代码修改正确，地区+时间权重系统工作正常

**但仍然0订单的原因**:
- ❌ signal_lcb提升有限（model_spread降低30%，但signal_lcb只提升约12%）
- ❌ signal_lcb_min阈值仍然过高（0.05）
- ❌ 其他门控仍然严格

**建议**:
1. **立即**: 降低signal_lcb_min到0.0
2. **观察**: 30分钟后检查订单
3. **如果仍无订单**: 考虑修改signal_sigma公式，提高model_spread权重
4. **1-2周后**: 切换到auto模式，进一步优化
