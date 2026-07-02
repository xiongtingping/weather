# Orderbook 缓存实现完成

**实施时间**: 2026-04-23 10:10
**状态**: ✅ 代码已实现，等待测试

---

## 🎉 实现内容

### 1. 缓存层架构

**位置**: `scan_weather_market.py` 第 43219-43244 行

**核心组件**：
```python
# 全局缓存字典
_orderbook_cache: Dict[str, Tuple[Dict, float]] = {}

# 线程锁（保证并发安全）
_orderbook_cache_lock = threading.Lock()

# 配置参数（可通过环境变量调整）
_ORDERBOOK_CACHE_TTL_SEC = 300        # 5 分钟（新鲜期）
_ORDERBOOK_CACHE_MAX_AGE_SEC = 3600   # 1 小时（最大年龄）
_ORDERBOOK_CACHE_ENABLED = True       # 默认启用
```

---

### 2. 缓存逻辑

#### 2.1 缓存读取（第 43333-43345 行）

```python
if _ORDERBOOK_CACHE_ENABLED and use_cache and cache_key:
    with _orderbook_cache_lock:
        if cache_key in _orderbook_cache:
            cached_data, cached_time = _orderbook_cache[cache_key]
            cache_age = now - cached_time

            # 如果缓存新鲜（< 5 分钟），直接返回
            if cache_age < _ORDERBOOK_CACHE_TTL_SEC:
                return cached_data
```

**效果**：
- 5 分钟内的数据直接返回，不调用 API
- 大幅减少 CLOB API 调用次数

---

#### 2.2 缓存更新（第 43498-43507 行）

```python
# API 调用成功后
result = _augment_with_price_quotes(...)

if _ORDERBOOK_CACHE_ENABLED and use_cache and cache_key:
    with _orderbook_cache_lock:
        _orderbook_cache[cache_key] = (result, now)

        # 1% 概率清理过期缓存
        if random.random() < 0.01:
            _cleanup_orderbook_cache()

return result
```

**效果**：
- 成功获取数据后立即缓存
- 自动清理过期条目

---

#### 2.3 降级使用旧缓存（第 43583-43594 行）

```python
# 所有重试都失败后
if _ORDERBOOK_CACHE_ENABLED and use_cache and cache_key:
    with _orderbook_cache_lock:
        if cache_key in _orderbook_cache:
            cached_data, cached_time = _orderbook_cache[cache_key]
            cache_age = now - cached_time

            # 如果缓存未超过 1 小时，使用旧缓存
            if cache_age < _ORDERBOOK_CACHE_MAX_AGE_SEC:
                return cached_data

# 没有可用缓存，抛出异常
raise ValueError(f"orderbook_unavailable {token_id}{hint}")
```

**效果**：
- CLOB API 失败时，使用旧缓存（最多 1 小时）
- 比完全失败好得多

---

### 3. 配置参数

#### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `PM_ORDERBOOK_CACHE_ENABLED` | `1` | 是否启用缓存（1=启用，0=禁用）|
| `PM_ORDERBOOK_CACHE_TTL_SEC` | `300` | 缓存新鲜期（秒），5 分钟 |
| `PM_ORDERBOOK_CACHE_MAX_AGE_SEC` | `3600` | 缓存最大年龄（秒），1 小时 |

#### 函数参数

```python
fetch_orderbook(
    token_id: str,
    retries: int = 3,
    backoff: float = 1.0,
    *,
    timeout_sec: Optional[float] = None,
    price_augment_enabled: Optional[bool] = None,
    apply_global_rate_limit: Optional[bool] = None,
    use_cache: bool = True,  # ← 新增参数
) -> Dict:
```

**use_cache 参数**：
- `True`（默认）：使用缓存
- `False`：跳过缓存，强制调用 API

---

## 📊 预期效果

### 缓存命中率

假设：
- 每个市场每 5 分钟决策一次
- 每个市场有 10 个结果
- 系统处理 200 个市场

**无缓存**：
- API 调用次数：200 市场 × 10 结果 = 2000 次/5分钟
- 每次都调用 CLOB API

**有缓存**：
- 第一次：2000 次 API 调用（缓存未命中）
- 后续 5 分钟内：0 次 API 调用（缓存命中）
- **缓存命中率：~95%**

---

### 性能提升

| 指标 | 无缓存 | 有缓存 | 改善 |
|------|--------|--------|------|
| API 调用次数 | 2000/5min | 100/5min | **95% ↓** |
| Worker 执行时间 | 10-20 分钟 | 2-5 分钟 | **60-75% ↓** |
| 技术阻止率 | 60% | ~10% | **50% ↓** |
| 决策吞吐量 | 100/小时 | 500/小时 | **5x ↑** |

---

### 降级效果

**场景**：CLOB API 完全不可用

| 时间 | 无缓存 | 有缓存 |
|------|--------|--------|
| 0-5 分钟 | ❌ 100% 失败 | ✅ 使用新鲜缓存 |
| 5-60 分钟 | ❌ 100% 失败 | ✅ 使用旧缓存 |
| 60+ 分钟 | ❌ 100% 失败 | ❌ 缓存过期，失败 |

**结论**：即使 API 完全不可用，系统仍可运行 1 小时。

---

## 🔧 配置建议

### 默认配置（已应用）

```bash
# 无需添加，默认已启用
PM_ORDERBOOK_CACHE_ENABLED=1
PM_ORDERBOOK_CACHE_TTL_SEC=300
PM_ORDERBOOK_CACHE_MAX_AGE_SEC=3600
```

**适用场景**：
- 正常运行
- CLOB API 偶尔慢

---

### 激进配置（更长缓存）

```bash
# 增加缓存时间
PM_ORDERBOOK_CACHE_TTL_SEC=600        # 10 分钟
PM_ORDERBOOK_CACHE_MAX_AGE_SEC=7200   # 2 小时
```

**适用场景**：
- CLOB API 非常慢
- 可以接受使用较旧数据

**优点**：
- 更少 API 调用
- 更高缓存命中率

**缺点**：
- 数据可能较旧
- 决策可能不够准确

---

### 保守配置（更短缓存）

```bash
# 减少缓存时间
PM_ORDERBOOK_CACHE_TTL_SEC=120        # 2 分钟
PM_ORDERBOOK_CACHE_MAX_AGE_SEC=600    # 10 分钟
```

**适用场景**：
- CLOB API 速度正常
- 需要更实时的数据

**优点**：
- 数据更新鲜
- 决策更准确

**缺点**：
- 更多 API 调用
- 缓存命中率较低

---

### 禁用缓存

```bash
# 完全禁用缓存
PM_ORDERBOOK_CACHE_ENABLED=0
```

**适用场景**：
- 调试问题
- 测试原始性能

---

## ✅ 测试步骤

### 1. 重启服务

```bash
# 重启 trade_engine
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

---

### 2. 观察 10 分钟

**检查指标**：

```bash
# 检查决策数量
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) as blocked,
  SUM(CASE WHEN will_order = 1 THEN 1 ELSE 0 END) as will_order
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes');
"

# 检查阻止原因
sqlite3 data/archive.db "
SELECT
  reason_codes_json,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND decided_state = 'blocked'
GROUP BY reason_codes_json
ORDER BY count DESC
LIMIT 5;
"
```

---

### 3. 预期结果

**成功指标**：
- ✅ market_tick_stale 阻止 < 20%（从 42.5% 下降）
- ✅ market_tick_missing 阻止 < 10%（从 17.6% 下降）
- ✅ 总阻止率 < 70%（从 93.9% 下降）
- ✅ 出现 will_order=1 的决策

**失败指标**：
- ❌ 阻止率仍然 > 90%
- ❌ 仍然没有 will_order=1
- ❌ 出现新的错误

---

## 🐛 故障排查

### 问题 1: 缓存未生效

**症状**：阻止率没有改善

**检查**：
```bash
# 检查环境变量
ps aux | grep "auto-trade-job-worker" | grep -v grep | awk '{print $2}' | head -1 | xargs -I {} sh -c 'ps eww {} 2>/dev/null | tr " " "\n" | grep "PM_ORDERBOOK_CACHE"'
```

**解决**：
- 确认 `PM_ORDERBOOK_CACHE_ENABLED=1`
- 重启服务

---

### 问题 2: 语法错误

**症状**：Worker 无法启动

**检查**：
```bash
python3 -m py_compile scan_weather_market.py
```

**解决**：
- 检查代码修改
- 恢复备份

---

### 问题 3: 内存占用过高

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

1. **重启服务**（应用缓存代码）
2. **观察 10 分钟**
3. **检查效果**

### 如果成功

- ✅ 保持当前配置
- ✅ 监控长期表现
- ✅ 考虑实施异步刷新（进一步优化）

### 如果失败

- 尝试增加 tick_stale 阈值
- 考虑实施 Redis 缓存
- 检查网络问题

---

## 🎯 成功标准

### 最低标准（必须达到）

- ✅ 代码无语法错误
- ✅ Worker 正常启动
- ✅ 缓存正常工作

### 理想标准（期望达到）

- ✅ market_tick_stale < 20%
- ✅ 总阻止率 < 70%
- ✅ 出现可交易订单

### 完美标准（最佳情况）

- ✅ market_tick_stale < 10%
- ✅ 总阻止率 < 60%
- ✅ 有实际交易执行

---

**状态**: 代码已实现，等待重启测试
