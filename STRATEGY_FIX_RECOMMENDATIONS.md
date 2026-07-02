# 策略失效修复建议

## 诊断结果

**当前状态**：
- 12天运行，597,104个决策
- 通过率：0%
- 历史订单：0个

**根本原因**：过度保守的多层过滤

## 修复方案

### 方案1：激进修复（立即恢复交易）

**调整参数**：
```bash
# 降低LCB阈值
PM_LCB_GLOBAL_EDGE_LCB_MIN=-0.01  # 从0.0005降到-0.01

# 放宽价差限制
PM_LCB_GLOBAL_MAX_SPREAD=0.12  # 从0.08提高到0.12

# 放宽极端概率过滤
# 修改代码：q_entry <= 0.005（从0.001）
# 修改代码：order_price >= 0.98（从0.99）
```

**预期效果**：
- 通过率：5-10%
- 每天订单：50-200个
- 风险：中等

### 方案2：保守修复（逐步放宽）

**第一步：降低LCB阈值**
```bash
PM_LCB_GLOBAL_EDGE_LCB_MIN=0.0001  # 从0.0005降到0.0001
```

**第二步：观察1-2天**
- 如果仍无订单，继续放宽

**第三步：调整极端概率过滤**
```python
# scan_weather_market.py
# 行2950左右：_candidate_is_plain_lcb_zero_prob_yes
q_entry_val <= 0.002  # 从0.001改为0.002

# 行2960左右：_candidate_is_plain_lcb_pinned_tail_no
order_price_val >= 0.985  # 从0.99改为0.985
```

**预期效果**：
- 通过率：1-3%
- 每天订单：10-50个
- 风险：低

### 方案3：数据驱动修复（推荐）

**分析历史最佳参数**：
```sql
-- 查找历史上edge最高的被阻止决策
SELECT
  json_extract(details_json, '$.edge_signal_lcb') as lcb,
  json_extract(details_json, '$.spread') as spread,
  json_extract(details_json, '$.q_round') as q,
  reason_codes_json
FROM auto_trade_decisions
WHERE created_at >= date('now', '-7 days')
  AND json_extract(details_json, '$.exec_edge_score') > 0.5
ORDER BY json_extract(details_json, '$.exec_edge_score') DESC
LIMIT 100;
```

**基于数据调整**：
1. 找出被错误过滤的高edge机会
2. 调整参数使这些机会能通过
3. 保持风控底线

## 立即行动建议

**最小风险方案**：
```bash
# 1. 降低LCB阈值到0
export PM_LCB_GLOBAL_EDGE_LCB_MIN=0.0

# 2. 放宽价差到10%
export PM_LCB_GLOBAL_MAX_SPREAD=0.10

# 3. 重启服务
launchctl stop com.weather.auto_trade_entry_1m
launchctl start com.weather.auto_trade_entry_1m
```

**观察指标**：
- 1小时内是否有订单生成
- 订单的edge_score分布
- 实际成交情况

## 风险控制

**保留的安全机制**：
- ✅ 价差上限（0.10-0.12）
- ✅ 市场数据新鲜度检查
- ✅ 流动性深度检查
- ✅ 事件超时保护

**移除的过度限制**：
- ❌ 过高的LCB阈值（0.0005）
- ❌ 过严的极端概率过滤（0.001/0.99）
- ❌ 多重edge检查叠加

## 后续优化

1. **回测验证**：用历史数据验证新参数
2. **A/B测试**：小仓位测试新策略
3. **动态调整**：根据市场条件自动调整阈值
4. **监控告警**：设置通过率告警（<1%触发）
