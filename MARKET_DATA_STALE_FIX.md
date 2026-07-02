# 市场数据陈旧问题 - 紧急修复方案

**问题**: 市场刷新死锁
**发现时间**: 2026-04-22 23:30
**严重性**: P0 - 致命

---

## 🔴 问题根源

### 死锁循环
```
tick 数据陈旧（69小时）
  ↓
市场刷新检测到陈旧，选择 yield（让步）
  ↓
数据不更新
  ↓
tick 继续陈旧
  ↓
循环...
```

### 日志证据
```
[model_scan] market_refresh running, yield while ticks stale/unknown
(tick_age_p90=250135s th=120s)
```

### 数据状态
- 最新更新：2026-04-22 22:59:54（约 7.5 小时前）
- 最旧数据：8.4 天前
- 新鲜数据（<5分钟）：111/565 (19.6%)

---

## 🚨 立即修复方案

### 方案 A: 强制刷新（推荐）

禁用 yield 逻辑，强制刷新所有市场数据。

```bash
cd /Users/xiong/weather/weather

# 1. 临时禁用 yield 逻辑
cat >> .env << 'EOF'

# === 强制市场刷新 ===
# 禁用陈旧数据 yield（临时）
PM_MARKET_REFRESH_YIELD_ON_STALE=0

# 增加刷新覆盖
PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN=20
PM_MARKET_REFRESH_TIME_BUDGET_SEC=40

# 增加 CLOB 超时
PM_MARKET_REFRESH_CLOB_TIMEOUT_SEC=5
PM_MARKET_REFRESH_CLOB_RETRIES=3

EOF

# 2. 重启市场刷新服务
launchctl kickstart -k "gui/$(id -u)/com.weather.market_refresh_1m"

# 3. 手动触发一次完整刷新
.venv/bin/python scan_weather_market.py \
  --scan-weather-today \
  --market-refresh-only \
  --market-refresh-max-outcomes 30 \
  --market-refresh-time-budget-sec 60 \
  --no-auto-trade \
  --days-ahead 3

# 4. 等待 5 分钟，检查效果
sleep 300
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) as fresh_5min
FROM event_fact_latest;
"
```

**预期效果**：
- 5-10 分钟内刷新所有市场数据
- fresh_5min 从 111 增加到 500+
- tick_age_p90 从 69 小时降低到 < 5 分钟

---

### 方案 B: 清空陈旧数据（激进）

如果方案 A 失败，清空陈旧数据强制重建。

```bash
cd /Users/xiong/weather/weather

# ⚠️ 警告：这会清空所有事实数据，需要重新扫描

# 1. 备份数据库
cp data/archive.db data/archive.db.backup.$(date +%Y%m%d_%H%M%S)

# 2. 清空事实表
sqlite3 data/archive.db "
DELETE FROM event_fact_latest;
DELETE FROM event_fact_history WHERE fact_as_of > datetime('now', '-7 days');
"

# 3. 触发完整扫描
.venv/bin/python scan_weather_market.py \
  --scan-weather-today \
  --days-ahead 3 \
  --no-auto-trade \
  --force-model-refresh
```

**风险**：
- 丢失历史事实数据
- 需要 30-60 分钟重建
- 仅在方案 A 失败时使用

---

## 📊 验证步骤

### 1. 检查数据新鲜度
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) as fresh_5min,
  ROUND(AVG((julianday('now') - julianday(updated_at)) * 86400), 0) as avg_age_sec
FROM event_fact_latest;
"
```

**目标**：
- fresh_5min > 500 (88%+)
- avg_age_sec < 600 (10分钟)

### 2. 检查拒绝率
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total_decisions,
  SUM(CASE WHEN will_order = 0 AND reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) as stale_rejects
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour');
"
```

**目标**：
- stale_rejects < 10% of total_decisions

### 3. 监控日志
```bash
tail -f data/model_scan.log | grep -E "market_refresh|tick_age"
```

**期望看到**：
```
[model_scan] market_refresh completed (events=20 ticks_updated=180)
tick_age_p90=120s  # 从 250,135s 降低到 120s
```

---

## 🔧 长期修复

### 修复 yield 逻辑

当前逻辑：
```python
if tick_age_p90 > threshold:
    yield  # 让步，不刷新
```

应该改为：
```python
if tick_age_p90 > threshold:
    force_refresh()  # 强制刷新，而不是让步
```

### 增加健康检查

添加监控告警：
```bash
# 每 5 分钟检查一次
*/5 * * * * /path/to/check_market_freshness.sh
```

如果 tick_age_p90 > 600s（10分钟），触发告警并强制刷新。

---

## 📝 执行记录

```bash
# 执行时间：
date

# 执行前状态：
sqlite3 data/archive.db "SELECT COUNT(*), SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) FROM event_fact_latest;"

# 执行方案 A

# 执行后状态（5分钟后）：
sqlite3 data/archive.db "SELECT COUNT(*), SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) FROM event_fact_latest;"

# 拒绝率变化：
# 执行前：36.03%
# 执行后：____%
```

---

**修复方案结束**
