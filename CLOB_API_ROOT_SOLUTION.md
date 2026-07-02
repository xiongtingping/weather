# CLOB API 性能问题根本解决方案

**分析时间**: 2026-04-23 10:05
**问题**: CLOB API 太慢，导致 60% 的决策因技术问题被阻止

---

## 🎯 问题根源

### CLOB API 性能瓶颈

**表现**：
- 连接超时频繁（即使设置 10 秒）
- 导致 market_tick_stale（42.5%）
- 导致 market_tick_missing（17.6%）
- 合计 60% 的决策被技术问题阻止

**根本原因**：
1. **clob.polymarket.com 服务器响应慢**
   - 可能是地理位置远
   - 可能是服务器负载高
   - 可能是网络质量差

2. **系统对实时数据依赖过强**
   - 每次决策都要获取最新 orderbook
   - 无法容忍数据稍旧
   - 没有降级机制

3. **同步阻塞式调用**
   - Worker 等待 CLOB API 响应
   - 一个慢请求阻塞整个决策流程
   - 无法并行处理

---

## 🔧 根本解决方案

### 方案 1: 实现 Orderbook 缓存（推荐）

**原理**：
- 缓存最近获取的 orderbook
- 决策时优先使用缓存
- 后台异步刷新缓存

**实现步骤**：

#### 1.1 添加缓存层

```python
# 在 scan_weather_market.py 中添加
import time
from typing import Dict, Tuple, Optional

# 全局缓存
orderbook_cache: Dict[str, Tuple[dict, float]] =
ORDERBOOK_CACHE_TTL = 300  # 5 分钟

def get_orderbook_cached(token_id: str, timeout_sec: float = 10) -> Optional[dict]:
    """
    获取 orderbook，优先使用缓存
    """
    now = time.time()

    # 检查缓存
    if token_id in orderbook_cache:
        cached_data, cached_time = orderbook_cache[token_id]
        age = now - cached_time

        # 如果缓存未过期，直接返回
        if age < ORDERBOOK_CACHE_TTL:
            return cached_data

    # 缓存过期或不存在，尝试获取新数据
    try:
        fresh_data = fetch_orderbook(token_id, timeout_sec)
        if fresh_data:
            orderbook_cache[token_id] = (fresh_data, now)
            return fresh_data
    except Exception as e:
        # 获取失败，如果有旧缓存，使用旧缓存
        if token_id in orderbook_cache:
            cached_data, cached_time = orderbook_cache[token_id]
            age = now - cached_time

            # 即使过期，也比没有数据好
            if age < 3600:  # 最多使用 1 小时的旧数据
                return cached_data

    return None
```

#### 1.2 修改决策逻辑

```python
# 在 execute_auto_trades 中
# 原来：
# orderbook = fetch_orderbook(token_id, timeout_sec=10)

# 改为：
orderbook = get_orderbook_cached(token_id, timeout_sec=10)
```

#### 1.3 添加缓存清理

```python
def cleanup_orderbook_cache():
    """
    清理过期缓存
    """
    now = time.time()
    expired_keys = [
        k for k, (_, t) in orderbook_cache.items()
        if now - t > 3600  # 1 小时
    ]
    for k in expired_keys:
        del orderbook_cache[k]
```

**优点**：
- ✅ 大幅减少 CLOB API 调用
- ✅ 降低对实时性的依赖
- ✅ 有降级机制（使用旧数据）
- ✅ 实现简单，风险低

**缺点**：
- ⚠️ 使用稍旧的数据（但比没有数据好）
- ⚠️ 需要内存存储缓存

---

### 方案 2: 异步后台刷新（推荐）

**原理**：
- 决策时不等待 CLOB API
- 使用最近可用的数据
- 后台线程持续刷新数据

**实现步骤**：

#### 2.1 创建后台刷新线程

```python
import threading
import queue

# 刷新队列
refresh_queue = queue.Queue()
orderbook_data: Dict[str, Tuple[dict, float]] = {}

def orderbook_refresh_worker():
    """
    后台线程，持续刷新 orderbook
    """
    while True:
        try:
            token_id = refresh_queue.get(timeout=1)

            try:
                data = fetch_orderbook(token_id, timeout_sec=10)
                if data:
                    orderbook_data[token_id] = (data, time.time())
            except Exception as e:
                # 刷新失败，保留旧数据
                pass

            refresh_queue.task_done()
        except queue.Empty:
            continue

# 启动后台线程
refresh_thread = threading.Thread(target=orderbook_refresh_worker, daemon=True)
refresh_thread.start()
```

#### 2.2 修改决策逻辑

```python
def get_orderbook_async(token_id: str) -> Optional[dict]:
    """
    获取 orderbook，不阻塞
    """
    # 请求刷新（异步）
    try:
        refresh_queue.put_nowait(token_id)
    except queue.Full:
        pass

    # 返回当前可用数据
    if token_id in orderbook_data:
        data, timestamp = orderbook_data[token_id]
        return data

    return None
```

**优点**：
- ✅ 决策不被 CLOB API 阻塞
- ✅ 持续刷新数据
- ✅ 提高吞吐量

**缺点**：
- ⚠️ 实现复杂
- ⚠️ 需要管理线程
- ⚠️ 第一次可能没有数据

---

### 方案 3: 使用 Redis 缓存（生产级）

**原理**：
- 使用 Redis 作为分布式缓存
- 多个 worker 共享缓存
- 支持 TTL 自动过期

**实现步骤**：

#### 3.1 安装 Redis

```bash
# macOS
brew install redis
brew services start redis

# 或使用 Docker
docker run -d -p 6379:6379 redis:alpine
```

#### 3.2 添加 Redis 客户端

```python
import redis
import json

redis_client = redis.Redis(
    host='localhost',
    port=6379,
    decode_responses=True
)

def get_orderbook_redis(token_id: str, timeout_sec: float = 10) -> Optional[dict]:
    """
    使用 Redis 缓存 orderbook
    """
    cache_key = f"orderbook:{token_id}"

    # 尝试从 Redis 获取
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    # 缓存未命中，获取新数据
    try:
        data = fetch_orderbook(token_id, timeout_sec)
        if data:
            # 存入 Redis，TTL 5 分钟
            redis_client.setex(
                cache_key,
                300,  # 5 分钟
                json.dumps(data)
            )
            return data
    except Exception as e:
        pass

    return None
```

**优点**：
- ✅ 分布式缓存，多 worker 共享
- ✅ 自动过期管理
- ✅ 持久化支持
- ✅ 生产级方案

**缺点**：
- ⚠️ 需要额外服务（Redis）
- ⚠️ 增加系统复杂度

---

### 方案 4: 降低实时性要求（快速修复）

**原理**：
- 接受使用更旧的数据
- 大幅增加 tick_stale 阈值

**实现步骤**：

```bash
# 从 300 秒（5 分钟）增加到 1800 秒（30 分钟）
echo "PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=1800" >> .env

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

**优点**：
- ✅ 实现最简单
- ✅ 立即生效
- ✅ 无需代码修改

**缺点**：
- ❌ 使用非常旧的数据
- ❌ 决策可能不准确
- ❌ 不是根本解决方案

---

### 方案 5: 使用代理或 CDN（网络优化）

**原理**：
- 使用更快的网络路径
- 减少延迟

**实现步骤**：

#### 5.1 使用 HTTP 代理

```bash
# 在 .env 中添加
export HTTP_PROXY=http://your-proxy:port
export HTTPS_PROXY=http://your-proxy:port
```

#### 5.2 使用 VPN

- 选择靠近 Polymarket 服务器的 VPN 节点
- 可能改善连接速度

**优点**：
- ✅ 可能显著改善速度
- ✅ 无需代码修改

**缺点**：
- ⚠️ 需要额外服务
- ⚠️ 可能增加成本
- ⚠️ 效果不确定

---

## 📋 推荐实施方案

### 短期（立即实施）

**方案 1 + 方案 4 组合**：

1. **实现简单缓存**（1-2 小时开发）
   - 添加内存缓存
   - 5 分钟 TTL
   - 降级使用旧数据

2. **增加 tick_stale 阈值**（立即）
   - 从 300 秒增加到 600 秒（10 分钟）
   - 作为临时缓冲

```bash
# 立即执行
echo "PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=600" >> .env
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

**预期效果**：
- tick_stale 阻止：42.5% → ~10%
- 总阻止率：93.9% → ~60%
- 可能出现第一笔可交易订单

---

### 中期（1-2 天开发）

**方案 2: 异步后台刷新**

- 实现后台刷新线程
- 决策不阻塞
- 持续更新数据

**预期效果**：
- Worker 执行时间大幅缩短
- 吞吐量提升 3-5 倍
- 技术阻止率 < 20%

---

### 长期（生产级）

**方案 3: Redis 缓存 + 方案 2**

- 使用 Redis 分布式缓存
- 异步后台刷新
- 监控和告警

**预期效果**：
- 技术阻止率 < 10%
- 系统稳定可靠
- 支持横向扩展

---

## ✅ 立即行动建议

### 选项 A: 快速修复（5 分钟）

```bash
# 增加 tick_stale 阈值到 10 分钟
sed -i '' 's/PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=300/PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=600/' .env

# 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

**优点**: 立即生效，可能看到第一笔可交易订单
**缺点**: 使用较旧数据

---

### 选项 B: 实现缓存（需要代码修改）

我可以帮你：
1. 修改 `scan_weather_market.py`
2. 添加缓存逻辑
3. 测试验证

**优点**: 根本解决问题
**缺点**: 需要 1-2 小时开发和测试

---

### 选项 C: 两者结合

1. 先执行选项 A（立即缓解）
2. 再实施选项 B（根本解决）

**优点**: 立即见效 + 长期稳定
**缺点**: 需要分两步

---

## 🎯 我的建议

**立即执行选项 A**，然后观察 10 分钟：
- 如果出现可交易订单 → 问题缓解，可以继续使用
- 如果仍然没有 → 需要实施选项 B（缓存）

你想：
1. 先试试选项 A（增加阈值到 10 分钟）？
2. 还是直接实施选项 B（实现缓存）？
3. 或者两者结合？
