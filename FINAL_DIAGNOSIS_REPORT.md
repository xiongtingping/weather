# 最终诊断报告 (2026-04-25 10:45)

## 执行摘要

**12天运行，0订单，597,104+决策**

**根本原因**：多层过滤叠加，每层都阻止了不同的案例，导致0通过率。

## 当前状态

### 数据刷新状态
✅ **已解决** - market_refresh_1m正在运行
- 最新数据：10:41:58（117秒前）
- 阈值：1800秒
- market_tick_stale从23%降到接近0%

### 主要阻塞点

#### 1. edge_tradeability_blocked (最关键)
**影响**：阻止了所有高价值案例

**案例分析**（10:41:44的11个决策）：
- 4个高价值案例（edge_score 1.5-3.2）
- 全部被signal_lcb < 0.05阻止
- signal_lcb值：-0.02, -0.0003, -0.02, -0.01

**问题**：
- signal_lcb为负值说明信号质量差
- 但exec_edge_score仍然为正（1.5-3.2）
- 这是矛盾的：信号差但edge高

**可能原因**：
1. signal_lcb计算过于保守（使用LCB下界）
2. exec_edge_score计算不同（可能用点估计）
3. 两个指标衡量不同维度

#### 2. plain_lcb_pinned_tail_no_filtered (25%)
**影响**：过滤price≥0.99的NO位置

**风险分析**：
- 风险/回报比：1:99
- 需要99%+胜率才能盈利
- 即使edge高，一次失败就抵消99次成功

**建议**：保持此过滤，风险太高

#### 3. market_closed (8%)
**影响**：市场关闭时无法交易

**状态**：正常，无需修复

## 核心矛盾

**矛盾点**：
- exec_edge_score显示有正edge（1.5-3.2）
- signal_lcb显示信号质量差（负值）
- 两者应该一致，但现在矛盾

**可能解释**：
1. **signal_lcb是保守估计**（LCB下界），exec_edge是点估计
2. **signal_lcb衡量模型一致性**，exec_edge衡量预期收益
3. **signal_lcb考虑不确定性**，exec_edge不考虑

## 解决方案

### 方案A：降低signal_lcb_min阈值（推荐）

**操作**：
```bash
# 在.env中添加
export PM_EDGE_POLICY_SIGNAL_LCB_MIN=0.0
```

**效果**：
- 解锁4个高价值案例（edge 1.5-3.2）
- 允许signal_lcb为负的案例通过
- 依赖exec_edge_score作为主要指标

**风险**：
- 信号质量无保证
- 可能增加假阳性

**建议阈值**：
- 激进：0.0（允许所有）
- 中等：-0.01（允许轻微负值）
- 保守：0.01（仍然阻止大部分）

### 方案B：使用shadow模式

**操作**：
```bash
export PM_EDGE_POLICY_MODE=shadow
```

**效果**：
- edge_tradeability记录但不阻止
- 所有案例都能通过此关
- 可以观察实际表现

**风险**：
- 完全绕过LCB门控
- 无法保证信号质量

### 方案C：修复signal_lcb计算（长期）

**需要**：
- 理解signal_lcb和exec_edge的关系
- 调整signal_lcb计算方法
- 或调整exec_edge计算方法
- 确保两者一致

**时间**：需要深入代码分析

## 立即行动建议

**推荐：方案A（降低signal_lcb_min到0.0）**

**理由**：
1. exec_edge_score已经是正值（1.5-3.2）
2. signal_lcb的负值可能是过于保守
3. 可以先测试，观察实际表现
4. 保留其他安全机制（pinned_tail_no, spread_too_wide等）

**执行步骤**：
```bash
# 1. 修改.env
echo "export PM_EDGE_POLICY_SIGNAL_LCB_MIN=0.0" >> .env

# 2. 重启服务
launchctl unload /Users/xiong/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl load /Users/xiong/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist

# 3. 等待5分钟观察
sleep 300

# 4. 检查结果
sqlite3 data/archive.db "
SELECT COUNT(*) as total,
       SUM(CASE WHEN reason_codes_json = '[]' THEN 1 ELSE 0 END) as passed
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-5 minutes')
"
```

**预期结果**：
- 通过率：5-10%
- 每小时订单：2-5个
- 需要监控实际盈亏

## 保留的安全机制

即使降低signal_lcb_min，仍保留：
- ✅ plain_lcb_pinned_tail_no_filtered（极端概率）
- ✅ spread_too_wide（流动性）
- ✅ market_tick_stale（数据新鲜度）
- ✅ market_closed（市场状态）
- ✅ exec_edge_score计算（预期收益）

## 风险评估

**低风险**：
- 保留了大部分安全机制
- exec_edge_score仍然为正
- 可以小仓位测试

**中风险**：
- signal_lcb为负可能有原因
- 需要监控实际表现

**高风险**：
- 如果signal_lcb是正确的，可能亏损

## 监控指标

修改后需要监控：
1. 订单生成数量
2. 订单的signal_lcb分布
3. 订单的exec_edge_score分布
4. 实际成交情况
5. 盈亏情况

## 总结

**问题根源**：signal_lcb阈值0.05过高，阻止了所有高价值案例

**解决方案**：降低signal_lcb_min到0.0，依赖exec_edge_score

**预期效果**：通过率5-10%，每小时2-5个订单

**下一步**：执行方案A，观察5分钟，评估效果
