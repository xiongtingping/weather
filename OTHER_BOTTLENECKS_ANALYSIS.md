# Weather 交易系统 - 其他卡点深度分析

**分析时间**: 2026-04-22 23:25
**数据范围**: 最近 24 小时
**总决策数**: 201,663 条

---

## 🔴 发现的关键卡点

### P0-2: 市场数据陈旧（最严重）

**现象**：
```
market_tick_stale: 72,664 次拒绝（36.03%）
tick_age_p90=248,958s (约 69 小时！)
```

**根因分析**：
1. **市场 tick 数据严重过期**
   - P90 延迟：69 小时
   - 阈值：120 秒
   - 超标：2075 倍！

2. **市场刷新机制失效**
   - `market_refresh_1m.sh` 运行但数据未更新
   - 可能原因：
     - CLOB API 限流
     - 刷新超时
     - 数据写入失败

3. **影响链路**：
```
市场数据陈旧
  ↓
36% 的交易被拒绝
  ↓
错过大量交易机会
  ↓
系统实际交易量 < 10%
```

**影响范围**：
- **致命** - 导致 36% 的交易机会被拒绝
- 是所有卡点中影响最大的

---

### P1-4: 概率过滤（严重）

**现象**：
```
plain_lcb_filtered: 49,466 次拒绝（24.53%）
  - plain_lcb_pinned_tail_no_filtered
  - plain_lcb_zero_prob_yes_filtered
```

**根因分析**：
1. **概率模型过于保守**
   - 尾部概率被钉住（pinned tail）
   - YES 概率为零被过滤

2. **可能原因**：
   - 模型校准问题
   - 极端天气事件概率估计过低
   - 缺乏历史数据支持

**影响范围**：
- 24.53% 的交易被拒绝
- 主要影响极端温度区间

---

### P1-5: 市场数据缺失（严重）

**现象**：
```
market_tick_missing: 5,362 次拒绝（2.66%）
market_snapshot_inconsistent: 8,459 次拒绝（4.2%）
```

**根因分析**：
1. **市场数据未获取**
   - Token ID 不存在
   - API 请求失败
   - 数据未写入数据库

2. **数据一致性问题**
   - 快照与实时数据不一致
   - 可能是并发写入冲突

**影响范围**：
- 6.86% 的交易被拒绝
- 影响新上市的市场

---

### P2-4: 策略门槛阻断（中等）

**现象**：
```
edge_tradeability_blocked: 6,662 次拒绝（3.3%）
  - signal_lcb_below_min: 2,750 次
  - edge_lcb_below_min: 1,188 次
```

**根因分析**：
1. **Shadow 模式已启用**
   - 当前只记录不阻断
   - 实际影响：0%（观察模式）

2. **如果启用 enforced 模式**
   - 将额外拒绝 3.3% 的交易
   - 主要是信号和边缘下界不足

**影响范围**：
- 当前：0%（shadow 模式）
- 潜在：3.3%（如果启用）

---

### P2-5: 市场关闭（中等）

**现象**：
```
market_closed: 10,455 次拒绝（5.18%）
```

**根因分析**：
1. **正常行为** - 市场已结算或关闭
2. **可能优化**：
   - 提前检测市场关闭时间
   - 避免无效计算

**影响范围**：
- 5.18% 的拒绝（正常）
- 不需要优化

---

### P2-6: 价差过宽（中等）

**现象**：
```
spread_too_wide: 5,592 次拒绝（2.77%）
```

**根因分析**：
1. **流动性不足**
   - 买卖价差过大
   - 市场深度不够

2. **可能原因**：
   - 冷门市场
   - 交易时段流动性差

**影响范围**：
- 2.77% 的交易被拒绝
- 主要影响小市场

---

## 📊 卡点优先级排序

| 优先级 | 卡点 | 拒绝率 | 影响 | 可修复性 |
|--------|------|--------|------|----------|
| **P0** | 市场数据陈旧 | 36.03% | 致命 | 高 |
| **P1** | 概率过滤 | 24.53% | 严重 | 中 |
| **P1** | 市场数据缺失 | 6.86% | 严重 | 高 |
| **P2** | 市场关闭 | 5.18% | 正常 | 无需 |
| **P2** | 策略门槛 | 3.3% | 中等 | 高 |
| **P2** | 价差过宽 | 2.77% | 中等 | 低 |

---

## 🎯 优化方案

### 方案 1: 修复市场数据陈旧（P0 - 立即执行）

**问题根源**：
```
tick_age_p90=248,958s (69 小时)
阈值=120s
```

**解决方案**：

#### 1.1 检查市场刷新状态
```bash
# 查看刷新日志
tail -100 /Users/xiong/weather/weather/data/logs/market_refresh_1m.err.log

# 检查刷新进程
ps aux | grep market_refresh
```

#### 1.2 强制刷新市场数据
```bash
cd /Users/xiong/weather/weather

# 手动触发刷新
python scan_weather_market.py \
  --market-refresh-only \
  --market-refresh-max-outcomes 50 \
  --market-refresh-time-budget-sec 60
```

#### 1.3 调整刷新配置
```bash
# 增加刷新频率和超时
cat >> .env << 'EOF'

# === 市场刷新优化 ===
# 增加刷新超时
PM_MARKET_REFRESH_CLOB_TIMEOUT_SEC=5
PM_MARKET_REFRESH_CLOB_RETRIES=3

# 增加刷新预算
PM_MARKET_REFRESH_TIME_BUDGET_SEC=30

# 增加并发
PM_MARKET_REFRESH_MAX_OUTCOMES=30

EOF
```

#### 1.4 重启市场刷新服务
```bash
launchctl kickstart -k "gui/$(id -u)/com.weather.market_refresh_1m"
```

**预期效果**：
- tick_age_p90 从 69 小时降低到 < 5 分钟
- 拒绝率从 36% 降低到 < 5%
- **恢复 30%+ 的交易机会**

---

### 方案 2: 优化概率过滤（P1 - 本周内）

**问题**：
```
plain_lcb_pinned_tail_no_filtered: 49,460 次
plain_lcb_zero_prob_yes_filtered: 42,617 次
```

**解决方案**：

#### 2.1 调整概率阈值
```bash
# 降低过滤阈值
cat >> .env << 'EOF'

# === 概率过滤优化 ===
# 允许更低的概率
PM_PLAIN_LCB_MIN_PROB=0.001  # 从 0.01 降低到 0.001

# 放宽尾部过滤
PM_PLAIN_LCB_TAIL_FILTER_ENABLED=0

EOF
```

#### 2.2 重新校准模型
```bash
# 运行 Q 值校准
python scripts/train_q_calibrator.py --days 30
```

**预期效果**：
- 拒绝率从 24.53% 降低到 < 10%
- 恢复极端温度区间的交易机会

---

### 方案 3: 修复市场数据缺失（P1 - 本周内）

**问题**：
```
market_tick_missing: 5,362 次
market_snapshot_inconsistent: 8,459 次
```

**解决方案**：

#### 3.1 启用自动修复
```bash
cat >> .env << 'EOF'

# === 市场数据修复 ===
# 启用陈旧数据自动刷新
PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE=1

# 允许 token 重映射
PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP=1

# 预取市场 ticks
PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS=1

EOF
```

#### 3.2 增加数据验证
- 在写入前验证数据完整性
- 记录缺失数据的 token ID
- 自动重试获取

**预期效果**：
- 拒绝率从 6.86% 降低到 < 2%

---

## 📈 综合优化效果预测

| 指标 | 当前 | 优化后 | 提升 |
|------|------|--------|------|
| 市场数据陈旧拒绝 | 36.03% | < 5% | -86% |
| 概率过滤拒绝 | 24.53% | < 10% | -59% |
| 数据缺失拒绝 | 6.86% | < 2% | -71% |
| **总拒绝率** | **67.42%** | **< 20%** | **-70%** |
| **实际交易机会** | **32.58%** | **> 80%** | **+145%** |

---

## 🚨 立即行动项

### 1. 修复市场数据陈旧（最高优先级）
```bash
# 立即执行
cd /Users/xiong/weather/weather
python scan_weather_market.py --market-refresh-only
```

### 2. 检查刷新日志
```bash
tail -50 data/logs/market_refresh_1m.err.log
```

### 3. 应用配置优化
```bash
# 添加市场刷新优化配置
# 重启服务
launchctl kickstart -k "gui/$(id -u)/com.weather.market_refresh_1m"
```

---

**报告结束**
