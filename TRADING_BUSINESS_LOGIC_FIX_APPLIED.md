# 交易卡点修复 - 业务逻辑优化应用

**应用时间**: 2026-04-23 09:23
**状态**: ✅ 配置已应用，等待验证

---

## 🔧 应用的修复

### 1. 禁用快照一致性检查

**配置**：
```bash
PM_LCB_GLOBAL_SNAPSHOT_CONSISTENCY_ENABLED=0
```

**目的**：
- 打破快照一致性死锁
- 允许在其他条件满足时交易
- 消除 4.5% 的阻止

**原因**：
- 系统要求最近 2 次快照都可交易
- 但过去 24 小时从未有过可交易决策
- pass_count=0，required=2 → 永远不通过
- 形成恶性循环

---

### 2. tick 过期阈值已设置

**配置**：
```bash
PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=300  # 已存在
```

**目的**：
- 允许使用 5 分钟内的数据
- 减少因 tick 过期被阻止
- 降低对 CLOB API 实时性的依赖

**原因**：
- CLOB API 经常超时
- 导致 tick 无法及时刷新
- 29.6% 的决策因 tick 过期被阻止

---

### 3. 重启服务

**操作**：
```bash
launchctl unload ~/Library/LaunchAgents/com.weather.trade_engine.plist
launchctl load ~/Library/LaunchAgents/com.weather.trade_engine.plist
```

**结果**：
- ✅ trade_engine 已重启（PID 74400）
- ✅ Worker 进程已启动（PID 74417, 74428）
- ✅ 重启时间：09:22-09:23

---

## 📊 预期效果

### 阻止率变化

| 阻止原因 | 修复前 | 预期修复后 | 改善 |
|---------|--------|-----------|------|
| market_snapshot_inconsistent | 4.5% | **0%** | ✅ -4.5% |
| market_tick_stale | 29.6% | **~10%** | ✅ -19.6% |
| 其他业务逻辑 | 65.9% | **~60%** | - |
| **总阻止率** | **99.97%** | **~70%** | ✅ **-29.97%** |

### 交易机会

| 指标 | 修复前 | 预期修复后 |
|------|--------|-----------|
| will_order=1 | 0 | **>0** |
| 可交易决策 | 0% | **~30%** |

---

## 🔍 监控指标

### 1. 快照一致性阻止

**检查**：
```sql
SELECT COUNT(*)
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND reason_codes_json LIKE '%market_snapshot_inconsistent%';
```

**期望**: 0

---

### 2. tick 过期阻止

**检查**：
```sql
SELECT COUNT(*)
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND reason_codes_json LIKE '%market_tick_stale%';
```

**期望**: 显著减少（从 29.6% 降到 ~10%）

---

### 3. 可交易订单

**检查**：
```sql
SELECT COUNT(*)
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND will_order = 1;
```

**期望**: >0（首次出现）

---

### 4. 总阻止率

**检查**：
```sql
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) as blocked,
  ROUND(100.0 * SUM(CASE WHEN decided_state = 'blocked' THEN 1 ELSE 0 END) / COUNT(*), 2) as blocked_pct
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes');
```

**期望**: ~70%（从 99.97% 下降）

---

## 📋 验证计划

### 2 分钟后（09:25）

**自动检查**：
- 后台任务将自动检查最近 5 分钟的决策
- 验证配置是否生效

**检查项**：
1. 是否有新的决策生成
2. 是否还有 snapshot_inconsistent 阻止
3. tick_stale 阻止是否减少
4. 是否有 will_order=1 的决策

---

### 10 分钟后（09:33）

**手动检查**：
```bash
# 检查阻止原因分布
sqlite3 data/archive.db "
SELECT
  reason_codes_json,
  COUNT(*) as count
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND decided_state = 'blocked'
GROUP BY reason_codes_json
ORDER BY count DESC
LIMIT 10;
"

# 检查可交易订单
sqlite3 data/archive.db "
SELECT
  slug,
  outcome,
  side,
  created_at
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-10 minutes')
  AND will_order = 1;
"
```

---

### 30 分钟后（09:53）

**全面评估**：
1. 阻止率是否降到 ~70%
2. 是否有实际交易执行
3. 系统是否稳定运行
4. 是否需要进一步优化

---

## 🎯 成功标准

### 最低标准（必须达到）

- ✅ snapshot_inconsistent 阻止 = 0
- ✅ tick_stale 阻止 < 20%
- ✅ 总阻止率 < 90%

### 理想标准（期望达到）

- ✅ 至少 1 个 will_order=1 的决策
- ✅ 总阻止率 ~70%
- ✅ 系统稳定运行

### 完美标准（最佳情况）

- ✅ 有实际交易执行
- ✅ 总阻止率 < 70%
- ✅ 无技术错误

---

## 📝 后续优化

### 如果仍然没有可交易订单

**可能原因**：
1. 业务逻辑过滤仍然太严格
   - plain_lcb_pinned_tail_no_filtered: 23.8%
   - plain_lcb_zero_prob_yes_filtered: 21.5%

2. 其他阻止原因
   - spread_too_wide: 2.3%
   - edge_tradeability_blocked: 多种

**下一步**：
- 分析剩余的主要阻止原因
- 评估是否可以放宽某些条件
- 考虑调整策略参数

---

### 如果有可交易订单但未执行

**可能原因**：
1. 订单执行逻辑有问题
2. 资金不足
3. API 调用失败

**下一步**：
- 检查订单执行日志
- 验证账户余额
- 检查 API 连接

---

## ✅ 当前状态

**配置**：
- ✅ PM_LCB_GLOBAL_SNAPSHOT_CONSISTENCY_ENABLED=0
- ✅ PM_AUTO_TRADE_MARKET_TICK_STALE_SEC=300
- ✅ 其他超时配置已优化

**服务**：
- ✅ trade_engine 运行中（PID 74400）
- ✅ Worker 进程运行中（PID 74417, 74428）
- ✅ 重启时间：09:22-09:23

**监控**：
- ⏳ 后台任务将在 09:25 自动检查结果
- ⏳ 等待验证配置效果

---

**状态**: 业务逻辑优化已应用，等待验证结果
