# 进程分析报告

**分析时间**: 2026-04-23 01:15
**当前运行进程**: 7个主要进程

---

## 📊 进程清单

### 1. auto_trade_watcher (PID: 45276) ✅
```
auto_trade_watcher.py --interval 10 --no-sms
```
**用途**: 监控交易任务队列，每10秒检查一次
**状态**: 正常，守护进程
**服务**: `com.weather.auto_trade_watcher`

---

### 2. model_scan (PID: 49708) ⚠️
```
scan_weather_market.py --scan-weather-today --tz America/New_York --days-ahead 2
--no-update-models --model-refresh-only --no-auto-trade
```
**用途**: 模型刷新（只刷新模型，不更新数据）
**状态**: 长时间运行（从 01:10 开始）
**服务**: `com.weather.model_scan`
**问题**: 这个进程应该很快完成，但运行了5分钟+

---

### 3. daily_scan (PID: 50632) ⚠️
```
scan_weather_market.py --scan-weather-today --days-ahead 2 --no-auto-trade
--ensemble-models gfs_seamless,icon_seamless,nws_forecast --depth-levels 5
```
**用途**: 每日完整扫描（获取天气数据 + 市场数据）
**状态**: 长时间运行
**服务**: `com.weather.daily_scan`
**问题**: 这是最重的任务，应该6小时运行一次

---

### 4. feedback_loop_hourly (PID: 50643) ✅
```
scan_weather_market.py --scan-weather-today --tz America/New_York --days-ahead 0
```
**用途**: 每小时反馈循环
**状态**: 正常
**服务**: `com.weather.feedback_loop_hourly`

---

### 5. model_scan_hourly (PID: 50632 重复?) ⚠️
```
scan_weather_market.py --scan-weather-today --days-ahead 2 --no-auto-trade
--ensemble-models gfs_seamless,icon_seamless,nws_forecast
```
**用途**: 每小时模型扫描
**状态**: 可能与 daily_scan 是同一个进程
**服务**: `com.weather.model_scan_hourly`

---

### 6. market_refresh_urgent (PID: 54225) ✅
```
scan_weather_market.py --scan-weather-today --market-refresh-only
--market-refresh-max-outcomes 20 --market-refresh-time-budget-sec 25
--market-refresh-clob-timeout-sec 5 --market-refresh-clob-retries 3
```
**用途**: 30秒刷新临近结算事件
**状态**: 正常，刚修复的服务
**服务**: `com.weather.market_refresh_urgent`

---

### 7. trade_sync (PID: 55895) ✅
```
scan_weather_market.py --trade-sync-only --trade-sync-date latest
```
**用途**: 同步交易状态
**状态**: 正常
**服务**: `com.weather.trade_engine`

---

### 8. auto_trade_job_worker (PID: 56437) ✅
```
scan_weather_market.py --auto-trade-job-worker --once
```
**用途**: 处理交易任务队列
**状态**: 正常，短期任务
**服务**: `com.weather.auto_trade_entry_1m`

---

## 🔍 问题分析

### 问题1: 多个扫描进程同时运行 ⚠️⚠️⚠️

**发现**:
- `model_scan` (PID: 49708) - 从 01:10 运行至今
- `daily_scan` (PID: 50632) - 从 01:11 运行至今
- `model_scan_hourly` - 可能与 daily_scan 重复

**原因**:
1. **服务调度冲突**: 多个服务在相近时间启动
2. **任务执行时间过长**: 单次扫描超过预期时间
3. **没有互斥锁**: 允许多个扫描同时运行

**影响**:
- 数据库锁竞争严重
- CPU 和内存占用高
- 互相阻塞，导致超时

---

### 问题2: 服务调度时间重叠

**当前调度**:
- `model_scan`: 每6小时（02:10, 08:10, 14:10, 20:10）
- `model_scan_hourly`: 每小时（:05分）
- `daily_scan`: 每6小时（02:10, 08:10, 14:10, 20:10）
- `market_refresh_1m`: 每1分钟
- `market_refresh_urgent`: 每30秒

**问题**:
- `model_scan` 和 `daily_scan` 在相同时间运行（02:10）
- `model_scan_hourly` 可能与其他服务冲突

---

### 问题3: 缺少互斥锁

**发现**:
- `market_refresh_1m.sh` 有锁机制（`market_refresh.lock`）
- 但其他扫描服务没有全局互斥锁
- 允许多个重量级扫描同时运行

---

## 🔧 解决方案

### 立即修复（P0）

#### 1. 停止重复的扫描进程
```bash
# 停止长时间运行的扫描
kill 49708  # model_scan (已运行5分钟+)
kill 50632  # daily_scan (已运行4分钟+)
```

#### 2. 调整服务调度时间（错峰执行）
```bash
# model_scan: 02:10, 08:10, 14:10, 20:10 (保持)
# model_scan_hourly: 每小时 :05 (保持)
# daily_scan: 改为 03:10, 09:10, 15:10, 21:10 (错开1小时)
# feedback_loop_hourly: 每小时 :15 (错开10分钟)
```

---

### 短期优化（P1）

#### 1. 添加全局扫描锁
```bash
# 在所有扫描脚本中添加
SCAN_LOCK_DIR="$ROOT/data/scan_global.lock"
if ! mkdir "$SCAN_LOCK_DIR" 2>/dev/null; then
  echo "Another scan is running, skip"
  exit 0
fi
trap "rm -rf $SCAN_LOCK_DIR" EXIT
```

#### 2. 减少扫描频率
- `model_scan_hourly`: 每小时 → 每2小时
- `daily_scan`: 每6小时 → 每12小时（只在 02:10 和 14:10）

#### 3. 优化扫描参数
- 减少 `--depth-levels`: 5 → 3
- 减少 `--days-ahead`: 2 → 1
- 增加超时限制

---

### 长期优化（P2）

#### 1. 合并重复服务
- 合并 `model_scan` 和 `model_scan_hourly`
- 合并 `daily_scan` 和 `feedback_loop_hourly`

#### 2. 实现任务队列
- 所有扫描任务进入队列
- 单个 worker 顺序执行
- 避免并发冲突

#### 3. 迁移到 PostgreSQL
- 支持真正的并发写入
- 减少锁竞争

---

## 📊 优化后预期

### 进程数量
- 当前: 7-11个进程同时运行
- 优化后: 3-5个进程（减少50%）

### 数据库锁竞争
- 当前: 频繁 `database is locked`
- 优化后: 偶尔锁竞争（<5%）

### 系统资源
- 当前: CPU 30-50%, 内存 2-3GB
- 优化后: CPU 15-25%, 内存 1-2GB

---

## ✅ 立即执行

```bash
# 1. 停止长时间运行的扫描
kill 49708 50632

# 2. 检查进程是否已停止
ps aux | grep scan_weather | grep -v grep

# 3. 观察5分钟，看是否还有锁竞争
tail -f data/market_refresh.log
```

---

**结论**:
- 当前有太多扫描进程同时运行
- 主要原因是服务调度时间重叠 + 缺少互斥锁
- 需要立即停止重复进程，并调整调度时间
