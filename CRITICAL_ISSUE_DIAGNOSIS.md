# 关键问题诊断报告

## 检查时间
2026-04-25 12:13 (本地时间)

## 核心问题

### 问题1: Signal指标完全缺失
**现象**:
- 所有决策记录的signal_alpha、signal_sigma、signal_lcb字段全部为NULL
- 1467个决策中，0个有signal指标
- 高价值案例（exec_edge > 5）也没有signal指标

**影响**:
- Model_spread权重提高的修改无法生效（因为signal计算本身就失败了）
- 无法通过signal_lcb门控（因为signal_lcb为NULL）
- 86个案例被signal_lcb_below_min阻止，但实际上是因为signal_lcb不存在

**根本原因**:
需要检查edge_signal_runtime.py的调用链：
1. 是否正确导入了edge_signal_runtime模块？
2. compute_signal_advantage函数是否被调用？
3. 输入参数（q_edge, trend_pct, model_spread等）是否存在？

### 问题2: 市场数据严重过期
**现象**:
- 412个案例（28.1%）被market_tick_stale阻止
- 29个独特市场的数据过期
- 例如：Amsterdam市场快照时间为2026-04-25T02:32:20，距离决策时间97.4分钟

**已采取措施**:
- ✅ 重启market_refresh_1m服务
- ✅ 确认新快照正在生成（London 04:10:48, Amsterdam 04:10:20）

**预期效果**:
- 下一轮决策时，market_tick_stale应该显著减少

### 问题3: 其他门控仍然严格
**阻止原因分布**（最近5分钟）:
1. market_tick_stale: 412 (27.4%)
2. plain_lcb_pinned_tail_no_filtered: 373 (24.8%)
3. market_closed: 246 (16.3%)
4. signal_lcb_below_min: 99 (6.6%)
5. plain_lcb_zero_prob_yes_filtered: 76 (5.0%)

## 诊断步骤

### 步骤1: 检查signal计算是否被调用
```python
# 在scan_weather_market.py中搜索compute_signal_advantage或enrich_signal_observability的调用
grep -n "compute_signal_advantage\|enrich_signal_observability" scan_weather_market.py
```

### 步骤2: 检查signal输入参数
```sql
-- 检查q_edge等基础参数是否存在
SELECT
    COUNT(*) as total,
    COUNT(CASE WHEN json_extract(details_json, '$.q_edge') IS NOT NULL THEN 1 END) as has_q_edge,
    COUNT(CASE WHEN json_extract(details_json, '$.trend_pct') IS NOT NULL THEN 1 END) as has_trend,
    COUNT(CASE WHEN json_extract(details_json, '$.model_spread') IS NOT NULL THEN 1 END) as has_spread
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
```

### 步骤3: 检查edge_signal_runtime导入
```bash
# 测试导入
python3 -c "from edge_signal_runtime import compute_signal_advantage; print('Import OK')"
```

### 步骤4: 手动测试signal计算
```python
from edge_signal_runtime import compute_signal_advantage

result = compute_signal_advantage(
    q_edge=0.1,
    trend_pct=0.02,
    model_spread=0.07,
    signal_z=1.0
)
print(result)
```

## 优先级

### P0 - 立即修复
**Signal指标缺失问题**
- 这是最严重的问题
- 导致所有signal相关的优化（包括model_spread权重提高）无法生效
- 必须先修复这个问题，才能看到其他优化的效果

### P1 - 已修复，等待验证
**市场数据过期问题**
- 已重启market_refresh_1m服务
- 新快照正在生成
- 下一轮决策应该会改善

### P2 - 后续优化
**其他门控优化**
- plain_lcb_pinned_tail_no_filtered (24.8%) - 市场结构问题
- market_closed (16.3%) - 时间问题
- 这些需要单独分析和优化

## 下一步行动

### 立即（现在）
1. 检查signal计算是否被调用
2. 检查signal输入参数是否存在
3. 测试edge_signal_runtime导入和计算

### 短期（修复后）
1. 重新运行auto_trade_job_worker
2. 验证signal指标是否出现
3. 验证model_spread权重提高是否生效

### 中期（验证后）
1. 分析signal_lcb分布
2. 评估是否需要进一步调整阈值
3. 优化其他门控

## 总结

**根本问题**: Signal指标计算完全失败，导致所有signal相关的优化无法生效。

**修改状态**: Model_spread权重提高的代码修改是正确的，但由于signal计算失败，修改无法产生效果。

**优先级**: 必须先修复signal计算问题，才能评估model_spread权重提高的效果。

**预期**: 修复signal计算后，应该能看到：
1. Signal_alpha、signal_sigma、signal_lcb字段有值
2. Signal_lcb_below_min阻止的案例数量变化
3. Model_spread权重提高的效果（signal_lcb提升50%）
