# Market_refresh优化方案

**创建时间**: 2026-04-25 14:10 UTC
**问题**: Market_tick_stale 31.8%，数据采集速度跟不上过期速度

---

## 📊 当前配置分析

### Launchd配置
```xml
StartInterval: 60秒（每分钟运行一次）
PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE: 6
PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC: 120秒
```

### 脚本配置（market_refresh_1m.sh）
```bash
# 默认配置
PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN: 12
PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN: 2
PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN: 0

# Auto-trade运行时（优先级模式）
PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN_AUTO: 2
PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN_AUTO: 0

# Burst模式（当tick_age_p90 >= 2700秒）
PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE: 6
PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC: 120秒
```

### 优先级策略
```bash
# 固定优先级（总是刷新）
- 有持仓的市场（Top 16）
- 有未成交订单的市场（Top 16）
- 最旧的market_tick（Top 12）

# 候选优先级（每次2个）
- 高exec_edge的市场
- 其他候选市场

# 尾部刷新（防止长尾）
- 最旧的1个市场（>45分钟）
- 最旧的6个facts（>45分钟）
```

---

## 🔍 问题分析

### 1. 刷新速度不足

**计算**:
- 每60秒运行一次
- Auto-trade运行时：最多6个事件
- 非auto-trade时：最多12个事件
- **每小时最多刷新**: 6×60 = 360个事件（auto-trade时）

**需求**:
- 假设有200个活跃市场
- 每个市场需要每3分钟刷新一次（180秒阈值）
- **每小时需要刷新**: 200×20 = 4000次
- **缺口**: 4000 - 360 = 3640次/小时

### 2. 优先级策略过于保守

**当前策略**:
- 固定优先级：16+16+12 = 44个市场
- 候选优先级：2个市场
- **每次最多刷新**: 44+2 = 46个市场（但受限于6个事件）

**问题**:
- 候选优先级太少（2个）
- 其他市场永远不会被刷新
- 导致大量市场数据过期

### 3. Burst模式触发条件太严格

**当前条件**:
- tick_age_p90 >= 2700秒（45分钟）
- 才会触发burst模式

**问题**:
- 45分钟太长，数据已经严重过期
- 应该在3-5分钟就触发burst

---

## 🎯 优化方案

### 方案1：提高刷新频率（立即实施）

**目标**: 从60秒降到30秒

**修改launchd配置**:
```xml
<key>StartInterval</key>
<integer>30</integer>  <!-- 从60改为30 -->
```

**预期效果**:
- 每小时刷新次数：360 → 720次
- Market_tick_stale：31.8% → 15-20%

**实施步骤**:
```bash
# 1. 修改plist文件
vim ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 2. 重新加载
launchctl unload ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 3. 验证
launchctl list | grep market_refresh
```

---

### 方案2：增加每次刷新事件数（立即实施）

**目标**: 从6个增加到12个（auto-trade时）

**修改launchd配置**:
```xml
<key>PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE</key>
<string>12</string>  <!-- 从6改为12 -->
```

**预期效果**:
- 每次刷新：6 → 12个事件
- 每小时刷新次数：360 → 720次
- Market_tick_stale：31.8% → 10-15%

**实施步骤**:
```bash
# 1. 修改plist文件
vim ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 2. 重新加载
launchctl unload ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
```

---

### 方案3：优化Burst触发条件（立即实施）

**目标**: 从2700秒降到180秒

**修改环境变量**:
```bash
# 添加到.env文件
export PM_MARKET_REFRESH_BURST_TICK_AGE_SEC=180  # 从2700改为180
```

**预期效果**:
- 数据过期3分钟就触发burst
- 更快响应数据过期
- Market_tick_stale：31.8% → 10-15%

**实施步骤**:
```bash
# 1. 修改.env文件
echo "export PM_MARKET_REFRESH_BURST_TICK_AGE_SEC=180" >> /Users/xiong/weather/.env

# 2. 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
```

---

### 方案4：增加候选优先级数量（本周实施）

**目标**: 从2个增加到6个

**修改环境变量**:
```bash
# 添加到.env文件
export PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN=6  # 从2改为6
export PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN=6
```

**预期效果**:
- 每次刷新更多候选市场
- 覆盖更多高exec_edge市场
- Market_tick_stale：进一步降低

---

### 方案5：动态优先级（下周实施）

**目标**: 根据数据年龄动态调整优先级

**实现逻辑**:
```python
# 优先级计算
priority_score = (
    has_position * 100 +
    has_open_order * 80 +
    exec_edge * 50 +
    (tick_age_sec / 60) * 10  # 每分钟+10分
)

# 按priority_score排序，优先刷新高分市场
```

**预期效果**:
- 自动优先刷新最需要的市场
- 避免手动调整优先级
- Market_tick_stale：稳定在5-10%

---

### 方案6：并发刷新（下周实施）

**目标**: 使用多进程并发刷新

**实现方式**:
```python
# 使用multiprocessing
from multiprocessing import Pool

def refresh_market(market_id):
    # 刷新单个市场
    pass

with Pool(processes=4) as pool:
    pool.map(refresh_market, market_ids)
```

**预期效果**:
- 刷新速度提升4倍
- 每小时刷新次数：720 → 2880次
- Market_tick_stale：稳定在2-5%

---

## 📈 综合优化效果预测

### 方案1+2+3（立即实施）

**配置变化**:
- 频率：60秒 → 30秒
- 事件数：6 → 12
- Burst触发：2700秒 → 180秒

**预期效果**:
- 每小时刷新次数：360 → 1440次
- Market_tick_stale：31.8% → 8-12%
- **改善幅度：60-70%**

### 方案1+2+3+4（本周完成）

**配置变化**:
- 上述所有
- 候选优先级：2 → 6

**预期效果**:
- 每小时刷新次数：1440 → 1800次
- Market_tick_stale：8-12% → 5-8%
- **改善幅度：75-85%**

### 方案1+2+3+4+5+6（下周完成）

**配置变化**:
- 上述所有
- 动态优先级
- 并发刷新（4进程）

**预期效果**:
- 每小时刷新次数：1800 → 7200次
- Market_tick_stale：5-8% → 2-5%
- **改善幅度：85-95%**

---

## 🚀 实施计划

### 阶段1：立即实施（今天，30分钟）

**步骤**:
1. 修改launchd配置（StartInterval: 30秒）
2. 修改launchd配置（BURST_MAX_EVENTS: 12）
3. 修改.env文件（BURST_TICK_AGE_SEC: 180）
4. 重启market_refresh服务
5. 监控30分钟，观察效果

**验证**:
```bash
# 监控market_tick_stale
watch -n 60 'sqlite3 /Users/xiong/weather/data/archive.db "
SELECT
    COUNT(*) as total,
    SUM(CASE WHEN reason_codes_json LIKE \"%market_tick_stale%\" THEN 1 ELSE 0 END) as stale,
    ROUND(SUM(CASE WHEN reason_codes_json LIKE \"%market_tick_stale%\" THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as pct
FROM auto_trade_decisions
WHERE created_at >= datetime(\"now\", \"-5 minutes\")
"'
```

**成功标准**:
- Market_tick_stale降到15%以下
- 服务稳定运行，无卡住

---

### 阶段2：本周实施（2-3天）

**步骤**:
1. 修改.env文件（PRIORITY_CANDIDATES: 6）
2. 测试配置
3. 重启服务
4. 监控24小时

**成功标准**:
- Market_tick_stale降到8%以下
- 高exec_edge市场数据新鲜

---

### 阶段3：下周实施（5-7天）

**步骤**:
1. 实现动态优先级算法
2. 实现并发刷新
3. 测试和调优
4. 部署到生产

**成功标准**:
- Market_tick_stale稳定在5%以下
- 系统稳定，无性能问题

---

## ⚠️ 风险和注意事项

### 风险1：API限流

**问题**: 刷新频率提高可能触发API限流

**缓解措施**:
- 监控API错误率
- 如果触发限流，降低频率或事件数
- 添加重试和退避机制

### 风险2：数据库锁竞争

**问题**: 更频繁的写入可能导致锁竞争

**缓解措施**:
- 使用短超时（250ms）
- 失败时快速重试
- 监控数据库锁等待时间

### 风险3：系统负载

**问题**: 更频繁的刷新增加CPU/内存负载

**缓解措施**:
- 监控系统资源使用
- 如果负载过高，调整配置
- 考虑使用更强的机器

### 风险4：Worker卡住

**问题**: 更频繁的运行可能导致更多卡住

**缓解措施**:
- 使用monitor_worker_health.sh监控
- 添加超时保护
- 自动清理stuck进程

---

## 📊 监控指标

### 关键指标

**1. Market_tick_stale占比**
```sql
SELECT
    ROUND(SUM(CASE WHEN reason_codes_json LIKE '%market_tick_stale%' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as pct
FROM auto_trade_decisions
WHERE created_at >= datetime('now', '-5 minutes')
```

**2. 数据新鲜度（P90）**
```sql
WITH latest AS (
  SELECT slug, event_date, MAX(created_at) AS ts
  FROM event_market_ticks
  WHERE event_date >= date('now', '-3 day')
  GROUP BY slug, event_date
)
SELECT
    CAST((julianday('now') - julianday(ts)) * 86400 AS INTEGER) as age_sec
FROM latest
ORDER BY age_sec DESC
LIMIT 1 OFFSET (SELECT COUNT(*) * 0.9 FROM latest)
```

**3. 刷新成功率**
```bash
# 查看market_refresh日志
tail -100 /Users/xiong/weather/data/market_refresh.log | grep "exit="
```

**4. API错误率**
```bash
# 查看错误日志
tail -100 /Users/xiong/weather/data/logs/market_refresh_1m.err.log
```

---

## 🎯 成功标准

### 短期（今天）
- ✅ 配置修改完成
- ✅ 服务稳定运行
- 📊 Market_tick_stale降到15%以下

### 中期（本周）
- 📊 Market_tick_stale降到8%以下
- ✅ 无API限流问题
- ✅ 无系统负载问题

### 长期（下周）
- 📊 Market_tick_stale稳定在5%以下
- ✅ 动态优先级生效
- ✅ 并发刷新稳定运行

---

## 📝 实施命令

### 立即实施（复制粘贴）

```bash
# 1. 备份当前配置
cp ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist.backup

# 2. 修改StartInterval（60→30）
sed -i '' 's/<integer>60<\/integer>/<integer>30<\/integer>/' ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 3. 修改BURST_MAX_EVENTS（6→12）
sed -i '' 's/<string>6<\/string>/<string>12<\/string>/' ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 4. 添加BURST_TICK_AGE_SEC到.env
echo "export PM_MARKET_REFRESH_BURST_TICK_AGE_SEC=180" >> /Users/xiong/weather/.env

# 5. 重启服务
launchctl unload ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist
launchctl load ~/Library/LaunchAgents/com.weather.market_refresh_1m.plist

# 6. 验证
launchctl list | grep market_refresh
echo "✅ Market_refresh优化完成！"
echo "等待30分钟后检查效果..."
```

---

**总结**: 通过提高刷新频率（30秒）、增加事件数（12个）、优化burst触发（180秒），预计可以将market_tick_stale从31.8%降到8-12%，改善幅度60-70%。
