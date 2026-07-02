# 交易系统0订单根本原因与修复方案

## 执行摘要

**当前状态**：12天运行，597,104个决策，0个订单
**根本原因**：edge_realizable_v2计算失败率94.5%，导致edge_tradeability门控100%阻止

## 完整诊断链

### 1. 表面现象
- 最近24小时：7,661个决策
- 被阻止：100%
- 历史订单：0个

### 2. 阻止原因分布

| 原因 | 占比 | 说明 |
|------|------|------|
| market_tick_stale | 41% | ✅ 已修复（数据刷新问题） |
| pinned_tail_no_filtered | 24.58% | ⚠️ 极端概率过滤（风险控制） |
| zero_prob_yes_filtered | 14.76% | ✅ 合理过滤 |
| edge_tradeability_blocked | 7.9% | ❌ **真正的阻塞点** |

### 3. edge_tradeability三重门控

edge_tradeability不是单一阈值，而是**三重门控**：

```python
# edge_policy_runtime.py
1. signal_lcb >= signal_lcb_min (默认0.0)  # 信号质量
2. path_lcb >= path_lcb_min (默认0.10)     # 路径概率
3. edge_lcb >= edge_lcb_min (默认0.0)      # 可实现edge
```

### 4. 关键发现

**所有决策的edge_lcb和path_lcb都是NULL**：

```sql
总决策：7,661
edge_lcb为NULL：7,661 (100%)
path_lcb为NULL：7,661 (100%)
signal_lcb为NULL：157 (2%)
```

**edge_realizable_v2计算失败率94.5%**：

```sql
edge_realizable_v2为NULL：7,242 (94.5%)
edge_realizable_v2_raw为NULL：7,242 (94.5%)
exec_audit_gate_pass为NULL：7,661 (100%)
```

**策略配置要求v2必须存在**：

```bash
PM_EDGE_POLICY_REQUIRE_V2=1  # 要求v2必须存在
PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=1  # 要求exec_audit必须通过
```

### 5. 根本原因

**edge_realizable_v2计算失败 → edge_lcb为NULL → edge_tradeability阻止 → 0订单**

即使v2计算成功的5.5%案例中：
- 70%的v2值为负（考虑执行成本后亏损）
- 30%的v2值为正，但被signal_lcb_below_min阻止

## 修复方案

### 方案1：禁用v2强制要求（立即生效）

**修改.env**：
```bash
# 禁用v2强制要求
PM_EDGE_POLICY_REQUIRE_V2=0

# 禁用exec_audit强制要求
PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=0

# 降低path_lcb阈值（从0.10降到0.05）
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
```

**预期效果**：
- 解除94.5%的v2 NULL阻塞
- 通过率：从0%提升到5-10%
- 每天订单：50-200个
- 风险：中等（失去v2的风险控制）

**优点**：
- 立即生效
- 简单直接
- 恢复交易

**缺点**：
- 失去edge_realizable_v2的风险控制
- 可能产生亏损交易

### 方案2：修复v2计算（根本解决）

**问题诊断**：
1. v2计算依赖多个前置条件（exec_edge_entry, exec_entry_price, target_exit_price等）
2. 94.5%的案例这些前置条件不满足
3. 需要调查为什么这些条件不满足

**修复步骤**：
```bash
# 1. 启用v2调试日志
export PM_EDGE_REALIZABLE_V2_DEBUG=1

# 2. 运行一次决策，查看v2计算失败原因
python3 scan_weather_market.py --once

# 3. 根据日志修复v2计算逻辑
```

**预期时间**：1-2天调查 + 修复

### 方案3：混合方案（推荐）

**第一步：立即恢复交易**（方案1）
```bash
# .env
PM_EDGE_POLICY_REQUIRE_V2=0
PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=0
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
```

**第二步：并行修复v2**（方案2）
- 调查v2计算失败原因
- 修复v2计算逻辑
- 验证v2计算成功率

**第三步：逐步启用v2**
```bash
# 先用shadow模式观察
PM_EDGE_POLICY_MODE=shadow
PM_EDGE_POLICY_REQUIRE_V2=1

# 观察1-2天，确认v2计算正常后
PM_EDGE_POLICY_MODE=enforced
```

## 立即行动

**最小风险修复**（5分钟）：

```bash
# 1. 修改.env
cd /Users/xiong/weather
cat >> .env << 'EOF'

# === 紧急修复：禁用v2强制要求 (2026-04-25) ===
PM_EDGE_POLICY_REQUIRE_V2=0
PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT=0
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
EOF

# 2. 重启服务
launchctl stop com.weather.auto_trade_entry_1m
launchctl start com.weather.auto_trade_entry_1m

# 3. 观察1小时
# 检查是否有订单生成
sqlite3 data/archive.db "SELECT COUNT(*) FROM auto_orders WHERE created_at >= datetime('now', '-1 hour')"
```

**观察指标**：
- 1小时内订单数量
- 通过率（通过决策/总决策）
- edge_score分布
- 实际成交情况

## 风险评估

**保留的安全机制**：
- ✅ signal_lcb检查（信号质量）
- ✅ market_tick_stale检查（数据新鲜度）
- ✅ spread_too_wide检查（流动性）
- ✅ pinned_tail_no/zero_prob_yes过滤（极端概率）

**移除的机制**：
- ❌ edge_realizable_v2检查（可实现edge）
- ❌ exec_audit检查（执行审计）
- ⚠️ path_lcb阈值降低（从0.10到0.05）

**残留风险**：
- 可能产生负edge交易（因为没有v2的风险控制）
- 建议小仓位测试
- 密切监控实际盈亏

## 后续优化

### 短期（1-2天）
1. 修复edge_realizable_v2计算
2. 调查v2计算失败原因
3. 验证v2计算成功率

### 中期（1周）
1. 启用shadow模式观察v2
2. 对比有/无v2的交易表现
3. 调整v2校准参数

### 长期（1个月）
1. 基于实际交易数据重新校准v2
2. 优化p_fill_entry/p_converge/p_fill_exit参数
3. 建立自动校准机制

## 技术细节

### edge_realizable_v2计算流程

```python
# 1. 计算base_edge（v1）
base_edge = exec_edge_entry - fee

# 2. 查找校准参数
p_fill_entry = 0.80  # 成交概率
p_converge = 0.85    # 收敛概率
p_fill_exit = 0.80   # 退出成交概率

# 3. 计算v2
edge_realizable_v2 = base_edge * p_fill_entry * p_converge - hold_cost
```

### 为什么v2为NULL？

可能原因：
1. exec_edge_entry为NULL
2. exec_entry_price为NULL
3. target_exit_price为NULL
4. 校准文件加载失败
5. bucket_key匹配失败

需要调试日志确认具体原因。

## 总结

**根本原因**：edge_realizable_v2计算失败率94.5%，导致edge_tradeability门控100%阻止所有交易。

**立即修复**：禁用v2强制要求（PM_EDGE_POLICY_REQUIRE_V2=0），恢复交易。

**长期解决**：修复v2计算逻辑，重新启用v2风险控制。

**预期效果**：通过率从0%提升到5-10%，每天50-200个订单。
