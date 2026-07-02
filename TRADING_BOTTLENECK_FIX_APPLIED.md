# 交易卡点修复已应用

**修复时间**: 2026-04-23 01:33
**状态**: 配置已更新，等待验证

---

## ✅ 已完成的修复

### 1. 定位根本原因

**问题**：
- 修复只应用到了 `market_refresh` 流程（数据刷新）
- 未应用到 `auto_trade` 流程（交易决策）
- auto_trade 流程仍使用默认的 `timeout=1.2s`

**证据**：
```
过去2小时的超时错误：
- timeout_1.2s: 4,093 次 ← auto_trade 使用的旧配置
- timeout_3s: 0 次
- timeout_5s: 0 次
```

---

### 2. 应用的修复

**修改文件**: `.env`

**新增配置**：
```bash
# Auto-trade entry refresh CLOB timeout (默认 1.2s 太短，增加到 5s)
PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC=5
PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES=3
```

**代码位置**：
- `scan_weather_market.py` 第 14365-14370 行
- 变量：`entry_refresh_timeout_sec`, `entry_refresh_retries`
- 默认值：`1.2s`, `1 retry`
- 新值：`5s`, `3 retries`

---

### 3. 重启服务

**重启的服务**：
```bash
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_watcher.plist
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_entry_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_watcher.plist
```

**当前状态**：
- `auto_trade_entry_1m`: 运行中
- `auto_trade_watcher`: 运行中 (PID 83920)
- `auto-trade-job-worker`: 2个进程运行中

---

## 📊 当前状态

### 修复前（过去2小时）

| 指标 | 值 |
|------|-----|
| 总决策数 | 70,846 |
| 阻止数 | 70,837 |
| 实际交易 | 0 |
| **阻止率** | **99.99%** |
| market_tick_missing | 6,697 (9.5%) |
| timeout_1.2s | 4,093 (61% of tick_missing) |

---

### 修复后（等待验证）

**预期效果**：
- market_tick_missing 从 9.5% 降至 **<2%**
- timeout_1.2s 应该消失，被 timeout_5s 替代
- 总阻止率从 99.99% 降至 **~70%**
- 实际交易数从 0 增加到 **~30%**

---

## 🔍 验证方法

### 1. 检查超时类型是否改变

```bash
sqlite3 data/archive.db "
SELECT
  CASE
    WHEN reasons_json LIKE '%connect timeout=1.2%' THEN 'timeout_1.2s'
    WHEN reasons_json LIKE '%connect timeout=5%' THEN 'timeout_5s'
    ELSE 'other'
  END as timeout_type,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-5 minutes')
  AND reason_codes_json LIKE '%market_tick_missing%'
GROUP BY timeout_type;
"
```

**期望结果**：
- `timeout_1.2s` 应该为 0 或很少
- `timeout_5s` 应该出现（如果仍有超时）

---

### 2. 检查阻止率是否下降

```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) as blocked,
  SUM(CASE WHEN will_order = 1 THEN 1 ELSE 0 END) as will_trade,
  ROUND(100.0 * SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) / COUNT(*), 2) as blocked_pct
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes');
"
```

**期望结果**：
- `blocked_pct` 应该从 99.99% 降至 ~70%
- `will_trade` 应该 > 0

---

### 3. 检查 market_tick_missing 趋势

```bash
sqlite3 data/archive.db "
SELECT
  datetime(created_at) as time,
  COUNT(*) as decisions,
  SUM(CASE WHEN reason_codes_json LIKE '%market_tick_missing%' THEN 1 ELSE 0 END) as tick_missing
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
GROUP BY strftime('%Y-%m-%d %H:%M', created_at)
ORDER BY time DESC;
"
```

**期望结果**：
- `tick_missing` 应该显著减少

---

## ⏰ 验证时间表

| 时间 | 操作 |
|------|------|
| 01:33 | 修复应用，服务重启 |
| 01:35 | 第一批新配置的决策生成 |
| 01:38 | 应该看到 timeout_5s 出现 |
| 01:40 | 应该看到阻止率开始下降 |
| 01:45 | 应该看到实际交易出现 |

---

## 🚨 如果修复无效

**可能原因**：
1. 环境变量未被 worker 进程读取
2. 代码中有其他硬编码的超时值
3. CLOB API 本身响应慢（非超时问题）

**进一步调试**：
```bash
# 检查 worker 进程的环境变量
ps eww -p $(pgrep -f "auto-trade-job-worker" | head -1) | tr ' ' '\n' | grep PM_AUTO_TRADE

# 检查最新的错误详情
sqlite3 data/archive.db "
SELECT reasons_json
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-2 minutes')
  AND reason_codes_json LIKE '%market_tick_missing%'
LIMIT 1;
"
```

---

## 📝 技术细节

### 配置读取流程

1. `auto_trade_entry_1m.sh` 启动
2. 加载 `.env` 文件
3. 启动 `scan_weather_market.py --auto-trade-job-worker`
4. Python 代码读取 `PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC`
5. 在 `_try_refresh_market_tick()` 中使用该超时值
6. 调用 `fetch_orderbook(timeout_sec=5.0, retries=3)`

### 关键代码路径

```python
# scan_weather_market.py:14365-14370
entry_refresh_timeout_sec = _env_float("PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC")
if entry_refresh_timeout_sec is None or not math.isfinite(entry_refresh_timeout_sec):
    entry_refresh_timeout_sec = 1.2  # 默认值
entry_refresh_timeout_sec = max(1.0, min(float(entry_refresh_timeout_sec), 10.0))

entry_refresh_retries = _env_int("PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES")
if entry_refresh_retries is None or entry_refresh_retries < 1:
    entry_refresh_retries = 1  # 默认值
entry_refresh_retries = max(1, min(int(entry_refresh_retries), 3))
```

---

**状态**: 修复已应用，等待5-10分钟验证效果
