# Weather 交易系统优化方案

**制定时间**: 2026-04-22
**目标**: 解决 7 个关键交易卡点，提升系统稳定性和交易效率

---

## 方案总览

### 三阶段实施策略

| 阶段 | 时间 | 目标 | 风险等级 |
|------|------|------|----------|
| 第一阶段 | 立即执行（1小时内） | 紧急止血，恢复交易能力 | 低 |
| 第二阶段 | 本周内（3-5天） | 稳定性提升，减少故障 | 中 |
| 第三阶段 | 本月内（2-4周） | 性能优化，架构升级 | 中高 |

---

## 第一阶段：紧急止血（立即执行）

### 目标
- 解决 P0 网络连接失败
- 缓解 P1 扫描超时和任务阻塞
- 恢复交易系统基本功能

### 方案 1.1：网络连接优化

**问题**: `data-api.polymarket.com` 连接超时

**具体步骤**:

1. **修改环境变量** (`.env` 文件)
```bash
# 网络优化 - 启用直连模式
PM_CLOB_FORCE_DIRECT=1

# 增加重试和超时
PM_CLOB_RETRY_MAX=5
PM_CLOB_TIMEOUT_MS=60000
PM_CLOB_DIRECT_RETRIES=3

# 缩短代理冷却期
PM_CLOB_PROXY_COOLDOWN_SEC=300

# Data API 优化
PM_CLOB_TRADES_MAX_PAGES=5
POLY_DATA_API_TIMEOUT=15
POLY_DATA_API_RETRIES=3
```

2. **清理代理冷却状态**
```bash
rm -f /Users/xiong/weather/weather/data/clob_proxy_cooldown.json
```

3. **验证连接**
```bash
cd /Users/xiong/weather/weather
node scripts/polymarket_order.mjs balance
```

**预期效果**:
- 连接成功率从 ~60% 提升到 ~95%
- 交易同步失败率降低 80%

**回滚方案**:
```bash
PM_CLOB_FORCE_DIRECT=0  # 恢复代理模式
```

---

### 方案 1.2：扫描超时优化

**问题**: 模型刷新任务频繁超时（360秒）

**具体步骤**:

1. **调整超时配置**
```bash
# 扫描超时优化
PM_SCAN_TIMEOUT_SEC=600
PM_SCAN_OPEN_METEO_TIMEOUT_SEC=20
PM_SCAN_OPEN_METEO_RETRIES=2
PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT=0

# 减少并发压力
PM_SCAN_MAX_EVENTS_PER_RUN=3
PM_SCAN_MAX_MATCHED_PER_RUN=3
```

2. **临时减少模型数量**
```bash
# 只保留最可靠的 2 个模型
# 在 scripts/model_scan.sh 中修改
--ensemble-models gfs_seamless,ecmwf_ifs025
```

3. **重启扫描守护进程**
```bash
sudo launchctl kickstart -k system/com.weather.model_scan
```

**预期效果**:
- 超时率从 ~40% 降低到 <10%
- 扫描完成时间从 360s 降低到 ~200s

**回滚方案**:
```bash
PM_SCAN_TIMEOUT_SEC=360
--ensemble-models gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast
```

---

### 方案 1.3：任务队列优化

**问题**: 任务队列阻塞，连续 6 分钟无法入队

**具体步骤**:

1. **减少单任务处理量**
```bash
# 任务优化
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=6
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=300
```

2. **清理僵尸任务**（谨慎操作）
```bash
cd /Users/xiong/weather/weather
sqlite3 data/archive.db "
UPDATE auto_trade_jobs
SET state='timeout', ended_at=datetime('now')
WHERE state='running'
AND datetime(started_at, '+10 minutes') < datetime('now');
"
```

3. **重启 worker**
```bash
sudo launchctl kickstart -k system/com.weather.auto_trade_worker
```

**预期效果**:
- 任务执行时间从 6-7 分钟降低到 3-4 分钟
- 队列阻塞率降低 70%

**回滚方案**:
```bash
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=12
```

---

### 方案 1.4：策略门槛调整（观察模式）

**问题**: 策略门槛过严，拒绝大量候选

**具体步骤**:

1. **启用 shadow 模式**（记录但不阻断）
```bash
# 策略优化 - 先观察
PM_EDGE_POLICY_MODE=shadow
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
```

2. **监控 shadow 数据**
```bash
# 查看被 shadow 阻断的交易
sqlite3 data/archive.db "
SELECT
  slug,
  outcome,
  edge_tradeability_blocked,
  edge_tradeability_reasons
FROM event_fact_latest
WHERE edge_tradeability_shadow_block = 1
ORDER BY updated_at DESC
LIMIT 20;
"
```

**预期效果**:
- 收集数据，评估门槛调整的影响
- 不影响当前交易策略

**回滚方案**:
```bash
PM_EDGE_POLICY_MODE=enforced
```

---

## 第一阶段执行清单

```bash
#!/bin/bash
# 执行脚本：phase1_emergency_fix.sh

set -e
cd /Users/xiong/weather/weather

echo "=== 第一阶段：紧急止血 ==="

# 1. 备份当前配置
cp .env .env.backup.$(date +%Y%m%d_%H%M%S)

# 2. 更新环境变量
cat >> .env << 'EOF'

# === 紧急优化配置 (2026-04-22) ===
# 网络优化
PM_CLOB_FORCE_DIRECT=1
PM_CLOB_RETRY_MAX=5
PM_CLOB_TIMEOUT_MS=60000
PM_CLOB_DIRECT_RETRIES=3
PM_CLOB_PROXY_COOLDOWN_SEC=300
PM_CLOB_TRADES_MAX_PAGES=5
POLY_DATA_API_TIMEOUT=15
POLY_DATA_API_RETRIES=3

# 扫描优化
PM_SCAN_TIMEOUT_SEC=600
PM_SCAN_OPEN_METEO_TIMEOUT_SEC=20
PM_SCAN_OPEN_METEO_RETRIES=2
PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT=0
PM_SCAN_MAX_EVENTS_PER_RUN=3
PM_SCAN_MAX_MATCHED_PER_RUN=3

# 任务优化
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=6
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=300

# 策略优化（观察模式）
PM_EDGE_POLICY_MODE=shadow
PM_EDGE_POLICY_PATH_LCB_MIN=0.05
EOF

# 3. 清理代理冷却
rm -f data/clob_proxy_cooldown.json

# 4. 验证连接
echo "验证网络连接..."
node scripts/polymarket_order.mjs balance || echo "警告: 连接测试失败"

# 5. 重启守护进程
echo "重启守护进程..."
sudo launchctl kickstart -k system/com.weather.model_scan || true
sudo launchctl kickstart -k system/com.weather.auto_trade_worker || true
sudo launchctl kickstart -k system/com.weather.trade_sync_loop || true

echo "=== 第一阶段完成 ==="
echo "请监控日志："
echo "  tail -f data/model_scan.log"
echo "  tail -f data/logs/trade_engine.log"
```

**执行时间**: 5-10 分钟
**风险等级**: 低
**需要停机**: 否（滚动重启）

---

## 第二阶段：稳定性提升（本周内）

### 目标
- 实现智能代理选择
- 优化模型刷新逻辑
- 增强任务调度能力

### 方案 2.1：智能代理选择

**实现方案**:

修改 `scripts/polymarket_order.mjs`，增加代理健康检查：

```javascript
// 新增：代理健康检查函数
async function probeProxyHealth(host, port) {
  const start = Date.now();
  const reachable = await proxyReachable(host, port, 1000);
  const latency = Date.now() - start;
  return { reachable, latency };
}

// 新增：选择最优代理
async function selectBestProxy() {
  const candidates = proxyPortCandidates;
  const results = [];

  for (const port of candidates) {
    const health = await probeProxyHealth('127.0.0.1', port);
    results.push({ port, ...health });
  }

  // 选择延迟最低的可达代理
  const best = results
    .filter(r => r.reachable)
    .sort((a, b) => a.latency - b.latency)[0];

  return best?.port || null;
}
```

**预期效果**:
- 代理选择成功率提升到 90%
- 平均响应时间降低 30%

---

### 方案 2.2：增量模型刷新

**实现方案**:

修改 `scan_weather_market.py`，实现优先级刷新：

```python
def prioritize_refresh_events(events, positions, open_orders):
    """优先刷新有持仓/订单的市场"""
    priority_slugs = set()

    # 有持仓的市场
    for pos in positions:
        priority_slugs.add(pos['slug'])

    # 有订单的市场
    for order in open_orders:
        priority_slugs.add(order['slug'])

    # 分组
    priority = [e for e in events if e['slug'] in priority_slugs]
    others = [e for e in events if e['slug'] not in priority_slugs]

    return priority, others

# 使用
priority_events, other_events = prioritize_refresh_events(
    all_events, positions, open_orders
)

# 先刷新优先级事件
refresh_models(priority_events, timeout=30)

# 后台异步刷新其他事件
if time_remaining > 60:
    refresh_models(other_events[:5], timeout=15)
```

**预期效果**:
- 关键市场数据实时性提升 90%
- 整体扫描时间降低 40%

---

### 方案 2.3：任务并发调度

**实现方案**:

修改任务调度逻辑，支持入场/出场并发：

```python
# 新增：任务类型优先级
TASK_PRIORITY = {
    'exit': 100,   # 出场最高优先级
    'entry': 80,   # 入场次之
    'refresh': 50  # 刷新最低
}

# 新增：并发 worker 支持
def can_run_concurrent(task1, task2):
    """判断两个任务是否可以并发执行"""
    # 不同类型可以并发
    if task1['source'] != task2['source']:
        return True
    # 不同市场可以并发
    if task1.get('slug') != task2.get('slug'):
        return True
    return False
```

**预期效果**:
- 任务吞吐量提升 2-3 倍
- 队列阻塞率降低 90%

---

## 第三阶段：性能优化（本月内）

### 目标
- 实现实时数据流
- 优化数据库性能
- 引入智能缓存

### 方案 3.1：WebSocket 实时数据

**实现方案**:

新增 WebSocket 订阅模块：

```python
# 新文件：websocket_market_feed.py
import asyncio
import websockets

async def subscribe_market_updates(token_ids, callback):
    """订阅市场实时更新"""
    uri = "wss://clob.polymarket.com/ws"

    async with websockets.connect(uri) as ws:
        # 订阅
        await ws.send(json.dumps({
            "type": "subscribe",
            "channels": [f"book:{tid}" for tid in token_ids]
        }))

        # 接收更新
        async for message in ws:
            data = json.loads(message)
            await callback(data)
```

**预期效果**:
- 市场数据延迟从 60s 降低到 <1s
- 减少 80% 的轮询请求

---

### 方案 3.2：数据库性能优化

**实现方案**:

1. **增加索引**
```sql
-- 优化查询性能
CREATE INDEX IF NOT EXISTS idx_event_fact_latest_slug_outcome
ON event_fact_latest(slug, outcome, event_date);

CREATE INDEX IF NOT EXISTS idx_auto_trade_jobs_state_priority
ON auto_trade_jobs(state, priority DESC, created_at);

CREATE INDEX IF NOT EXISTS idx_edge_trades_exit_triggered
ON edge_trades(exit_triggered_at)
WHERE exit_triggered_at IS NOT NULL;
```

2. **批量写入优化**
```python
def batch_upsert_facts(facts, batch_size=100):
    """批量写入事实表"""
    with db.transaction():
        for i in range(0, len(facts), batch_size):
            batch = facts[i:i+batch_size]
            db.executemany(
                "INSERT OR REPLACE INTO event_fact_latest ...",
                batch
            )
```

**预期效果**:
- 数据库写入速度提升 5-10 倍
- 查询响应时间降低 60%

---

### 方案 3.3：智能缓存层

**实现方案**:

新增 Redis 缓存层：

```python
# 新文件：cache_manager.py
import redis
import json

class CacheManager:
    def __init__(self):
        self.redis = redis.Redis(
            host='localhost',
            port=6379,
            decode_responses=True
        )

    def cache_market_tick(self, token_id, data, ttl=60):
        """缓存市场 tick 数据"""
        key = f"tick:{token_id}"
        self.redis.setex(key, ttl, json.dumps(data))

    def get_market_tick(self, token_id):
        """获取缓存的 tick 数据"""
        key = f"tick:{token_id}"
        data = self.redis.get(key)
        return json.loads(data) if data else None
```

**预期效果**:
- API 调用量降低 70%
- 响应速度提升 10 倍

---

## 监控和验证

### 关键指标

1. **网络健康度**
```bash
# 监控脚本
watch -n 60 'grep "CLOB Client" data/logs/trade_engine.log | tail -20'
```

2. **扫描成功率**
```bash
# 统计脚本
grep "model_scan" data/model_scan.log | \
  awk '{if($3=="ok") ok++; else fail++} END {print "成功率:", ok/(ok+fail)*100"%"}'
```

3. **任务队列状态**
```bash
# 查询脚本
sqlite3 data/archive.db "
SELECT
  state,
  COUNT(*) as count,
  AVG(julianday(ended_at) - julianday(started_at)) * 86400 as avg_duration_sec
FROM auto_trade_jobs
WHERE run_date = date('now')
GROUP BY state;
"
```

### 告警阈值

| 指标 | 正常 | 警告 | 严重 |
|------|------|------|------|
| 网络成功率 | >95% | 90-95% | <90% |
| 扫描超时率 | <5% | 5-15% | >15% |
| 任务队列长度 | <3 | 3-10 | >10 |
| 平均任务时长 | <180s | 180-300s | >300s |

---

## 回滚计划

### 快速回滚

```bash
#!/bin/bash
# 回滚脚本：rollback.sh

cd /Users/xiong/weather/weather

# 恢复备份配置
LATEST_BACKUP=$(ls -t .env.backup.* | head -1)
cp "$LATEST_BACKUP" .env

# 重启服务
sudo launchctl kickstart -k system/com.weather.model_scan
sudo launchctl kickstart -k system/com.weather.auto_trade_worker
sudo launchctl kickstart -k system/com.weather.trade_sync_loop

echo "已回滚到: $LATEST_BACKUP"
```

### 分阶段回滚

- **第一阶段**: 修改 `.env` 即可，无需代码回滚
- **第二阶段**: 使用 `git revert` 回滚代码修改
- **第三阶段**: 停用新增服务（WebSocket、Redis）

---

## 风险评估

| 方案 | 风险等级 | 主要风险 | 缓解措施 |
|------|----------|----------|----------|
| 1.1 网络优化 | 低 | 直连可能被限流 | 保留代理作为备用 |
| 1.2 扫描优化 | 低 | 数据完整性降低 | 监控 Q 值缺失率 |
| 1.3 任务优化 | 中 | 并发冲突 | 增加锁机制 |
| 1.4 策略调整 | 中 | 亏损交易增加 | Shadow 模式观察 |
| 2.1 智能代理 | 中 | 探测增加延迟 | 异步探测 |
| 2.2 增量刷新 | 中 | 逻辑复杂度 | 充分测试 |
| 2.3 并发调度 | 高 | 数据竞争 | 事务隔离 |
| 3.1 WebSocket | 高 | 连接稳定性 | 断线重连 |
| 3.2 数据库优化 | 中 | 索引维护成本 | 定期分析 |
| 3.3 缓存层 | 中 | 缓存一致性 | TTL 控制 |

---

**方案结束**
