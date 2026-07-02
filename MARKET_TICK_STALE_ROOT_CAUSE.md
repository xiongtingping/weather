# Market_tick_stale问题根因分析

**分析时间**: 2026-04-25 13:40 UTC
**问题**: market_tick_stale占比16.7%（426个案例），是最大可优化卡点

---

## 🔍 根因发现

### 问题1：Market_refresh进程卡住

**发现**:
```bash
ps aux | grep market_refresh
xiong  44532  ... 10:45上午  /Python scan_weather_market.py --market-refresh-only
xiong  44524  ... 10:45上午  /bin/bash market_refresh_frequent.sh
```

**问题**:
- 进程从10:45上午运行到现在（13:40），**卡住了3小时**
- 导致市场数据无法更新
- 所有市场数据都超过180秒（3分钟）阈值

### 问题2：Market_refresh_1m服务配置

**当前配置**:
```xml
<key>StartInterval</key>
<integer>60</integer>  <!-- 每60秒运行一次 -->
```

**环境变量**:
```xml
<key>PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC</key>
<string>120</string>  <!-- 单次运行最多120秒 -->

<key>PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE</key>
<string>6</string>  <!-- 每次最多刷新6个事件 -->
```

**问题**:
- 理论上每60秒运行一次，每次最多120秒
- 但实际上进程卡住，没有超时机制
- 导致后续的refresh无法启动（因为有锁）

### 问题3：扫描锁机制

**代码逻辑**:
```bash
# market_refresh_frequent.sh
if ! acquire_scan_lock "market_refresh_frequent" 0; then
  echo "skipped (another scan is running)"
  exit 0
fi
```

**问题**:
- 如果有进程卡住，锁不会释放
- 后续的refresh都会被跳过
- 导致市场数据一直过期

---

## 📊 影响分析

### 当前状态

**Market_tick_stale统计**（最近1小时）:
- 案例数：426个（16.7%）
- 阈值：180秒（3分钟）
- 实际情况：所有数据都超过3小时未更新

**影响**:
- 即使signal_lcb通过，数据过期也会阻止交易
- 这是**0订单的主要原因之一**

### 为什么还没解决

**历史原因**:
1. **之前的优化重点在signal层**
   - 提高model_spread系数（0.40→0.60）
   - 降低model_spread系数（0.60→0.18）
   - 优化signal_alpha计算
   - 但忽略了数据采集问题

2. **进程卡住是隐藏问题**
   - 服务配置正确（每60秒运行）
   - 但实际执行时卡住
   - 没有超时保护机制

3. **缺乏监控**
   - 没有监控market_refresh的运行状态
   - 没有告警机制
   - 日志文件为空（0B）

---

## 🎯 解决方案

### 立即行动（P0）

**1. 杀掉卡住的进程**
```bash
kill -9 44532 44524
```

**2. 清理扫描锁**
```bash
rm -f /Users/xiong/weather/data/runtime_locks/scan_lock_*
```

**3. 重启market_refresh服务**
```bash
launchctl unload ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
```

### 短期修复（P1，本周）

**1. 添加超时保护**

修改`scripts/market_refresh_1m.sh`，添加超时：
```bash
# 使用timeout命令（需要安装coreutils）
timeout 120s .venv/bin/python scan_weather_market.py \
  --scan-weather-today \
  --days-ahead 2 \
  --market-refresh-only \
  --depth-levels 5
```

或使用Python的timeout：
```bash
.venv/bin/python -c "
import subprocess, signal
def timeout_handler(signum, frame):
    raise TimeoutError('Market refresh timeout')
signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(120)
try:
    subprocess.run(['.venv/bin/python', 'scan_weather_market.py', ...])
finally:
    signal.alarm(0)
"
```

**2. 改进锁机制**

添加锁超时清理：
```bash
# 检查锁文件年龄，超过5分钟自动清理
find /Users/xiong/weather/data/runtime_locks/ -name "scan_lock_*" -mmin +5 -delete
```

**3. 添加监控**

创建监控脚本：
```bash
#!/bin/bash
# 检查market_refresh是否正常运行
last_refresh=$(sqlite3 data/archive.db "SELECT MAX(created_at) FROM market_ticks")
now=$(date -u +%s)
last_refresh_ts=$(date -d "$last_refresh" +%s)
age=$((now - last_refresh_ts))

if [ $age -gt 300 ]; then
  echo "ALERT: Market data stale for ${age}s"
  # 发送告警
fi
```

### 中期优化（P2，下周）

**1. 动态调整refresh频率**

根据市场活跃度调整：
```python
# 高优先级市场：30秒
# 中优先级市场：60秒
# 低优先级市场：120秒
```

**2. 优化refresh逻辑**

- 优先刷新有持仓的市场
- 优先刷新高exec_edge的市场
- 跳过已关闭的市场

**3. 增加并发**

- 使用多进程并发刷新
- 避免单个市场卡住影响全局

---

## 📈 预期效果

### 如果立即修复

**当前**:
- Market_tick_stale: 426个（16.7%）
- 数据年龄: >3小时

**修复后**:
- Market_tick_stale: 预计降到50-100个（2-5%）
- 数据年龄: <3分钟

### 如果完成所有优化

**总通过率提升**:
- Signal_lcb优化: 12.8% → 6-8%
- Market_tick_stale优化: 16.7% → 2-5%
- **总通过率: 0% → 3-5%**
- **预期每小时订单数: 0 → 3-5个**

---

## 🔄 为什么之前没解决

### 优先级问题

**之前的focus**:
1. ✅ Signal层优化（model_spread系数调整）
2. ✅ Signal_alpha优化（趋势权重、consensus_adjustment）
3. ❌ 数据采集优化（被忽略）

**原因**:
- Signal层是"算法优化"，更有技术挑战性
- 数据采集是"工程问题"，看起来不够sexy
- 但实际上，**数据质量是基础**

### 监控缺失

**没有及时发现**:
- 没有监控market_refresh运行状态
- 没有监控数据新鲜度
- 日志文件为空，无法追踪

### 锁机制问题

**设计缺陷**:
- 锁没有超时机制
- 进程卡住后锁永远不释放
- 后续refresh都被阻止

---

## 💡 经验教训

### 1. 数据质量是基础

再好的算法，如果数据过期，也无法交易。

### 2. 监控和告警很重要

如果有监控，3小时前就能发现问题。

### 3. 超时保护是必须的

任何长时间运行的任务都需要超时保护。

### 4. 锁机制需要自动清理

避免死锁导致系统停摆。

---

## 🎯 行动计划

### 今天（立即）

1. ✅ 杀掉卡住的进程
2. ✅ 清理扫描锁
3. ✅ 重启market_refresh服务
4. ✅ 验证数据开始更新

### 本周

1. 添加超时保护
2. 改进锁机制
3. 添加监控脚本
4. 设置告警

### 下周

1. 动态调整refresh频率
2. 优化refresh逻辑
3. 增加并发处理

---

**总结**: Market_tick_stale问题的根因是**进程卡住+缺乏超时保护+监控缺失**。这是一个典型的工程问题，不是算法问题。立即修复后，预计可以显著提升通过率。
