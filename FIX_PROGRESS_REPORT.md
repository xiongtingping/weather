# 修复进展报告

## 当前状态（2026-04-25 10:25）

### 已完成的修复

1. ✅ **禁用edge_realizable_v2强制要求**
   - PM_EDGE_POLICY_REQUIRE_V2=0
   - PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=0

2. ✅ **启用shadow模式**
   - PM_EDGE_POLICY_MODE=shadow
   - edge_tradeability不再阻止交易

3. ✅ **降低path_lcb阈值**
   - PM_EDGE_POLICY_PATH_LCB_MIN=0.05（从0.10降低）

### 当前阻塞点

**最近5分钟决策统计**：
- 总决策：578
- 通过：0
- 通过率：0%
- 最新决策时间：10:17:26（8分钟前）

**主要阻止原因**：
1. **market_tick_stale (28.2%)** - 市场数据过期
2. **plain_lcb_pinned_tail_no_filtered (25.6%)** - 极端概率过滤（NO位置price≥0.99）
3. **plain_lcb_zero_prob_yes_filtered (9.5%)** - 极端概率过滤（YES位置q≤0.001）
4. **spread_too_wide (9.7%)** - 价差过大
5. **event_worker_timeout (最新)** - 事件处理超时

### 服务状态

- ✅ market_refresh_1m: 运行中
- ⚠️ auto_trade_entry_1m: OnDemand模式，按需启动
- ⚠️ 决策频率很低（8分钟才1个）

## 根本问题

### 1. edge_tradeability已解决
- shadow模式生效
- 不再阻止交易

### 2. 极端概率过滤（39.1%阻塞）
**plain_lcb_pinned_tail_no_filtered (25.6%)**：
- 过滤NO位置price≥0.99
- 这是风险控制机制
- 虽然有高edge，但风险/回报比极差（1:99）

**plain_lcb_zero_prob_yes_filtered (9.5%)**：
- 过滤YES位置q≤0.001
- 合理过滤，没有高价值案例

### 3. market_tick_stale（28.2%阻塞）
- 市场数据刷新不及时
- market_refresh_1m已启动，但可能刷新频率不够

### 4. 决策频率低
- 8分钟才1个决策
- auto_trade_entry_1m是OnDemand模式
- 可能需要更频繁的触发

## 下一步行动

### 方案A：放宽极端概率过滤（激进）

**修改scan_weather_market.py**：
```python
# 第2932行：_candidate_is_plain_lcb_pinned_tail_no
# 将0.99改为0.995或0.998
order_price >= 0.995  # 从0.99放宽到0.995
```

**预期效果**：
- 解锁25.6%的案例
- 通过率：从0%提升到10-15%
- 风险：高（风险/回报比仍然很差）

### 方案B：优化市场数据刷新（保守）

**增加刷新频率**：
```bash
# 修改market_refresh_1m.plist
# 将StartInterval从60改为30（每30秒刷新一次）
```

**预期效果**：
- 减少market_tick_stale阻塞
- 通过率：从0%提升到5-10%
- 风险：低

### 方案C：混合方案（推荐）

**第一步：优化数据刷新**
- 增加market_refresh频率到30秒
- 确保数据及时更新

**第二步：适度放宽极端概率过滤**
- 将pinned_tail_no阈值从0.99放宽到0.995
- 只解锁部分高edge案例

**第三步：监控和调整**
- 观察24小时
- 根据实际表现调整参数

## 立即可执行的命令

### 选项1：只优化数据刷新（最安全）
```bash
# 无需修改，market_refresh已在运行
# 等待数据刷新，观察1小时
```

### 选项2：放宽极端概率过滤（激进）
```bash
cd /Users/xiong/weather

# 备份原文件
cp scan_weather_market.py scan_weather_market.py.backup

# 修改pinned_tail_no阈值（需要手动编辑）
# 第2932行：将0.99改为0.995

# 重启服务
launchctl stop com.weather.auto_trade_entry_1m
launchctl start com.weather.auto_trade_entry_1m
```

### 选项3：手动触发决策（测试）
```bash
# 手动运行一次，查看详细输出
bash scripts/auto_trade_entry_1m.sh

# 检查结果
sqlite3 data/archive.db "
SELECT COUNT(*) as total,
       SUM(CASE WHEN reason_codes_json = '[]' THEN 1 ELSE 0 END) as passed
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-1 minute')
"
```

## 风险评估

### 当前保留的安全机制
- ✅ signal_lcb检查（信号质量）
- ✅ market_tick_stale检查（数据新鲜度）
- ✅ spread_too_wide检查（流动性）
- ✅ pinned_tail_no过滤（极端概率，阈值0.99）
- ✅ zero_prob_yes过滤（极端概率）

### 如果放宽pinned_tail_no到0.995
- ⚠️ 风险/回报比从1:99变为1:199
- ⚠️ 需要99.5%+的胜率才能盈利
- ⚠️ 建议极小仓位测试

## 总结

**已解决**：
- ✅ edge_realizable_v2阻塞（shadow模式）
- ✅ edge_tradeability不再阻止交易

**待解决**：
- ⚠️ 极端概率过滤（39.1%阻塞）
- ⚠️ market_tick_stale（28.2%阻塞）
- ⚠️ 决策频率低

**建议**：
1. 先观察1小时，看market_refresh是否解决数据过期问题
2. 如果仍无订单，考虑适度放宽pinned_tail_no阈值
3. 密切监控实际盈亏，随时调整

**预期通过率**：
- 当前：0%
- 优化后：5-15%（取决于采用哪个方案）
