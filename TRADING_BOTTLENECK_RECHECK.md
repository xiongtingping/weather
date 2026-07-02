# 交易卡点复查报告

**检查时间**: 2026-04-23 01:30
**检查范围**: 过去2小时的交易决策数据

---

## 📊 当前状态

### 总体阻止率

| 指标 | 修复前 | 当前 | 变化 |
|------|--------|------|------|
| 总决策数 | ~70,000 | 70,846 | - |
| 阻止数 | ~69,986 | 70,837 | - |
| 实际交易 | 0 | 0 | ❌ 无改善 |
| **阻止率** | **99.98%** | **99.99%** | ❌ **无改善** |

---

## 🔍 问题分析

### 1. 修复未生效的原因

**CLOB API 超时配置未应用到 auto_trade 流程**

检查发现：
- ✅ `market_refresh_1m.sh` 已配置 `timeout=3s, retries=2`
- ✅ `market_refresh_urgent.sh` 已配置 `timeout=5s, retries=3`
- ❌ **但 auto_trade 流程仍在使用旧的 `timeout=1.2s`**

**证据**：
```
最近的超时错误（过去2小时）：
- timeout_1.2s: 4,093 次 ← 旧配置
- timeout_3s: 0 次
- timeout_5s: 0 次
```

**原因**：
- `market_refresh_1m.sh` 和 `market_refresh_urgent.sh` 只负责**刷新市场数据**
- 但 **auto_trade 决策流程** 使用的是 `auto_trade_entry_1m.sh` 和 `auto_trade_watcher.sh`
- 这些 auto_trade 脚本**没有应用新的超时配置**

---

### 2. market_tick_missing 趋势

**过去10分钟的 tick_missing 数量**：

```
时间          | 总决策 | tick_missing
--------------|--------|-------------
17:29         | 67     | 0  ← 服务重启后改善
17:28         | 43     | 0
17:27         | 22     | 0
17:26         | 72     | 22
17:25         | 45     | 32
17:24         | 30     | 24
17:23         | 55     | 18
17:22         | 50     | 36
```

**观察**：
- 17:27 之后（服务重启后）tick_missing 降为 0
- 但这只是 **market_refresh** 流程的改善
- **auto_trade 流程仍在使用旧配置**

---

### 3. 当前运行的进程

```bash
# market_refresh_1m (已应用新配置)
--market-refresh-clob-timeout-sec 3
--market-refresh-clob-retries 2

# market_refresh_urgent (已应用新配置)
--market-refresh-clob-timeout-sec 5
--market-refresh-clob-retries 3

# auto_trade_job_worker (仍使用旧配置)
--auto-trade-job-worker --once
# ← 没有显式的 timeout 参数，使用默认值 1.2s
```

---

## 🎯 根本原因

**修复只应用到了数据刷新流程，未应用到交易决策流程**

系统有两个独立的流程：

1. **数据刷新流程** (`market_refresh_1m.sh`, `market_refresh_urgent.sh`)
   - 作用：定期刷新市场 tick 数据
   - 状态：✅ 已修复（timeout 3s/5s）
   - 效果：tick_missing 已降为 0

2. **交易决策流程** (`auto_trade_entry_1m.sh`, `auto_trade_watcher.sh`)
   - 作用：执行交易决策，需要实时获取 orderbook
   - 状态：❌ 未修复（仍使用 timeout 1.2s）
   - 效果：99.99% 的决策仍被阻止

---

## 🔧 需要修复的文件

### 1. `scripts/auto_trade_entry_1m.sh`

需要添加：
```bash
: "${PM_AUTO_TRADE_CLOB_TIMEOUT_SEC:=5}"
: "${PM_AUTO_TRADE_CLOB_RETRIES:=3}"

export PM_AUTO_TRADE_CLOB_TIMEOUT_SEC PM_AUTO_TRADE_CLOB_RETRIES
```

并在调用 `scan_weather_market.py` 时传递：
```bash
--auto-trade-clob-timeout-sec "${PM_AUTO_TRADE_CLOB_TIMEOUT_SEC}"
--auto-trade-clob-retries "${PM_AUTO_TRADE_CLOB_RETRIES}"
```

### 2. `scripts/auto_trade_watcher.sh`

同样需要添加超时配置。

### 3. `.env` 文件

添加全局配置：
```bash
PM_AUTO_TRADE_CLOB_TIMEOUT_SEC=5
PM_AUTO_TRADE_CLOB_RETRIES=3
```

---

## 📈 预期效果

修复后，预计：
- market_tick_missing 从 35.7% 降至 **<5%**
- 总阻止率从 99.99% 降至 **~70%**
- 实际交易数从 0 增加到 **~30%**

---

## ✅ 下一步行动

1. **立即修复** auto_trade 脚本的超时配置
2. **重启** auto_trade 相关服务
3. **观察** 10分钟后的交易决策数据
4. **验证** 阻止率是否降至 ~70%

---

**状态**: 问题已定位，等待修复
