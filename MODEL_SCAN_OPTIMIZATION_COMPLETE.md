# Model Scan 频率优化 - 实施完成报告

**实施时间**: 2026-04-23 00:25 - 00:35
**状态**: ✅ 配置已完成，等待验证

---

## ✅ 已完成的工作

### 1. 创建每小时扫描服务
- ✅ 脚本：`scripts/model_scan_hourly.sh`
- ✅ 服务：`com.weather.model_scan_hourly.plist`
- ✅ 运行时间：每小时第5分钟
- ✅ 服务已加载并激活

### 2. 修改 daily_scan 为 6小时扫描
- ✅ 修改：`com.weather.daily_scan.plist`
- ✅ 运行时间：02:10, 08:10, 14:10, 20:10（每6小时）
- ✅ 对齐 GFS/ICON 模型更新时间
- ✅ 服务已重新加载

### 3. 保持现有的5分钟市场刷新
- ✅ 服务：`com.weather.model_scan`
- ✅ 功能：只刷新市场盘口（--model-refresh-only）
- ✅ 无需修改

---

## 📊 新的扫描架构

### 三层刷新策略

```
┌─────────────────────────────────────────────────────────┐
│  每5分钟：市场盘口刷新                                    │
│  - 只调用 CLOB API                                       │
│  - 更新市场报价                                          │
│  - 0次 Open-Meteo 调用                                   │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│  每1小时：模型预测刷新（新增）                            │
│  - 扫描所有事件                                          │
│  - 更新模型预测和 fact                                   │
│  - 约15-20次 Open-Meteo 调用                             │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│  每6小时：全量扫描+结算                                   │
│  - 完整模型刷新                                          │
│  - 结算已到期的市场                                      │
│  - 约50次 Open-Meteo 调用                                │
└─────────────────────────────────────────────────────────┘
```

---

## 📈 预期效果

### 数据新鲜度

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 最大陈旧时间 | 24小时 | 1小时 |
| 平均陈旧时间 | 12小时 | 30分钟 |
| fresh_5min % | 19.6% | > 80% |
| fresh_1hour % | 19.6% | > 95% |

### 拒绝率

| 拒绝原因 | 优化前 | 优化后 |
|----------|--------|--------|
| market_tick_stale | 36.03% | < 5% |
| 总拒绝率 | 78.67% | < 25% |
| 实际交易机会 | 21.33% | > 75% |

### API 调用量

| 时间段 | 调用次数 | 说明 |
|--------|---------|------|
| 每小时 | ~20 | 每小时扫描 |
| 每6小时 | ~50 | 全量扫描 |
| 每天总计 | ~480 | 24×20 + 4×50 |
| API限额 | 10,000 | Open-Meteo 免费版 |
| 使用率 | 4.8% | 安全余量 95.2% |

---

## 🔧 服务配置

### 当前运行的服务

```bash
com.weather.model_scan              # 每5分钟，市场刷新
com.weather.model_scan_hourly       # 每1小时，模型刷新（新增）
com.weather.daily_scan              # 每6小时，全量扫描（已修改）
com.weather.market_refresh_1m       # 每1分钟，市场报价
com.weather.auto_trade_watcher      # 自动交易监控
com.weather.auto_trade_entry_1m     # 自动交易入口
com.weather.trade_engine            # 交易引擎
com.weather.feedback_loop_hourly    # 反馈循环
com.weather.q_calibrated_writeback_hourly  # Q值校准
```

### 运行时间表

| 时间 | 服务 | 功能 |
|------|------|------|
| 每分钟 | market_refresh_1m | 市场报价刷新 |
| 每5分钟 | model_scan | 市场盘口刷新 |
| 每小时 :05 | model_scan_hourly | 模型预测刷新 |
| 02:10, 08:10, 14:10, 20:10 | daily_scan | 全量扫描+结算 |

---

## 📝 验证步骤

### 1. 检查服务状态
```bash
launchctl list | grep com.weather
```

### 2. 检查数据新鲜度（1小时后）
```bash
./check_data_freshness.sh
```

**目标**：
- fresh_5min > 450 (80%+)
- fresh_1hour > 530 (95%+)
- newest_age_sec < 3600 (1小时)

### 3. 检查拒绝率（2小时后）
```bash
sqlite3 data/archive.db "
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN will_order = 0 AND reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) as stale_rejects,
  ROUND(100.0 * SUM(CASE WHEN will_order = 0 AND reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) / COUNT(*), 2) as stale_pct
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour');
"
```

**目标**：
- stale_pct < 5%

### 4. 监控 API 调用量（每天）
```bash
# 查看日志中的 Open-Meteo 请求
grep -r "open-meteo" data/logs/*.log | wc -l
```

**目标**：
- 每天 < 500 次

---

## ⚠️ 风险控制

### 监控指标
1. **数据新鲜度**：每小时检查一次
2. **API调用量**：每天检查一次
3. **拒绝率**：每小时检查一次
4. **扫描失败率**：每天检查一次

### 降级策略
如果触发 API 限流：
1. 立即停止每小时扫描
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.weather.model_scan_hourly.plist
   ```
2. 恢复每6小时扫描（保持现状）
3. 增加缓存 TTL

---

## 🎯 下一步

### 立即（今晚）
- ✅ 配置已完成
- ⏳ 等待首次完整扫描完成（约10-20分钟）
- ⏳ 验证数据更新

### 明天
- 检查数据新鲜度
- 检查拒绝率变化
- 监控 API 调用量
- 查看扫描日志

### 本周
- 优化每小时扫描的事件选择逻辑
- 调整扫描超时配置
- 监控系统稳定性

---

## 📚 相关文档

- [频率优化方案](./MODEL_SCAN_FREQUENCY_OPTIMIZATION.md)
- [市场数据修复完成](./MARKET_DATA_FIX_COMPLETE.md)
- [完整卡点总结](./COMPLETE_BOTTLENECK_SUMMARY.md)

---

**实施完成时间**: 2026-04-23 00:35
**预期生效时间**: 2026-04-23 01:05（下一个小时扫描）
**首次6小时扫描**: 2026-04-23 02:10
