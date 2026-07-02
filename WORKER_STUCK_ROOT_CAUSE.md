# Worker卡住问题根因分析

**分析时间**: 2026-04-25 14:05 UTC
**问题**: Auto_trade_entry worker反复卡住，导致决策无法执行

---

## 🔍 根因发现

### 问题：Runtime Lock死锁

**现象**:
1. Worker进程启动后立即卡住（运行41分钟无响应）
2. 任务状态变为"retrying"，错误信息："runtime_lock_busy"
3. Lock文件存在但进程已无响应
4. 后续worker无法获取锁，任务堆积

**根因**:
```python
# scan_weather_market.py:36245
def _auto_trade_runtime_lock_acquire(run_date: str, *, note: str = "") -> bool:
    lock_dir = DATA_DIR / "auto_trade_runtime.lock"
    # 问题1: 进程卡住后，lock永远不释放
    # 问题2: stale_sec默认1800秒（30分钟），太长
    # 问题3: 没有超时保护机制
```

**触发条件**:
- Worker进程在执行`run_auto_trades_for_date()`时卡住
- Lock文件存在，PID有效，但进程无响应
- 30分钟内lock不会被清理
- 后续worker全部失败："runtime_lock_busy"

---

## 📊 影响分析

### 当前状态

**Worker卡住历史**:
- PID 53489: 13:53启动，运行10分钟后卡住
- PID 80853: 14:03启动，运行41分钟后卡住
- 两次都需要手动kill才能恢复

**任务队列状态**:
- 3229: entry_1m任务，失败1次（runtime_lock_busy）
- 3234: manual_check任务，成功执行（手动清理lock后）

**Market_tick_stale状态**:
- 最近10分钟：1164个（31.8%）
- **比之前更差**（之前21.1%）
- 原因：worker卡住导致决策延迟

---

## 🎯 解决方案

### 立即修复（P0）

**1. 降低lock stale时间**
```bash
# 修改环境变量，从1800秒降到300秒（5分钟）
export PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC=300
```

**2. 添加worker超时保护**
```python
# 在run_auto_trade_job_worker_once()中添加超时
# 当前没有超时，导致worker可以无限卡住
```

**3. 添加lock监控**
```bash
# 监控lock年龄，超过5分钟自动清理
find /Users/xiong/weather/data/auto_trade_runtime.lock -mmin +5 -exec rm -rf {} \;
```

### 短期修复（P1，今天）

**1. 修改launchd配置**

修改`com.weather.task_queue_worker.plist`：
```xml
<!-- 添加超时保护 -->
<key>SoftResourceLimits</key>
<dict>
    <key>CPU</key>
    <integer>300</integer>  <!-- 5分钟CPU时间 -->
</dict>
<key>HardResourceLimits</key>
<dict>
    <key>CPU</key>
    <integer>600</integer>  <!-- 10分钟CPU时间 -->
</dict>
```

**2. 添加heartbeat检测**

Worker已有heartbeat机制（45秒），但没有检测卡住：
```python
# 当前heartbeat只更新lease_until
# 应该检测：如果heartbeat失败，主动退出
```

**3. 添加进程监控**
```bash
# 监控worker进程，如果运行超过10分钟，自动kill
ps aux | grep auto_trade_job_worker | awk '{if($10 > 10) print $2}' | xargs kill -9
```

### 中期优化（P2，本周）

**1. 优化lock机制**
- 使用数据库锁代替文件锁
- 添加自动清理机制
- 缩短stale时间到2-3分钟

**2. 优化worker逻辑**
- 添加超时保护（5-10分钟）
- 优化heartbeat检测
- 添加进度日志

**3. 添加监控告警**
- Worker运行时间超过5分钟：告警
- Lock年龄超过3分钟：告警
- 任务失败率超过10%：告警

---

## 📈 预期效果

### 如果立即修复

**当前**:
- Worker卡住频率：每10-40分钟
- 需要手动kill才能恢复
- Market_tick_stale：31.8%

**修复后**:
- Lock自动清理（5分钟）
- Worker自动超时（10分钟）
- Market_tick_stale：预计降到15-20%

### 如果完成所有优化

**总体改善**:
- Worker稳定运行，无需手动干预
- Lock机制健壮，自动清理
- Market_tick_stale：预计降到5-10%
- **系统可以自动恢复，无需人工介入**

---

## 🔄 为什么会卡住

### Worker卡住的可能原因

**1. 数据库锁等待**
```python
# run_auto_trades_for_date()中可能等待数据库锁
# 如果其他进程持有锁，worker会一直等待
```

**2. 网络请求超时**
```python
# 获取市场数据时，网络请求可能卡住
# 没有超时保护，导致worker无限等待
```

**3. 死循环或无限递归**
```python
# 某些边界情况下，代码可能进入死循环
# 需要添加日志追踪具体卡在哪里
```

### 如何定位卡住位置

**添加详细日志**:
```python
# 在run_auto_trades_for_date()中添加进度日志
print(f"[auto-trade] processing market {market_id}")
print(f"[auto-trade] fetching data for {market_id}")
print(f"[auto-trade] calculating signals for {market_id}")
```

**使用strace追踪**:
```bash
# 当worker卡住时，使用strace查看系统调用
strace -p <worker_pid> -s 1000
```

---

## 💡 经验教训

### 1. Lock机制需要超时保护

任何lock都需要：
- 自动清理机制（stale检测）
- 合理的超时时间（5-10分钟，不是30分钟）
- 监控和告警

### 2. Worker需要超时保护

长时间运行的任务需要：
- 超时保护（5-10分钟）
- Heartbeat检测
- 进度日志

### 3. 监控是必须的

如果有监控：
- 5分钟内就能发现worker卡住
- 自动清理lock，无需人工介入
- 避免问题累积

### 4. 简单的问题可能有复杂的影响

Worker卡住看起来是小问题，但导致：
- 任务堆积
- 决策延迟
- Market_tick_stale恶化（31.8%）
- 需要反复手动干预

---

## 🎯 行动计划

### 今天（立即）

1. ✅ 降低lock stale时间到300秒
2. ✅ 添加lock监控脚本
3. ✅ 添加worker进程监控
4. ⏳ 验证worker不再卡住

### 本周

1. 修改launchd配置，添加资源限制
2. 优化worker超时保护
3. 添加详细日志
4. 设置监控告警

### 下周

1. 优化lock机制（数据库锁）
2. 优化worker逻辑
3. 完整的监控和告警系统

---

## 🔍 验证方法

### 实时监控

```bash
# 监控worker进程
watch -n 5 'ps aux | grep auto_trade_job_worker | grep -v grep'

# 监控lock年龄
watch -n 5 'ls -lh /Users/xiong/weather/data/auto_trade_runtime.lock/'

# 监控任务队列
watch -n 5 'sqlite3 /Users/xiong/weather/data/archive.db "SELECT state, COUNT(*) FROM auto_trade_jobs GROUP BY state"'
```

### 手动检查

```bash
# 检查worker是否卡住（运行时间>10分钟）
ps aux | grep auto_trade_job_worker | awk '{if($10 > 10) print "STUCK:", $0}'

# 检查lock年龄（>5分钟）
find /Users/xiong/weather/data/auto_trade_runtime.lock -mmin +5

# 检查任务失败率
sqlite3 /Users/xiong/weather/data/archive.db "
SELECT
    state,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM auto_trade_jobs), 1) as pct
FROM auto_trade_jobs
GROUP BY state
"
```

---

**总结**: Worker卡住的根因是**runtime lock死锁+缺乏超时保护+监控缺失**。这是一个典型的工程问题，需要添加超时保护、监控和自动恢复机制。立即修复后，系统应该可以自动恢复，无需人工介入。
