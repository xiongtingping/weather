# Model Spread权重提高 - 状态检查报告

## 检查时间
2026-04-25 12:07 (本地时间)

## 修改回顾
- **修改时间**: 2026-04-25 04:00 UTC
- **修改内容**: edge_signal_runtime.py Line 68
- **修改前**: `signal_sigma += max(0.0, model_spread_f) * 0.40`
- **修改后**: `signal_sigma += max(0.0, model_spread_f) * 0.60`

## 当前运行状态

### 系统状态
- ✅ auto_trade_entry_1m服务运行中
- ✅ trade_engine服务运行中
- ✅ task_queue_worker服务运行中
- ✅ 代码修改已生效（服务已重启）

### 最近10分钟数据

#### 决策统计
- **总决策数**: 1,467
- **将要下单**: 0
- **订单生成**: 0

#### 主要阻止原因
1. **market_tick_stale**: 412 (28.1%) - 市场数据过期
2. **plain_lcb_pinned_tail_no_filtered**: 364 (24.8%) - 市场结构问题
3. **market_closed**: 246 (16.8%) - 市场关闭
4. **signal_lcb_below_min**: 86 (5.9%) - Signal LCB低于阈值
5. **plain_lcb_zero_prob_yes_filtered**: 76 (5.2%)
6. **spread_too_wide**: 64 (4.4%)
7. **edge_lcb_below_min**: 51 (3.5%)

## 问题分析

### 1. 修改未产生预期效果
**现象**:
- 仍然0订单
- signal_lcb_below_min仍然阻止86个案例（与修改前相同）
- 高价值案例（exec_edge > 5）仍然被阻止

**可能原因**:
1. **Signal_alpha本身过低**
   - 即使signal_sigma降低，如果signal_alpha接近0或为负，signal_lcb仍然为负
   - Model_spread权重提高只能减少不确定性，无法提高期望值

2. **其他门控更严格**
   - market_tick_stale (412个) - 数据新鲜度问题
   - plain_lcb_pinned_tail_no_filtered (364个) - 市场结构问题
   - market_closed (246个) - 市场关闭

3. **Signal_lcb_min阈值仍然过高**
   - 当前阈值: 0.0
   - 但如果signal_lcb为负值，仍然无法通过

### 2. 数据质量问题
**market_tick_stale占比28.1%**:
- 市场数据过期是最大的阻止原因
- 说明数据采集或刷新存在问题
- 即使signal_lcb提高，数据过期也会阻止交易

### 3. 市场结构问题
**plain_lcb_pinned_tail_no_filtered占比24.8%**:
- 市场结构不适合交易（尾部概率固定）
- 这是市场本身的问题，不是模型问题

## 下一步建议

### 方案A: 检查signal_alpha分布（推荐）
**目的**: 了解signal_alpha是否本身就很低

**操作**:
```sql
SELECT
    AVG(CAST(json_extract(details_json, '$.signal_alpha') AS REAL)) as avg_alpha,
    AVG(CAST(json_extract(details_json, '$.signal_sigma') AS REAL)) as avg_sigma,
    AVG(CAST(json_extract(details_json, '$.signal_lcb') AS REAL)) as avg_lcb,
    AVG(CAST(json_extract(details_json, '$.model_spread') AS REAL)) as avg_spread
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-1 hour')
  AND json_extract(details_json, '$.signal_alpha') IS NOT NULL
```

### 方案B: 解决数据新鲜度问题（高优先级）
**问题**: market_tick_stale占比28.1%

**可能原因**:
1. market_refresh_1m服务频率不够
2. 数据采集超时
3. 网络问题

**操作**:
```bash
# 检查market_refresh服务状态
launchctl list | grep market_refresh

# 查看market_refresh日志
tail -100 /Users/xiong/weather/data/logs/market_refresh_1m.log
```

### 方案C: 降低signal_lcb_min到负值（激进）
**当前**: PM_LCB_GLOBAL_SIGNAL_LCB_MIN=0.0
**建议**: PM_LCB_GLOBAL_SIGNAL_LCB_MIN=-0.05

**预期效果**:
- 允许signal_lcb在[-0.05, 0)范围的案例通过
- 可能解锁部分高价值案例

**风险**: 中等（允许负LCB案例）

### 方案D: 检查scenario模式是否仍然生效
**验证**:
```bash
# 检查环境变量
grep PM_SCAN_WEIGHTING /Users/xiong/weather/.env

# 检查最近的model_spread分布
sqlite3 data/archive.db "SELECT
    CASE
        WHEN CAST(json_extract(details_json, '$.model_spread') AS REAL) < 0.05 THEN '< 0.05'
        WHEN CAST(json_extract(details_json, '$.model_spread') AS REAL) < 0.10 THEN '0.05-0.10'
        WHEN CAST(json_extract(details_json, '$.model_spread') AS REAL) < 0.15 THEN '0.10-0.15'
        ELSE '>= 0.15'
    END as range,
    COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-1 hour')
  AND json_extract(details_json, '$.model_spread') IS NOT NULL
GROUP BY range"
```

## 总结

**修改已生效但未产生预期效果**:
- ✅ 代码修改正确
- ✅ 服务已重启
- ❌ 仍然0订单
- ❌ signal_lcb_below_min仍然阻止86个案例

**根本原因**:
1. **数据新鲜度问题**（28.1%案例）- 最大阻碍
2. **市场结构问题**（24.8%案例）- 无法解决
3. **Signal_alpha可能本身就很低** - 需要验证
4. **其他门控仍然严格** - 需要逐个分析

**建议优先级**:
1. **立即**: 检查signal_alpha分布（方案A）
2. **高优先级**: 解决数据新鲜度问题（方案B）
3. **中优先级**: 验证scenario模式是否生效（方案D）
4. **低优先级**: 考虑降低signal_lcb_min到负值（方案C）

**结论**:
Model_spread权重提高是正确的方向，但不是唯一的瓶颈。需要解决数据新鲜度问题和验证signal_alpha分布，才能看到实际效果。
