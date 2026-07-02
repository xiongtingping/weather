# 异步后台刷新实现完成

**实施时间**: 2026-04-23 10:30
**状态**: ✅ 代码已实现，等待测试

---

## 🎉 实现内容

### 1. 异步刷新架构

**核心组件**：

```python
# 刷新队列（最多 1000 个待刷新项）
_orderbook_refresh_queue = queue.Queue(maxsize=1000)

# 后台工作线程（默认 3 个）
_orderbook_refresh_threads = []

# 刷新工作函数
def _orderbook_refresh_worker():
    while not shutdown:
        token_id = queue.get()
        fresh_data = fetch_orderbook(token_id, use_cache=False)
        update_cache(token_id, fresh_data)
```

---

### 2. 工作流程

#### 2.1 决策时（不阻塞）

```
用户请求 orderbook
    ↓
检查缓存
    ↓
有缓存？
    ├─ 是 → 立即返回缓存
    │       └─ 请求异步刷新（后台）
    │
    └─ 否 → 同步获取（第一次）
            └─ 更新缓存
            └─ 请求异步刷新（后台）
```

**关键特点**：
- ✅ 决策**不等待** API 响应
- ✅ 立即返回缓存数据
- ✅ 后台持续刷新

---

#### 2.2 后台刷新（持续进行）

```
后台线程（3个）
    ↓
从队列获取 token_id
    ↓
调用 CLOB API
    ↓
成功？
    ├─ 是 → 更新缓存
    └─ 否 → 保留旧缓存
    ↓
继续处理下一个
```

**关键特点**：
- ✅ 3 个并发线程
- ✅ 失败不影响缓存
- ✅ 持续运行

---

### 3. 智能刷新策略

#### 3.1 主动刷新

```python
if cache_age > TTL * 0.7:  # 缓存年龄 > 70% TTL
    request_refresh()      # 主动刷新
```

**场景**：缓存还新鲜，但快过期了
**动作**：提前刷新，避免过期

---

#### 3.2 被动刷新

```python
if cache_age > TTL:        # 缓存已过期
    request_refresh()      # 请求刷新
    return stale_cache     # 但仍返回旧缓存
```

**场景**：缓存已过期
**动作**：返回旧缓存，后台刷新

---

#### 3.3 首次获取

```python
if no_cache:               # 没有缓存
    data = fetch_sync()    # 同步获取
    update_cache()         # 更新缓存
    request_refresh()      # 后续异步刷新
```

**场景**：第一次访问
**动作**：同步获取，后续异步

---

## 📊 配置参数

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `PM_ORDERBOOK_CACHE_ENABLED` | `1` | 是否启用缓存 |
| `PM_ORDERBOOK_CACHE_TTL_SEC` | `120` | 缓存新鲜期（2分钟）|
| `PM_ORDERBOOK_CACHE_MAX_AGE_SEC` | `3600` | 缓存最大年龄（1小时）|
| `PM_ORDERBOOK_ASYNC_REFRESH_ENABLED` | `1` | 是否启用异步刷新 |
| `PM_ORDERBOOK_REFRESH_INTERVAL_SEC` | `60` | 刷新间隔（60秒）|
| `PM_ORDERBOOK_REFRESH_WORKERS` | `3` | 刷新线程数 |

---

## 🚀 预期效果

### 性能提升

| 指标 | 同步缓存 | 异步刷新 | 改善 |
|------|---------|---------|------|
| 决策阻塞时间 | 5-10秒 | <0.1秒 | **99% ↓** |
| Worker 执行时间 | 5-10分钟 | 1-2分钟 | **80% ↓** |
| 缓存数据新鲜度 | 2-5分钟 | <1分钟 | **80% ↑** |
| API 调用次数 | 100/5min | 50/5min | **50% ↓** |
| 决策吞吐量 | 100/小时 | 1000/小时 | **10x ↑** |

---

### 阻止率改善

| 阻止原因 | 同步缓存 | 异步刷新 | 改善 |
|---------|---------|---------|------|
| market_tick_stale | 32.4% | <5% | **85% ↓** |
| market_tick_missing | 12.5% | <3% | **76% ↓** |
| 总技术阻止 | 45% | <10% | **78% ↓** |
| 总阻止率 | 92.6% | <60% | **35% ↓** |

---

### 数据新鲜度

**同步缓存**：
- 缓存命中：返回 0-2 分钟的数据
- 缓存未命中：等待 5-10 秒获取新数据

**异步刷新**：
- 缓存命中：返回 0-1 分钟的数据（后台持续刷新）
- 缓存未命中：第一次等待，后续立即返回

**结论**：异步刷新的数据**更新鲜**且**不阻塞**。

---

## 🔧 工作原理详解

### 场景 1: 首次访问（冷启动）

```
T=0: 请求 token_A
  → 缓存未命中
  → 同步获取（等待 5 秒）
  → 更新缓存
  → 请求异步刷新
  → 返回数据

T=60: 后台刷新 token_A
  → 获取新数据
  → 更新缓存

T=120: 再次请求 token_A
  → 缓存命中（新鲜）
  → 立即返回（<0.1秒）
  → 请求异步刷新
```

**首次**：等待 5 秒
**后续**：立即返回（<0.1秒）

---

### 场景 2: 持续访问（热数据）

```
T=0: 请求 token_B
  → 缓存命中（1分钟前）
  → 立即返回
  → 请求异步刷新

T=60: 后台刷新 token_B
  → 获取新数据
  → 更新缓存

T=120: 请求 token_B
  → 缓存命中（新鲜）
  → 立即返回
  → 请求异步刷新

T=180: 后台刷新 token_B
  → 持续更新...
```

**所有请求**：立即返回（<0.1秒）
**数据新鲜度**：<1分钟

---

### 场景 3: CLOB API 故障

```
T=0: 请求 token_C
  → 缓存命中（5分钟前）
  → 立即返回旧缓存
  → 请求异步刷新

T=60: 后台刷新 token_C
  → CLOB API 超时
  → 保留旧缓存（不更新）

T=120: 请求 token_C
  → 缓存命中（旧数据）
  → 立即返回
  → 请求异步刷新

T=180: 后台刷新 token_C
  → CLOB API 恢复
  → 更新缓存
```

**决策**：不受 API 故障影响
**降级**：使用旧缓存（最多 1 小时）

---

## ✅ 优势总结

### vs 无缓存

| 方面 | 无缓存 | 异步刷新 |
|------|--------|---------|
| 决策速度 | 5-10秒 | <0.1秒 |
| API 调用 | 每次都调用 | 后台调用 |
| 故障容忍 | 立即失败 | 使用旧缓存 |
| 吞吐量 | 低 | 高 |

---

### vs 同步缓存

| 方面 | 同步缓存 | 异步刷新 |
|------|---------|---------|
| 决策速度 | 缓存命中快，未命中慢 | 始终快 |
| 数据新鲜度 | 0-5分钟 | 0-1分钟 |
| 阻塞 | 缓存未命中时阻塞 | 从不阻塞 |
| 复杂度 | 简单 | 中等 |

---

### vs Redis 缓存

| 方面 | Redis | 异步刷新 |
|------|-------|---------|
| 部署 | 需要 Redis 服务 | 无需额外服务 |
| 性能 | 非常快 | 快 |
| 共享 | 多进程共享 | 单进程 |
| 复杂度 | 高 | 中等 |

---

## 🧪 测试步骤

### 1. 重启服务

```bash
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

---

### 2. 观察 10 分钟

**检查指标**：

```bash
# 检查决策数量和阻止率
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) as blocked,
  SUM(CASE WHEN will_order = 1 THEN 1 ELSE 0 END) as will_order,
  ROUND(100.0 * SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) / COUNT(*), 2) as blocked_pct
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes');
"

# 检查技术阻止原因
sqlite3 data/archive.db "
SELECT
  reason_codes_json,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND (reason_codes_json LIKE '%market_tick%')
GROUP BY reason_codes_json
ORDER BY count DESC;
"
```

---

### 3. 预期结果

**成功指标**：
- ✅ market_tick_stale < 5%（从 32.4% 大幅下降）
- ✅ market_tick_missing < 3%（从 12.5% 大幅下降）
- ✅ 总阻止率 < 60%（从 92.6% 下降）
- ✅ **出现 will_order=1 的可交易订单**

**性能指标**：
- ✅ Worker 执行时间 < 3 分钟（从 10+ 分钟下降）
- ✅ 决策吞吐量 > 500/小时（从 100/小时提升）

---

## 🐛 故障排查

### 问题 1: 后台线程未启动

**症状**：效果与同步缓存相同

**检查**：
```bash
ps aux | grep "auto-trade-job-worker" | head -1 | awk '{print $2}' | xargs lsof -p 2>/dev/null | grep -c "Python"
```

**解决**：
- 检查 `PM_ORDERBOOK_ASYNC_REFRESH_ENABLED=1`
- 重启服务

---

### 问题 2: 队列满

**症状**：日志中出现 "Queue is full"

**原因**：刷新请求太多，线程处理不过来

**解决**：
```bash
# 增加工作线程
echo "PM_ORDERBOOK_REFRESH_WORKERS=5" >> .env
```

---

### 问题 3: 内存占用高

**症状**：系统内存不足

**原因**：缓存太多数据

**解决**：
```bash
# 减少缓存时间
echo "PM_ORDERBOOK_CACHE_MAX_AGE_SEC=1800" >> .env  # 30 分钟
```

---

## 📋 下一步

### 立即执行

1. **重启服务**（应用异步刷新）
2. **观察 10 分钟**
3. **检查效果**

### 如果成功

- ✅ 保持当前配置
- ✅ 监控长期表现
- ✅ 考虑增加刷新线程（如果需要）

### 如果失败

- 检查后台线程是否启动
- 检查队列是否满
- 回退到同步缓存

---

## 🎯 成功标准

### 最低标准（必须达到）

- ✅ 后台线程正常启动
- ✅ 缓存正常工作
- ✅ 决策不阻塞

### 理想标准（期望达到）

- ✅ market_tick_stale < 10%
- ✅ 总阻止率 < 65%
- ✅ 出现可交易订单

### 完美标准（最佳情况）

- ✅ market_tick_stale < 5%
- ✅ 总阻止率 < 60%
- ✅ 有实际交易执行
- ✅ Worker 执行时间 < 3 分钟

---

**状态**: 异步刷新已实现，等待重启测试
