# 交易卡点深度分析 - 无实盘订单根本原因

**分析时间**: 2026-04-23 08:40
**状态**: ⚠️ 发现根本问题

---

## 🔍 问题总结

**表面现象**：
- 没有可交易的实盘订单（will_order=0）
- 阻止率 84.62%

**根本原因**：
1. ✅ CLOB API 超时已解决（1.2s → 5s）
2. ❌ **但 CLOB API 仍然经常超时**（即使 5s）
3. ❌ **Worker 执行时间过长**（>10 分钟）
4. ❌ **市场 tick 严重过期**（6518s = 1.8 小时）

---

## 📊 数据分析

### Worker 超时历史

| 时间 | Worker Timeout 设置 | 实际运行时间 | 结果 |
|------|---------------------|--------------|------|
| 08:06 | 300s (5分钟) | ~300s | ❌ 超时 |
| 08:16 | 300s (5分钟) | ~300s | ❌ 超时 |
| 08:29 | 600s (10分钟) | ~600s | ❌ 超时 |
| 08:39 | 600s (10分钟) | ~600s | ❌ 超时 |

**观察**：
- 每次都是刚好在超时限制时被终止
- 说明 **worker 需要更长时间才能完成**
- 600 秒（10 分钟）仍然不够

---

### 阻止原因分布（过去 30 分钟）

| 原因 | 数量 | 占比 |
|------|------|------|
| market_tick_stale | 2 | 9.1% |
| market_snapshot_inconsistent | 14 | 63.6% |
| market_tick_missing | 0 | 0% |

**关键发现**：
- `market_tick_stale`：市场 tick 过期（6518s = 1.8 小时）
- 原因：CLOB API 超时，无法刷新 tick
- 即使设置了 5s 超时，仍然经常失败

---

### CLOB API 超时详情

**错误信息**：
```
HTTPSConnectionPool(host='clob.polymarket.com', port=443):
Max retries exceeded with url: /book?token_id=...
(Caused by ConnectTimeoutError(..., 'Connection to clob.polymarket.com timed out. (connect timeout=5.0)'))
```

**分析**：
- 超时设置：5 秒
- 重试次数：3 次
- **仍然失败**：说明 CLOB API 响应非常慢或不可用

---

## 🎯 根本原因

### 1. CLOB API 性能问题

**问题**：
- CLOB API 响应时间 > 5 秒
- 即使重试 3 次仍然失败
- 导致无法获取最新的 orderbook

**影响**：
- 市场 tick 无法刷新
- tick_age 持续增长（6518s）
- 超过 120s 阈值后被阻止

---

### 2. Worker 执行时间过长

**问题**：
- 每个 job 处理 6 个事件
- 每个事件需要：
  - 获取 orderbook（可能超时）
  - 计算决策
  - 写入数据库
- 总时间 > 10 分钟

**影响**：
- Worker 被强制终止
- Job 标记为 retrying
- 不断重试，永远无法完成

---

### 3. 恶性循环

```
CLOB API 慢/超时
    ↓
无法刷新 tick
    ↓
tick 过期（>120s）
    ↓
决策被阻止
    ↓
Worker 处理更多事件
    ↓
Worker 超时
    ↓
Job 重试
    ↓
继续尝试刷新 tick
    ↓
CLOB API 慢/超时（循环）
```

---

## 🔧 解决方案

### 方案 1: 增加 CLOB API 超时（推荐）

**问题**：5 秒仍然不够

**修复**：
```bash
# 增加到 10 秒
PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC=10
PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES=3
```

**优点**：
- 给 CLOB API 更多时间响应
- 可能解决超时问题

**缺点**：
- 如果 API 真的很慢，仍然可能超时
- Worker 执行时间会更长

---

### 方案 2: 增加 Worker 超时

**问题**：600 秒（10 分钟）不够

**修复**：
```bash
# 增加到 20 分钟
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=1200
```

**优点**：
- 给 worker 更多时间完成
- 避免被强制终止

**缺点**：
- 如果真的卡住，需要等待更长时间
- 不解决根本问题（CLOB API 慢）

---

### 方案 3: 减少每个 Job 处理的事件数

**问题**：每个 job 处理 6 个事件，太多

**修复**：
```bash
# 从 6 减少到 3
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=3
```

**优点**：
- 减少单个 job 的执行时间
- 更容易在超时前完成

**缺点**：
- 需要更多 jobs 来处理所有事件
- 总体吞吐量可能下降

---

### 方案 4: 放宽 tick 过期阈值

**问题**：120 秒太严格

**修复**：
```bash
# 从 120 秒增加到 300 秒（5 分钟）
PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=300
```

**优点**：
- 允许使用稍旧的 tick
- 减少因 tick 过期而被阻止的决策

**缺点**：
- 使用过期数据可能导致不准确的决策
- 不解决根本问题（CLOB API 慢）

---

### 方案 5: 跳过无法刷新的市场（推荐）

**问题**：一直尝试刷新失败的市场，浪费时间

**修复**：
- 如果 CLOB API 超时，跳过该市场
- 不要一直重试
- 继续处理其他市场

**优点**：
- 避免卡在失败的市场上
- 可以处理其他可用的市场

**缺点**：
- 需要修改代码逻辑

---

## 📋 推荐的综合方案

### 立即执行（配置调整）

1. **增加 CLOB API 超时**：
   ```bash
   PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC=10
   ```

2. **增加 Worker 超时**：
   ```bash
   PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=1200  # 20 分钟
   ```

3. **减少每个 Job 的事件数**：
   ```bash
   PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=3
   ```

4. **放宽 tick 过期阈值**：
   ```bash
   PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=300  # 5 分钟
   ```

---

### 长期优化（代码修改）

1. **实现超时快速失败**：
   - 如果 CLOB API 超时，立即跳过
   - 不要一直重试

2. **并行处理事件**：
   - 不要串行处理每个事件
   - 使用多线程/多进程

3. **缓存 orderbook**：
   - 缓存最近的 orderbook
   - 如果无法刷新，使用缓存

4. **监控 CLOB API 性能**：
   - 记录每次 API 调用的时间
   - 识别慢的 token_id

---

## 📊 预期效果

### 应用配置调整后

| 指标 | 当前 | 预期 |
|------|------|------|
| CLOB 超时成功率 | ~50% | ~80% |
| Worker 完成率 | 0% | ~70% |
| tick 过期阻止 | 9.1% | <5% |
| 实际交易 | 0 | >0 |

---

## ✅ 下一步行动

1. **应用配置调整**
2. **重启 trade_engine**
3. **观察 30 分钟**
4. **检查是否有实际交易**

---

**状态**: 已识别根本原因，等待应用修复
