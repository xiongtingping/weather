# Model Scan 频率优化方案

**问题**: 当前设计每天只做一次完整模型扫描，导致数据陈旧36%拒绝率
**目标**: 在不触发API限流的前提下，保持数据新鲜度匹配LCB策略需求

---

## 📊 当前状态分析

### 现有机制
1. **model_scan.sh**（每5分钟）
   - 只刷新市场盘口（`--model-refresh-only`）
   - 不更新模型预测
   - 不更新 `event_fact_latest.updated_at`

2. **daily_scan_and_settlement.sh**（每天凌晨2:10）
   - 完整模型扫描
   - 更新所有预测
   - 问题：频率太低，数据陈旧8-24小时

### 天气模型更新频率
- **GFS**: 每6小时（00, 06, 12, 18 UTC）
- **ICON**: 每6小时（00, 06, 12, 18 UTC）
- **ECMWF**: 每12小时（00, 12 UTC）
- **NWS**: 每小时

### LCB策略的时间敏感性
- LCB 依赖置信区间，需要相对新鲜的预测
- 数据陈旧8小时+，置信区间失效
- 当前拒绝率：36%（market_tick_stale）

---

## 🎯 优化方案

### 方案A：分层刷新策略（推荐）

**核心思想**: 根据事件重要性和模型更新频率，分层刷新

#### 1. 高频刷新（每5分钟）- 保持现状
```bash
# model_scan.sh
--model-refresh-only  # 只刷新市场盘口
```
- 目的：保持市场报价新鲜
- API调用：只调用 CLOB API
- 无 Open-Meteo 调用

#### 2. 中频刷新（每1小时）- 新增
```bash
# 新建 model_scan_hourly.sh
--scan-weather-today
--days-ahead 2
--no-auto-trade
--max-events-per-run 10  # 限制事件数
--priority-mode          # 只扫描优先级高的事件
```
- 目的：更新关键事件的模型预测
- 优先级：
  1. 有持仓的事件
  2. 有挂单的事件
  3. edge_lcb > 阈值的候选事件
- API调用：约10-20次 Open-Meteo 请求
- 时间：约2-3分钟

#### 3. 低频刷新（每6小时）- 修改现有
```bash
# 修改 daily_scan 为 6hourly_scan
# 运行时间：02:10, 08:10, 14:10, 20:10
--scan-weather-today
--days-ahead 2
--no-auto-trade
--force-model-refresh    # 强制刷新所有模型
```
- 目的：全量更新所有事件
- 时机：对齐 GFS/ICON 模型更新时间
- API调用：约100-200次 Open-Meteo 请求
- 时间：约10-15分钟

---

### 方案B：智能触发刷新

**核心思想**: 根据数据陈旧度和交易需求，动态触发刷新

#### 触发条件
```python
# 在 model_scan.sh 中添加逻辑
if tick_age_p90 > 3600:  # 数据陈旧超过1小时
    if has_positions or has_open_orders:
        # 立即触发完整扫描
        run_full_scan(priority_events_only=True)
```

#### 优点
- 按需刷新，节省API调用
- 响应交易需求

#### 缺点
- 逻辑复杂
- 可能错过交易机会

---

## 🔧 实施方案A（推荐）

### 第1步：创建每小时扫描服务

```bash
# 创建 scripts/model_scan_hourly.sh
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

# 只扫描优先级高的事件
.venv/bin/python scan_weather_market.py \
  --scan-weather-today \
  --days-ahead 2 \
  --no-auto-trade \
  --max-events-per-run 10 \
  --priority-mode \
  --ensemble-models gfs_seamless,icon_seamless,nws_forecast \
  --depth-levels 5
```

### 第2步：创建 launchd 服务

```xml
<!-- ~/Library/LaunchAgents/com.weather.model_scan_hourly.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.weather.model_scan_hourly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>/Users/xiong/weather/scripts/model_scan_hourly.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Minute</key>
            <integer>5</integer>
        </dict>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/Users/xiong/weather/data/logs/model_scan_hourly.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/xiong/weather/data/logs/model_scan_hourly.err.log</string>
    <key>WorkingDirectory</key>
    <string>/Users/xiong/weather</string>
</dict>
</plist>
```

### 第3步：修改 daily_scan 为 6hourly_scan

```xml
<!-- 修改 ~/Library/LaunchAgents/com.weather.daily_scan.plist -->
<key>StartCalendarInterval</key>
<array>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>14</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <dict>
        <key>Hour</key>
        <integer>20</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
</array>
```

---

## 📊 预期效果

### API 调用量对比

| 方案 | 每小时调用 | 每天调用 | 风险 |
|------|-----------|---------|------|
| 当前（每天1次） | 0 | ~200 | 低，但数据陈旧 |
| 方案A（分层） | ~15 | ~360 | 中，可控 |
| 每小时全量 | ~200 | ~4800 | 高，可能限流 |

### 数据新鲜度对比

| 方案 | 最大陈旧时间 | 平均陈旧时间 | fresh_5min % |
|------|-------------|-------------|--------------|
| 当前 | 24小时 | 12小时 | 19.6% |
| 方案A | 1小时 | 30分钟 | > 80% |

### 拒绝率预期

| 拒绝原因 | 当前 | 方案A |
|----------|------|-------|
| market_tick_stale | 36.03% | < 5% |
| 总拒绝率 | 78.67% | < 25% |
| 实际交易机会 | 21.33% | > 75% |

---

## ⚠️ 风险控制

### Open-Meteo API 限流
- **免费版限流**: 10,000 次/天
- **方案A调用量**: ~360 次/天
- **安全余量**: 96.4%

### 监控指标
```bash
# 每天检查API调用量
sqlite3 data/archive.db "
SELECT
  date(created_at) as date,
  COUNT(*) as api_calls
FROM model_request_cooldown
WHERE created_at > datetime('now', '-7 days')
GROUP BY date(created_at)
ORDER BY date DESC;
"
```

### 降级策略
如果触发限流：
1. 立即停止每小时扫描
2. 恢复每6小时扫描
3. 增加缓存TTL

---

## 🚀 实施步骤

### 立即执行（今晚）
1. 创建 `model_scan_hourly.sh`
2. 创建 launchd 服务
3. 加载服务并测试

### 明天执行
1. 监控API调用量
2. 监控数据新鲜度
3. 监控拒绝率变化

### 本周执行
1. 修改 daily_scan 为 6hourly_scan
2. 优化优先级选择逻辑
3. 调整扫描事件数量

---

## 📝 配置建议

### 每小时扫描配置
```bash
# .env
PM_SCAN_HOURLY_MAX_EVENTS=10           # 限制事件数
PM_SCAN_HOURLY_PRIORITY_ONLY=1         # 只扫描优先级事件
PM_SCAN_HOURLY_TIMEOUT_SEC=300         # 5分钟超时
```

### 优先级定义
```python
# 优先级排序
1. has_position = True
2. has_open_orders = True
3. edge_lcb > PM_EDGE_POLICY_PATH_LCB_MIN
4. tick_age < 3600  # 市场报价新鲜
5. days_until_close < 2  # 即将结算
```

---

**推荐**: 先实施每小时扫描，观察1-2天后再决定是否增加6小时全量扫描
