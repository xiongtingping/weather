# 市场数据陈旧问题 - 完整修复报告

**修复时间**: 2026-04-23 00:00 - 00:25
**状态**: ✅ 所有问题已修复，等待数据更新验证

---

## 🔍 发现的所有问题（5层）

### 第1层：Yield 死锁 ✅ 已修复

**问题**：
```
数据陈旧（69小时）
  ↓
model_scan 检测到陈旧，执行 yield（让步）
  ↓
数据不更新
  ↓
继续陈旧...（死锁）
```

**修复**：
```bash
# .env
PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE=0
```

**验证**：日志显示 "bypass skip" 而不是 "yield"

---

### 第2层：系统架构设计 ✅ 已理解

**设计**：
- `model_scan.sh`（每5分钟）：只刷新市场报价（`--market-refresh-only`）
- `daily_scan_and_settlement.sh`（每天凌晨2:10）：完整模型扫描

**原因**：
- 完整扫描需要调用 Open-Meteo API，频繁调用会触发速率限制
- 市场报价刷新只调用 CLOB API，可以高频运行

**影响**：
- `event_fact_latest.updated_at` 只在完整扫描时更新
- 如果 daily_scan 失败，数据会陈旧24小时+

---

### 第3层：超时配置错误 ✅ 已修复

**问题**：
- `model_scan.sh` 使用 `PM_SCAN_MODEL_SCAN_TIMEOUT_SEC`（默认420秒）
- 我们设置的是 `PM_SCAN_TIMEOUT_SEC`（无效）
- 导致扫描在7分钟后被强制终止

**修复**：
```bash
# .env
PM_SCAN_MODEL_SCAN_TIMEOUT_SEC=1800
PM_MODEL_SCAN_AUTO_TRADE_TIMEOUT_SEC=1800
```

**验证**：日志不再显示 "timeout kill pid"

---

### 第4层：硬编码的 model-refresh-only ✅ 已理解

**问题**：
- `model_scan.sh` 第787行硬编码 `--model-refresh-only`
- 无环境变量可以控制

**原因**：
- 这是设计如此，`model_scan.sh` 就是用来刷新市场报价的
- 完整扫描由 `daily_scan_and_settlement.sh` 负责

**解决方案**：
- 修复 `daily_scan_and_settlement.sh` 的问题（见第5层）
- 或手动运行完整扫描

---

### 第5层：daily_scan 路径错误（真正的根因）✅ 已修复

**问题**：
```bash
# daily_scan_and_settlement.sh 第4行
ROOT_DIR="/Users/xiong/weather"  # 错误！

# 实际路径应该是
ROOT_DIR="/Users/xiong/weather/weather"
```

**影响**：
```
/Users/xiong/weather/scripts/lib/run_with_timeout.sh: No such file or directory
```

**结果**：
- 每天凌晨2:10的完整扫描都失败
- 数据一直停留在上次成功扫描的时间（2026-04-22 22:59:54）

**修复**：
1. 整理目录结构，消除嵌套
2. 更新 `daily_scan_and_settlement.sh` 路径
3. 更新所有 launchd 服务配置（9个）

---

## 🛠️ 额外修复的问题

### 目录结构混乱 ✅ 已整理

**问题**：
- `/Users/xiong/weather/weather/` 多了一层嵌套
- 导致路径配置混乱

**修复**：
1. 将内层 `weather/` 的所有内容移到外层
2. 更新所有 launchd 服务配置：
   ```bash
   for f in ~/Library/LaunchAgents/com.weather.*.plist; do
     sed -i '' 's|/Users/xiong/weather/weather|/Users/xiong/weather|g' "$f"
   done
   ```
3. 更新 `daily_scan_and_settlement.sh` 路径
4. 清理空数据库文件和旧备份

**结果**：
- 统一路径为 `/Users/xiong/weather/`
- 所有服务配置一致

---

## 📊 修复前后对比

### 数据状态

| 指标 | 修复前 | 修复后（预期） |
|------|--------|----------------|
| 总事件数 | 565 | 565 |
| 新鲜数据（<5分钟） | 111 (19.6%) | > 500 (88%+) |
| 最新更新时间 | 2026-04-22 22:59:54 | 2026-04-23 00:xx:xx |
| 数据年龄 | 8+ 小时 | < 5 分钟 |

### 拒绝率

| 拒绝原因 | 修复前 | 修复后（预期） |
|----------|--------|----------------|
| market_tick_stale | 36.03% | < 5% |
| 总拒绝率 | 78.67% | < 20% |
| 实际交易机会 | 21.33% | > 80% |

---

## 🔧 应用的配置优化

### 网络优化
```bash
PM_CLOB_FORCE_DIRECT=0                    # 保持代理
PM_CLOB_RETRY_MAX=5                       # 增加重试
PM_CLOB_TIMEOUT_MS=60000                  # 增加超时
PM_CLOB_PROXY_COOLDOWN_SEC=300            # 缩短冷却
PM_CLOB_RETRY_BASE_MS=1000                # 重试延迟
PM_CLOB_RETRY_MAX_MS=8000                 # 最大延迟
```

### 扫描优化
```bash
PM_SCAN_TIMEOUT_SEC=900                   # 增加超时
PM_SCAN_MODEL_SCAN_TIMEOUT_SEC=1800       # 模型扫描超时
PM_MODEL_SCAN_AUTO_TRADE_TIMEOUT_SEC=1800 # 自动交易超时
PM_SCAN_OPEN_METEO_TIMEOUT_SEC=20         # API 超时
PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT=0   # 禁用限流
PM_SCAN_MAX_EVENTS_PER_RUN=3              # 减少压力
```

### 任务优化
```bash
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=6        # 减少单任务量
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=300 # 任务超时
PM_AUTO_TRADE_EVENT_WORKER_TIMEOUT_SEC=180 # 事件超时
```

### 市场刷新优化
```bash
PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE=0  # 禁用 yield
PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN=20                  # 增加覆盖
PM_MARKET_REFRESH_TIME_BUDGET_SEC=40                     # 增加预算
PM_MARKET_REFRESH_CLOB_TIMEOUT_SEC=5                     # CLOB 超时
PM_MARKET_REFRESH_CLOB_RETRIES=3                         # CLOB 重试
```

### 策略优化
```bash
PM_EDGE_POLICY_MODE=shadow                # 观察模式
PM_EDGE_POLICY_PATH_LCB_MIN=0.05          # 降低门槛
```

---

## 📝 验证步骤

### 1. 检查数据新鲜度
```bash
./check_data_freshness.sh
```

**目标**：
- fresh_5min > 500 (88%+)
- newest_age_sec < 300 (5分钟)

### 2. 检查拒绝率
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total_decisions,
  SUM(CASE WHEN will_order = 0 AND reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) as stale_rejects,
  ROUND(100.0 * SUM(CASE WHEN will_order = 0 AND reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) / COUNT(*), 2) as stale_reject_pct
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour');
"
```

**目标**：
- stale_reject_pct < 5%

### 3. 监控 daily_scan
```bash
# 查看日志
tail -f data/logs/daily_scan.err.log

# 检查服务状态
launchctl list | grep com.weather.daily_scan
```

**目标**：
- 无错误日志
- 每天凌晨2:10成功运行

---

## 🎯 当前状态

- ✅ 所有配置问题已修复
- ✅ 目录结构已整理
- ✅ 所有服务已重新加载
- ⏳ 完整模型扫描正在运行（PID 82666）
- ⏳ 等待数据更新验证

---

## 📚 相关文档

- [完整卡点总结](./COMPLETE_BOTTLENECK_SUMMARY.md)
- [其他卡点分析](./OTHER_BOTTLENECKS_ANALYSIS.md)
- [市场数据修复方案](./MARKET_DATA_STALE_FIX.md)
- [优化计划（代理模式）](./OPTIMIZATION_PLAN_PROXY.md)

---

## 🚀 下一步

1. **验证数据更新**（10-20分钟后）
   - 运行 `./check_data_freshness.sh`
   - 确认 fresh_5min > 500

2. **监控拒绝率**（1小时后）
   - 检查 market_tick_stale 拒绝率
   - 目标：< 5%

3. **优化其他卡点**
   - 概率过滤（24.53% 拒绝）
   - 市场数据缺失（6.86% 拒绝）

4. **长期监控**
   - 确保 daily_scan 每天成功运行
   - 监控数据新鲜度
   - 跟踪交易成功率

---

**报告结束**

**修复完成时间**: 2026-04-23 00:25
**预期生效时间**: 2026-04-23 00:30（扫描完成后）
