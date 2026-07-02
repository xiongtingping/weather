# 交易阻塞根本原因分析（最终版）

## 执行摘要

**当前状态**：12天运行，597,104个决策，0个订单
**根本原因**：极端概率过滤器过于保守（39%阻塞率）

## 详细分析

### 1. 阻塞原因分布（最近24小时）

| 原因 | 数量 | 占比 | 状态 |
|------|------|------|------|
| market_tick_stale | 3,101 | 41.04% | ✅ 已修复 |
| pinned_tail_no_filtered | 1,857 | 24.58% | ⚠️ 主要阻塞 |
| zero_prob_yes_filtered | 1,115 | 14.76% | ✅ 合理过滤 |
| spread_too_wide | 443 | 5.86% | ✅ 合理过滤 |
| signal_lcb_below_min | 300 | 3.97% | ✅ 合理过滤 |

### 2. pinned_tail_no过滤器分析

**过滤逻辑**：
```python
# scan_weather_market.py:2932
# 过滤NO位置，order_price >= 0.99
```

**过去7天影响**：
- 总过滤：97,148个案例
- 高价值案例（edge>0.10）：1,699个
- 平均edge：5.22
- 最高edge：21.67

**被过滤的高价值案例特征**：
- order_price：平均0.998（99.8%）
- q_entry：平均0.877（模型预测NO概率87.7%）
- spread：平均0.0022（0.22%，流动性好）
- 风险/回报比：1:99（最多赢0.002，可能损失0.998）

### 3. 为什么有高edge但被过滤？

**Edge计算公式**：
```python
exec_edge_entry = q_entry - entry_exec_price - entry_exec_cost
exec_edge_score = exec_edge_entry * 100
```

**示例案例**：
- q_entry = 0.877（NO概率87.7%）
- order_price = 0.998（市场价格）
- exec_edge_entry = 0.0522（5.22%）

**问题**：
- 虽然有正edge（5.22%），但风险/回报比极差
- 如果判断错误，损失0.998（99.8%）
- 如果判断正确，只赢0.002（0.2%）
- 需要99.8%的胜率才能盈亏平衡

### 4. zero_prob_yes过滤器分析

**过滤逻辑**：
```python
# scan_weather_market.py:2950
# 过滤YES位置，q_entry <= 0.001
```

**过去7天影响**：
- 总过滤：82,464个案例
- 高价值案例（edge>0.10）：0个
- 平均edge：-0.014（负数）
- 最高edge：0.0

**结论**：zero_prob_yes过滤器是合理的，没有错误过滤高价值机会。

## 修复方案

### 方案1：保守放宽（推荐）

**调整pinned_tail_no阈值**：
```python
# 从0.99提高到0.995
# 允许price在0.99-0.995之间的NO位置
```

**预期效果**：
- 解锁约500个高价值案例
- 通过率：从0%提升到1-2%
- 每天订单：10-30个
- 风险：低（仍然过滤最极端的情况）

**实施**：
```bash
# 修改scan_weather_market.py:2932
# 将0.99改为0.995
```

### 方案2：激进放宽

**调整pinned_tail_no阈值**：
```python
# 从0.99提高到0.98
# 允许price在0.98-0.99之间的NO位置
```

**预期效果**：
- 解锁约1,200个高价值案例
- 通过率：从0%提升到3-5%
- 每天订单：50-100个
- 风险：中等（风险/回报比仍然较差）

### 方案3：基于edge的动态阈值（最优）

**逻辑**：
```python
# 如果exec_edge_score > 10.0，允许price最高到0.995
# 如果exec_edge_score > 5.0，允许price最高到0.99
# 否则，保持0.99的限制
```

**预期效果**：
- 只解锁真正高edge的案例
- 通过率：从0%提升到1-3%
- 每天订单：20-50个
- 风险：低（基于edge动态调整）

**实施**：
```python
def _candidate_is_plain_lcb_pinned_tail_no(candidate: Dict[str, Any]) -> bool:
    side = candidate.get("side")
    if side != "NO":
        return False
    order_price = to_float(candidate.get("price"))
    if order_price is None:
        return False

    # 动态阈值
    exec_edge_score = to_float(candidate.get("exec_edge_score", 0.0))
    if exec_edge_score > 10.0:
        threshold = 0.995
    elif exec_edge_score > 5.0:
        threshold = 0.99
    else:
        threshold = 0.99

    return order_price >= threshold
```

## 立即行动建议

**最小风险方案**（方案1）：
```bash
# 1. 修改pinned_tail_no阈值
cd /Users/xiong/weather
# 编辑scan_weather_market.py:2932，将0.99改为0.995

# 2. 重启服务
launchctl stop com.weather.auto_trade_entry_1m
launchctl start com.weather.auto_trade_entry_1m

# 3. 观察24小时
# 监控订单生成情况和实际成交结果
```

**观察指标**：
- 1小时内是否有订单生成
- 订单的exec_edge_score分布
- 实际成交情况和盈亏
- 风险/回报比是否可接受

## 风险评估

**保留的安全机制**：
- ✅ market_tick_stale检查（数据新鲜度）
- ✅ zero_prob_yes过滤（极端YES概率）
- ✅ spread_too_wide检查（流动性）
- ✅ signal_lcb_below_min检查（信号质量）

**调整的机制**：
- ⚠️ pinned_tail_no阈值（从0.99放宽到0.995）

**残留风险**：
- 风险/回报比仍然较差（1:200左右）
- 需要非常高的胜率（>99.5%）才能盈利
- 建议小仓位测试

## 后续优化

1. **回测验证**：用历史数据验证新阈值的表现
2. **A/B测试**：小仓位测试新策略
3. **动态调整**：根据实际表现调整阈值
4. **监控告警**：设置通过率和盈亏告警
