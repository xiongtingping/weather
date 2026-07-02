# 交易卡点最终诊断报告

**诊断时间**: 2026-04-23 01:40
**状态**: ❌ 修复无效，环境变量已加载但超时仍为 1.2s

---

## 🔍 诊断结果

### 1. 环境变量已正确加载

**验证**：
```bash
ps eww -p 98235 | grep PM_AUTO_TRADE_ENTRY_REFRESH
```

**结果**：
```
PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC=5
PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES=3
```

✅ 环境变量已被 watcher 进程读取

---

### 2. 但超时仍为 1.2s

**最新错误**（1分钟前）：
```
connect timeout=1.2
```

**统计**（过去2分钟）：
- `timeout_1.2s`: 4,875 次
- `timeout_5s`: 0 次

❌ 代码仍在使用 1.2s 超时

---

### 3. 代码分析

**配置读取位置**（scan_weather_market.py:14365-14372）：
```python
entry_refresh_timeout_sec = _env_float("PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC")
if entry_refresh_timeout_sec is None or not math.isfinite(entry_refresh_timeout_sec):
    entry_refresh_timeout_sec = 1.2  # 默认值
entry_refresh_timeout_sec = max(1.0, min(float(entry_refresh_timeout_sec), 10.0))

entry_refresh_retries = _env_int("PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES")
if entry_refresh_retries is None or entry_refresh_retries < 1:
    entry_refresh_retries = 1  # 默认值
entry_refresh_retries = max(1, min(int(entry_refresh_retries), 3))
```

**配置传递位置**（scan_weather_market.py:20293-20294）：
```python
"timeout_sec": float(entry_refresh_timeout_sec),
"retries": int(entry_refresh_retries),
```

**使用位置**（scan_weather_market.py:20400+）：
```python
book = fetch_orderbook(
    token_id,
    retries=int(refresh_cfg.get("retries") or 1),
    backoff=0.15,
    timeout_sec=float(refresh_cfg.get("timeout_sec") or 2.5),  # ← 这里
    price_augment_enabled=bool(refresh_cfg.get("price_augment_enabled")),
)
```

---

## 🎯 根本原因

**配置只在 worker 启动时读取一次**

1. `auto_trade_watcher.py` 启动时读取环境变量
2. 启动 `auto-trade-job-worker` 进程
3. **worker 进程在启动时读取配置，之后不再重新读取**
4. 修改 `.env` 后，**已运行的 worker 进程不会重新读取**

**时间线**：
- 01:33 - 修改 `.env`，添加 `PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC=5`
- 01:33 - 重启 `auto_trade_watcher`
- 01:35 - watcher 启动新的 worker 进程
- ❌ **但这些 worker 进程是在 01:33 之前的 watcher 启动的**
- ❌ **它们在启动时读取的配置仍是默认的 1.2s**

---

## 🔧 正确的修复方法

### 方法1: 杀死所有 worker 进程（推荐）

```bash
# 杀死所有 auto-trade-job-worker 进程
pkill -f "auto-trade-job-worker"

# watcher 会自动启动新的 worker，新 worker 会读取新配置
sleep 5

# 验证新 worker 已启动
ps aux | grep "auto-trade-job-worker"
```

### 方法2: 重启整个系统

```bash
# 停止所有 auto_trade 服务
launchctl unload ~/Library/LaunchAgents/com.weather.auto_trade_*.plist

# 等待所有进程退出
sleep 5

# 启动所有 auto_trade 服务
launchctl load ~/Library/LaunchAgents/com.weather.auto_trade_*.plist
```

---

## 📊 当前状态

### 阻止率（过去5分钟）

| 指标 | 值 |
|------|-----|
| 总决策数 | 75,920 |
| 阻止数 | 75,911 |
| 实际交易 | 0 |
| **阻止率** | **99.99%** |

### 超时错误（过去2分钟）

| 超时类型 | 数量 |
|---------|------|
| timeout_1.2s | 4,875 |
| timeout_5s | 0 |
| other_timeout | 1 |
| no_timeout | 2,864 |

---

## ✅ 验证步骤

修复后，应该看到：

1. **超时类型改变**：
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
   WHERE created_at >= datetime('now', '-2 minutes')
     AND reason_codes_json LIKE '%market_tick_missing%'
   GROUP BY timeout_type;
   "
   ```

   **期望**：`timeout_1.2s` 应该为 0，`timeout_5s` 应该出现

2. **阻止率下降**：
   ```bash
   sqlite3 data/archive.db "
   SELECT
     COUNT(*) as total,
     SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) as blocked,
     ROUND(100.0 * SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) / COUNT(*), 2) as blocked_pct
   FROM auto_trade_decisions
   WHERE created_at >= datetime('now', '-5 minutes');
   "
   ```

   **期望**：`blocked_pct` 应该从 99.99% 降至 ~70%

3. **实际交易出现**：
   ```bash
   sqlite3 data/archive.db "
   SELECT COUNT(*) as actual_trades
   FROM auto_trade_decisions
   WHERE created_at >= datetime('now', '-5 minutes')
     AND will_order = 1;
   "
   ```

   **期望**：`actual_trades` 应该 > 0

---

## 🚨 关键发现

**Python 进程在启动时读取环境变量，之后不会重新读取**

这意味着：
- 修改 `.env` 后，必须重启所有相关进程
- 仅重启 launchd 服务不够，必须杀死所有 worker 进程
- worker 进程是长期运行的，不会自动重新读取配置

---

**下一步**: 杀死所有 worker 进程，让 watcher 启动新的 worker
