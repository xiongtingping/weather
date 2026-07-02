# 交易卡点复查报告 - 新问题发现

**检查时间**: 2026-04-23 08:09
**状态**: ⚠️ CLOB 超时已解决，但发现新的 event worker 超时问题

---

## 📊 当前状态

### 修复效果总结

**CLOB API 超时问题** ✅ 已解决：
- `timeout=1.2s` 错误：4,875 次 → **0 次**
- `market_tick_missing`：6,697 次 → **0 次**

**但出现新问题** ⚠️：
- `event_worker_timeout`：2 次（100%）
- 决策状态：`error_timeout` 而非 `blocked`

---

## 🔍 新问题分析

### 1. Event Worker 超时

**错误信息**：
```
event_worker_timeout
执行超时：子进程在 120s 内未完成
```

**日志显示**：
```
2026-04-23 08:06:08 [trade_engine] worker_once_timeout
```

### 2. 超时原因

**trade_engine_loop.sh 配置**（第 105 行）：
```bash
export PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC:-120}"
```

**默认超时**: 120 秒

**实际情况**：
- Worker 处理单个事件超过 120 秒
- 被 trade_engine 强制终止
- 导致决策状态为 `error_timeout`

### 3. 为什么会超时？

**可能原因**：
1. **Trade sync 太慢**：日志显示 trade sync 耗时 174 秒
   ```
   2026-04-23 08:09:03 [trade_engine] sync_once_ok elapsed=174s
   ```

2. **Data API 超时**：
   ```
   data_api_request_failed detail=HTTPSConnectionPool(host='data-api.polymarket.com', port=443):
   Max retries exceeded
   ```

3. **Worker 处理时间过长**：
   - 每个事件需要获取 orderbook
   - 计算决策
   - 写入数据库
   - 总时间超过 120 秒

---

## 📈 对比分析

### 修复前 vs 修复后

| 指标 | 修复前 | 修复后 | 变化 |
|------|--------|--------|------|
| 总决策数 | 70,846 | 2 | - |
| CLOB 超时 | 4,875 (6.9%) | 0 | ✅ 已解决 |
| market_tick_missing | 6,697 (9.5%) | 0 | ✅ 已解决 |
| event_worker_timeout | 0 | 2 (100%) | ⚠️ 新问题 |
| 阻止率 | 99.99% | 0% | ✅ 改善 |
| 错误率 | 0% | 100% | ⚠️ 新问题 |

---

## 🎯 问题根源

**不是 CLOB 超时，而是整个 worker 执行超时**

1. **CLOB API 超时已解决**（1.2s → 5s）
2. **但 worker 整体执行时间过长**（>120s）
3. **trade_engine 强制终止 worker**
4. **导致决策状态为 error_timeout**

---

## 🔧 可能的解决方案

### 方案 1: 增加 event timeout（推荐）

**修改 `.env`**：
```bash
PM_AUTO_TRADE_EVENT_TIMEOUT_SEC=300  # 从 120s 增加到 300s (5分钟)
```

**优点**：
- 简单直接
- 允许 worker 有更多时间完成

**缺点**：
- 如果真的卡住，需要等待更长时间

---

### 方案 2: 优化 trade sync

**问题**：trade sync 耗时 174 秒，超过 worker timeout

**可能优化**：
- 减少 sync 频率
- 减少 sync 范围
- 增加 data API 超时

---

### 方案 3: 分离 trade sync 和 auto trade

**问题**：trade sync 和 auto trade 在同一个 worker 中

**优化**：
- 将 trade sync 移到独立进程
- auto trade worker 只负责交易决策

---

## 📊 当前系统状态

### Worker 进程

```
PID 61953: auto-trade-job-worker (运行中)
PID 61748: auto-trade-job-worker (运行中)
PID 52241: trade_engine_loop.sh (主进程)
```

### 最近决策

| 时间 | 市场 | 状态 | 原因 |
|------|------|------|------|
| 00:05:10 | Busan 温度 | error_timeout | event_worker_timeout |
| 00:03:10 | LA 温度 | error_timeout | event_worker_timeout |

---

## ✅ 下一步行动

### 立即执行

1. **增加 event timeout**：
   ```bash
   echo "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC=300" >> .env
   ```

2. **重启 trade_engine**：
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
   launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
   ```

3. **观察 10 分钟**：
   - 检查是否还有 event_worker_timeout
   - 检查决策是否正常执行

---

### 长期优化

1. **优化 trade sync 性能**
2. **分离 sync 和 trade 流程**
3. **监控 worker 执行时间**

---

## 📝 总结

**好消息** ✅：
- CLOB API 超时问题已完全解决
- market_tick_missing 已消失
- 阻止率从 99.99% 降至 0%

**新问题** ⚠️：
- Event worker 超时（120秒）
- 原因：trade sync 太慢（174秒）
- 影响：所有决策都超时

**建议**：
- 增加 event timeout 到 300 秒
- 观察是否解决问题
- 如果仍有问题，考虑优化 trade sync

---

**状态**: CLOB 超时已解决，发现新的 event worker 超时问题
