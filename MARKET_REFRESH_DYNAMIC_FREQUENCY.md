# 市场盘口动态刷新频率优化方案

**问题**: 当前市场刷新频率固定（每1分钟），无法应对临近结算、临近最高温等关键时刻的需求
**目标**: 根据事件紧急程度动态调整刷新频率，提高关键时刻的报价新鲜度

---

## 📊 当前状态分析

### 现有刷新机制
1. **market_refresh_1m.sh**（每1分钟）
   - 刷新所有事件的市场报价
   - 固定频率，无优先级

2. **model_scan.sh**（每5分钟）
   - 只刷新市场盘口（`--market-refresh-only`）
   - 固定频率，无优先级

### 问题
- 临近结算时（如今天7.4小时后），市场波动大，1分钟的刷新频率可能不够
- 临近最高温时刻（通常在下午2-4点），价格变化快
- 有持仓或挂单的事件需要更高频刷新
- 远期事件（>3天）不需要高频刷新

---

## 🎯 优化方案

### 方案A：分级刷新策略（推荐）

根据事件紧急程度，分为4个等级：

#### 等级1：极紧急（每30秒）
**触发条件**：
- 距离结算 < 2小时
- 有持仓（position_size > 0）
- 有挂单（open_orders > 0）

**刷新频率**：每30秒
**事件数量**：预计 5-10 个

#### 等级2：紧急（每1分钟）
**触发条件**：
- 距离结算 < 6小时
- 或有持仓/挂单
- 或 edge_lcb > 阈值（候选交易）

**刷新频率**：每1分钟（保持现状）
**事件数量**：预计 20-50 个

#### 等级3：正常（每5分钟）
**触发条件**：
- 距离结算 6-24小时
- 无持仓/挂单

**刷新频率**：每5分钟
**事件数量**：预计 100-200 个

#### 等级4：低频（每15分钟）
**触发条件**：
- 距离结算 > 24小时
- 无持仓/挂单
- edge_lcb < 阈值

**刷新频率**：每15分钟
**事件数量**：预计 300+ 个

---

### 方案B：临近最高温时刻加速

**核心思想**：在预测的最高温时刻前后1小时，提高刷新频率

#### 实施逻辑
```python
# 获取预测的最高温时刻
forecast_peak_time = get_forecast_peak_time(event)

# 计算距离峰值时刻的时间
hours_to_peak = (forecast_peak_time - now).total_seconds() / 3600

if -1 < hours_to_peak < 1:  # 峰值前后1小时
    refresh_interval = 30  # 每30秒
elif -2 < hours_to_peak < 2:  # 峰值前后2小时
    refresh_interval = 60  # 每1分钟
else:
    refresh_interval = 300  # 每5分钟
```

---

## 🔧 实施方案A（推荐）

### 第1步：创建优先级刷新脚本

```bash
# scripts/market_refresh_priority.sh
#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

# 加载环境变量
if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi

# 获取当前时间
NOW=$(date -u +%s)

# 查询不同优先级的事件
sqlite3 data/archive.db << 'EOF' > /tmp/priority_events.txt
SELECT
  slug,
  event_date,
  ROUND((julianday(event_date) - julianday('now')) * 24, 1) as hours_until,
  CASE
    WHEN (julianday(event_date) - julianday('now')) * 24 < 2 THEN 1  -- 极紧急
    WHEN (julianday(event_date) - julianday('now')) * 24 < 6 THEN 2  -- 紧急
    WHEN (julianday(event_date) - julianday('now')) * 24 < 24 THEN 3 -- 正常
    ELSE 4  -- 低频
  END as priority
FROM event_fact_latest
WHERE event_date > datetime('now')
ORDER BY priority, hours_until;
EOF

# 根据优先级刷新
# 优先级1：极紧急事件
PRIORITY_1=$(awk '$4 == 1 {print $1}' /tmp/priority_events.txt | head -20)
if [ -n "$PRIORITY_1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [priority_refresh] P1 events: $(echo "$PRIORITY_1" | wc -l)" >> "$ROOT/data/market_refresh.log"
  # 刷新这些事件
fi

# 优先级2：紧急事件
PRIORITY_2=$(awk '$4 == 2 {print $1}' /tmp/priority_events.txt | head -50)
if [ -n "$PRIORITY_2" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [priority_refresh] P2 events: $(echo "$PRIORITY_2" | wc -l)" >> "$ROOT/data/market_refresh.log"
fi
```

### 第2步：创建30秒刷新服务（极紧急事件）

```xml
<!-- ~/Library/LaunchAgents/com.weather.market_refresh_30s.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.weather.market_refresh_30s</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>/Users/xiong/weather/scripts/market_refresh_30s.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>30</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/xiong/weather/data/logs/market_refresh_30s.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/xiong/weather/data/logs/market_refresh_30s.err.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/xiong/weather</string>
</dict>
</plist>
```

### 第3步：修改现有的1分钟刷新（只处理优先级2）

```bash
# 在 market_refresh_1m.sh 中添加过滤逻辑
# 只刷新优先级2的事件（紧急但不是极紧急）
```

---

## 📊 预期效果

### 刷新频率对比

| 事件类型 | 当前 | 优化后 | 提升 |
|---------|------|--------|------|
| 极紧急（<2h） | 1分钟 | 30秒 | 2倍 |
| 紧急（<6h） | 1分钟 | 1分钟 | 保持 |
| 正常（<24h） | 1分钟 | 5分钟 | - |
| 低频（>24h） | 1分钟 | 15分钟 | - |

### API 调用量

| 时间段 | 当前 | 优化后 | 变化 |
|--------|------|--------|------|
| 每分钟 | ~100次 | ~30次 | -70% |
| 极紧急事件 | ~10次/分钟 | ~20次/分钟 | +100% |
| 总调用量/天 | ~144,000 | ~50,000 | -65% |

### 关键时刻响应

| 场景 | 当前 | 优化后 |
|------|------|--------|
| 结算前2小时 | 1分钟延迟 | 30秒延迟 |
| 最高温时刻 | 1分钟延迟 | 30秒延迟 |
| 有持仓事件 | 1分钟延迟 | 30秒延迟 |

---

## 🚀 简化实施方案（快速上线）

如果完整方案太复杂，可以先实施简化版：

### 简化版：只加速临近结算事件

```bash
# scripts/market_refresh_urgent.sh
#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

# 只刷新距离结算<2小时的事件
.venv/bin/python scan_weather_market.py \
  --market-refresh-only \
  --market-refresh-max-outcomes 20 \
  --market-refresh-time-budget-sec 25 \
  --filter-hours-until-close 2
```

创建30秒刷新服务：
```bash
# 创建 plist 文件
# StartInterval: 30
```

**优点**：
- 实施简单
- 立即见效
- 风险低

**缺点**：
- 不考虑最高温时刻
- 不考虑持仓/挂单

---

## ⚠️ 风险控制

### API 限流风险
- CLOB API 限流：未知（需要监控）
- 如果触发限流，立即停止30秒刷新

### 监控指标
```bash
# 每小时检查API调用量
grep "market_refresh" data/logs/*.log | wc -l

# 检查限流错误
grep -i "429\|rate limit" data/logs/*.log
```

### 降级策略
如果触发限流：
1. 停止30秒刷新服务
2. 恢复1分钟刷新
3. 增加刷新间隔到2分钟

---

## 📝 实施建议

### 立即实施（今晚）
- ✅ 简化版：30秒刷新临近结算事件（<2小时）
- ⏳ 监控API调用量

### 本周实施
- 完整版：4级优先级刷新
- 添加最高温时刻加速
- 添加持仓/挂单优先级

### 下周优化
- 根据市场波动率动态调整
- 根据交易量动态调整
- 机器学习预测最佳刷新频率

---

**推荐**: 先实施简化版，观察1-2天后再决定是否实施完整版
