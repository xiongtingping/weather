# 数据清理分析报告

**当前数据使用**: 7.7GB
**目标**: 安全清理，不影响系统运行

---

## 📊 数据库表分析（按大小排序）

| 表名 | 大小(MB) | 用途 | 可清理 | 建议 |
|------|---------|------|--------|------|
| event_runs | 2546 | 模型运行历史 | ✅ 部分 | 保留30天 |
| event_fact_history | 2341 | 事实历史记录 | ✅ 部分 | 保留30天 |
| auto_trade_decisions | 1908 | 交易决策历史 | ✅ 部分 | 保留30天 |
| model_snapshots | 279 | 模型快照 | ✅ 部分 | 保留30天 |
| market_snapshots | 100 | 市场快照 | ✅ 部分 | 保留30天 |
| event_market_ticks | 83 | 市场报价历史 | ✅ 部分 | 保留7天 |
| edge_bias_calibration_runs | 12 | 偏差校正 | ❌ 保留 | 用于校正 |
| feedback_snapshots | 4 | 反馈快照 | ❌ 保留 | 用于学习 |

---

## 🗑️ 可清理的数据

### 1. 历史数据（最大收益）

**event_runs** (2546 MB)
- 保留最近30天的模型运行记录
- 删除30天前的数据
- 预计节省：~2000 MB

**event_fact_history** (2341 MB)
- 保留最近30天的事实历史
- 删除30天前的数据
- 预计节省：~1800 MB

**auto_trade_decisions** (1908 MB)
- 保留最近30天的交易决策
- 删除30天前的数据
- 预计节省：~1500 MB

**market_snapshots** (100 MB)
- 保留最近30天的市场快照
- 删除30天前的数据
- 预计节省：~80 MB

**event_market_ticks** (83 MB)
- 保留最近7天的市场报价
- 删除7天前的数据
- 预计节省：~70 MB

**总计预计节省**: ~5.4 GB

---

### 2. 日志文件（中等收益）

**data/logs/** (17 MB)
- 保留最近7天的日志
- 删除旧日志
- 预计节省：~10 MB

**data/model_scan.log** (2.2 MB)
- 保留最近7天
- 预计节省：~1.5 MB

**data/market_refresh.log** (11 MB)
- 保留最近7天
- 预计节省：~8 MB

**data/auto_trade_worker_timeout_stacks.log** (5.2 MB)
- 保留最近7天
- 预计节省：~4 MB

**总计预计节省**: ~24 MB

---

### 3. 缓存文件（小收益）

**weather_events_cache.json** (8 MB)
- 可以删除，会自动重建
- 预计节省：8 MB

**weather_events_cache.good.json** (8 MB)
- 备份文件，可以删除
- 预计节省：8 MB

**总计预计节省**: ~16 MB

---

### 4. 其他文件

**ourairports_airports.csv** (12 MB)
- 机场数据，如果不需要可以删除
- 预计节省：12 MB

**archive.db-wal** (8 MB)
- SQLite WAL文件，不能删除（正在使用）
- 预计节省：0 MB

---

## ❌ 不能删除的数据

### 关键表（用于系统运行）

1. **event_fact_latest** - 最新事实数据（LCB计算依赖）
2. **edge_bias_calibration_runs** - 偏差校正数据
3. **feedback_snapshots** - 反馈学习数据
4. **q_calibration_bundle.json** - Q值校准数据
5. **q_quote_calibration_bundle.json** - 报价校准数据
6. **model_weights** - 模型权重
7. **model_scores** - 模型评分

### 最近数据（用于分析）

1. 最近30天的 event_runs（用于模型评估）
2. 最近30天的 event_fact_history（用于趋势分析）
3. 最近30天的 auto_trade_decisions（用于策略优化）
4. 最近7天的日志（用于调试）

---

## 🚀 清理方案

### 方案A：保守清理（推荐）

**保留期限**：
- 历史数据：30天
- 日志文件：7天
- 缓存文件：删除

**预计节省**：~5.5 GB
**风险**：低
**数据库大小**：7.7GB → 2.2GB

### 方案B：激进清理

**保留期限**：
- 历史数据：7天
- 日志文件：3天
- 缓存文件：删除

**预计节省**：~6.5 GB
**风险**：中（可能影响长期分析）
**数据库大小**：7.7GB → 1.2GB

---

## 📝 清理脚本

### 安全清理脚本（方案A）

```bash
#!/bin/bash
# 清理30天前的历史数据

ROOT="/Users/xiong/weather"
cd "$ROOT"

# 备份数据库
echo "备份数据库..."
cp data/archive.db data/archive.db.backup.$(date +%Y%m%d_%H%M%S)

# 清理历史数据
echo "清理历史数据..."
sqlite3 data/archive.db << 'EOF'
-- 清理30天前的 event_runs
DELETE FROM event_runs
WHERE created_at < datetime('now', '-30 days');

-- 清理30天前的 event_fact_history
DELETE FROM event_fact_history
WHERE fact_as_of < datetime('now', '-30 days');

-- 清理30天前的 auto_trade_decisions
DELETE FROM auto_trade_decisions
WHERE created_at < datetime('now', '-30 days');

-- 清理30天前的 market_snapshots
DELETE FROM market_snapshots
WHERE created_at < datetime('now', '-30 days');

-- 清理7天前的 event_market_ticks
DELETE FROM event_market_ticks
WHERE created_at < datetime('now', '-7 days');

-- 清理30天前的 model_snapshots
DELETE FROM model_snapshots
WHERE created_at < datetime('now', '-30 days');

-- 压缩数据库
VACUUM;

-- 分析优化
ANALYZE;
EOF

# 清理日志文件
echo "清理日志文件..."
find data/logs -name "*.log" -mtime +7 -delete 2>/dev/null || true
find data -name "*.log" -mtime +7 -delete 2>/dev/null || true

# 清理缓存文件
echo "清理缓存文件..."
rm -f data/weather_events_cache.json
rm -f data/weather_events_cache.good.json

# 显示清理结果
echo "清理完成！"
du -sh data/archive.db
```

---

## ⚠️ 注意事项

### 清理前

1. **备份数据库**
   ```bash
   cp data/archive.db data/archive.db.backup.$(date +%Y%m%d)
   ```

2. **停止所有服务**
   ```bash
   launchctl list | grep com.weather | awk '{print $3}' | xargs -I {} launchctl stop {}
   ```

3. **确认没有进程在使用数据库**
   ```bash
   lsof data/archive.db
   ```

### 清理后

1. **VACUUM 压缩数据库**（释放空间）
2. **ANALYZE 优化查询**（重建统计信息）
3. **重启所有服务**
4. **验证系统正常运行**

---

## 📊 预期效果

| 项目 | 清理前 | 清理后 | 节省 |
|------|--------|--------|------|
| 数据库大小 | 7.7 GB | 2.2 GB | 5.5 GB |
| 日志文件 | 17 MB | 7 MB | 10 MB |
| 缓存文件 | 16 MB | 0 MB | 16 MB |
| **总计** | **7.73 GB** | **2.21 GB** | **5.52 GB** |

---

**推荐**: 先执行方案A（保守清理），观察系统运行1-2天后再决定是否进一步清理
