# edge_realizable 自动训练系统

## 概述

自动训练系统每周从历史订单数据中学习，生成动态的 edge_realizable 校准模型，实现：
- **动态概率**：根据市场条件（spread、depth、时间）调整成交概率
- **自动更新**：每周自动重新训练，适应市场变化
- **版本管理**：自动备份旧模型，支持回滚

---

## 系统架构

```
历史订单数据 (edge_trades)
    ↓
训练脚本 (train_edge_realizable_calibration.py)
    ↓
校准文件 (edge_realizable_calibration.json)
    ↓
交易系统 (edge_realizable_v2)
    ↓
新订单数据
    ↓
[循环]
```

---

## 文件说明

### 1. 训练脚本

**文件**: `scripts/train_edge_realizable_calibration.py`

**功能**:
- 从数据库提取历史订单
- 按市场条件分桶统计
- 计算成交率和收敛率
- 生成校准文件

**使用**:
```bash
# 基本用法
python3 scripts/train_edge_realizable_calibration.py

# 自定义参数
python3 scripts/train_edge_realizable_calibration.py \
  --days 90 \
  --min-samples 20 \
  --db data/archive.db \
  --output data/edge_realizable_calibration.json

# 测试运行（不保存）
python3 scripts/train_edge_realizable_calibration.py --dry-run

# 静默模式
python3 scripts/train_edge_realizable_calibration.py --quiet
```

### 2. 自动化脚本

**文件**: `scripts/auto_train_edge_realizable.sh`

**功能**:
- 检查数据量
- 训练模型
- 验证质量
- 重启服务
- 生成报告

**使用**:
```bash
# 正常运行
./scripts/auto_train_edge_realizable.sh

# 强制训练（即使数据不足）
./scripts/auto_train_edge_realizable.sh --force
```

### 3. 定时任务配置

**文件**: `scripts/com.weather.edge_realizable_training.plist`

**功能**: macOS LaunchAgent 配置，每周日凌晨 3:00 自动训练

---

## 安装步骤

### 方法1：使用 LaunchAgent（推荐 macOS）

```bash
# 1. 复制配置文件
cp scripts/com.weather.edge_realizable_training.plist ~/Library/LaunchAgents/

# 2. 加载服务
launchctl load ~/Library/LaunchAgents/com.weather.edge_realizable_training.plist

# 3. 验证
launchctl list | grep edge_realizable

# 4. 查看下次运行时间
launchctl print user/$(id -u)/com.weather.edge_realizable_training
```

### 方法2：使用 cron

```bash
# 1. 编辑 crontab
crontab -e

# 2. 添加以下行（每周日 3:00）
0 3 * * 0 /Users/xiong/weather/scripts/auto_train_edge_realizable.sh >> /Users/xiong/weather/logs/cron_edge_realizable.log 2>&1

# 3. 验证
crontab -l
```

---

## 手动训练

### 立即训练

```bash
cd /Users/xiong/weather
./scripts/auto_train_edge_realizable.sh
```

### 测试训练（不部署）

```bash
python3 scripts/train_edge_realizable_calibration.py --dry-run
```

---

## 监控和维护

### 查看训练日志

```bash
# 最新日志
tail -f logs/edge_realizable_training_*.log

# 所有日志
ls -lt logs/edge_realizable_training_*.log
```

### 查看训练报告

```bash
# 最新报告
cat EDGE_REALIZABLE_TRAINING_REPORT_*.md

# 所有报告
ls -lt EDGE_REALIZABLE_TRAINING_REPORT_*.md
```

### 检查当前模型

```bash
# 查看模型信息
cat data/edge_realizable_calibration.json | python3 -m json.tool | head -30

# 提取关键指标
python3 -c "
import json
data = json.load(open('data/edge_realizable_calibration.json'))
print(f'版本: {data[\"version\"]}')
print(f'样本数: {data[\"sample_n\"]}')
print(f'p_fill_entry: {data[\"p_fill_entry_default\"]}')
print(f'p_converge: {data[\"p_converge_default\"]}')
print(f'buckets: {len(data[\"p_fill_entry_buckets\"])}')
"
```

### 回滚到旧版本

```bash
# 查看备份
ls -lt data/edge_realizable_calibration.*.bak

# 回滚
cp data/edge_realizable_calibration.20260424_160000.bak data/edge_realizable_calibration.json

# 重启服务
launchctl stop com.weather.trade_engine
launchctl start com.weather.trade_engine
```

---

## 训练要求

### 最小数据量

- **推荐**: 500+ 历史订单
- **最低**: 100 历史订单
- **当前**: 3 订单（不足，使用默认值）

### 数据质量

需要的字段：
- `spread`: 市场价差
- `depth_ratio`: 深度比
- `days_to_settle`: 距离结算天数
- `exec_edge_entry`: 可执行 edge
- `position_size`: 持仓大小（判断是否成交）
- `realized_pnl`: 实现盈亏（判断是否盈利）

### 训练频率

- **推荐**: 每周一次
- **最低**: 每月一次
- **最高**: 每天一次（数据变化快时）

---

## 校准文件格式

### 结构

```json
{
  "generated_at": "2026-04-24T16:00:00Z",
  "version": "v1_90days_20260424",
  "sample_n": 1234,

  "p_fill_entry_default": 0.82,
  "p_converge_default": 0.87,
  "p_fill_exit_default": 0.82,

  "p_fill_entry_buckets": {
    "d1|s1|r2|e2|x1|ot:ANY": {
      "p": 0.85,
      "hit": 42,
      "total": 50
    }
  },

  "p_converge_buckets": { ... },
  "p_fill_exit_buckets": { ... },

  "hold_cost_params": {
    "mode": "linear",
    "k_per_hour": 0
  }
}
```

### Bucket Key 格式

```
d{days_idx}|s{spread_idx}|r{depth_idx}|e{edge_idx}|x{delta_idx}|ot:{order_type}

示例：
d1|s1|r2|e2|x1|ot:ANY

含义：
- d1: days_to_settle 在 [1.0, 2.0) 范围
- s1: spread 在 [0.01, 0.03) 范围
- r2: depth_ratio 在 [1.0, 2.0) 范围
- e2: edge_val 在 [0.03, 0.06) 范围
- x1: delta_exit 在第1个桶
- ot:ANY: 任意订单类型
```

---

## 效果评估

### 对比指标

| 指标 | 固定值 | 动态值（预期） |
|------|--------|----------------|
| p_realize | 0.544 | 0.40-0.75 |
| 好市场 edge_v2 | exec_edge × 0.544 | exec_edge × 0.70 |
| 差市场 edge_v2 | exec_edge × 0.544 | exec_edge × 0.30 |
| 通过率 | 8-9% | 6-10% |
| 决策质量 | 中等 | 更精准 |

### 预期改进

1. **更精准的风险评估**
   - 好市场：保留更多 edge
   - 差市场：更保守

2. **更好的资源分配**
   - 优先交易高质量市场
   - 避免低质量市场

3. **适应市场变化**
   - 自动学习新模式
   - 调整概率估计

---

## 故障排查

### 问题1：训练失败

**症状**: 脚本报错退出

**检查**:
```bash
# 查看日志
tail -50 logs/edge_realizable_training_*.log

# 检查数据库
sqlite3 data/archive.db "SELECT COUNT(*) FROM edge_trades"

# 测试 Python 脚本
python3 scripts/train_edge_realizable_calibration.py --dry-run
```

### 问题2：数据不足

**症状**: "样本数不足" 警告

**解决**:
```bash
# 检查数据量
sqlite3 data/archive.db "
SELECT COUNT(*) FROM edge_trades
WHERE entry_at > datetime('now', '-90 days')
"

# 强制训练（使用默认值）
./scripts/auto_train_edge_realizable.sh --force
```

### 问题3：模型异常

**症状**: p_fill_entry 或 p_converge 异常（< 0.3 或 > 0.95）

**解决**:
```bash
# 回滚到旧版本
cp data/edge_realizable_calibration.*.bak data/edge_realizable_calibration.json

# 检查数据质量
sqlite3 data/archive.db "
SELECT * FROM edge_trades
WHERE entry_at > datetime('now', '-7 days')
LIMIT 10
"
```

### 问题4：定时任务未运行

**检查 LaunchAgent**:
```bash
# 查看状态
launchctl list | grep edge_realizable

# 查看日志
tail -50 logs/edge_realizable_training.log

# 重新加载
launchctl unload ~/Library/LaunchAgents/com.weather.edge_realizable_training.plist
launchctl load ~/Library/LaunchAgents/com.weather.edge_realizable_training.plist
```

**检查 cron**:
```bash
# 查看 crontab
crontab -l

# 查看日志
tail -50 logs/cron_edge_realizable.log
```

---

## 卸载

### 停止定时任务

```bash
# LaunchAgent
launchctl unload ~/Library/LaunchAgents/com.weather.edge_realizable_training.plist
rm ~/Library/LaunchAgents/com.weather.edge_realizable_training.plist

# cron
crontab -e
# 删除相关行
```

### 恢复默认配置

```bash
# 使用固定默认值
cat > data/edge_realizable_calibration.json << 'EOF'
{
  "generated_at": "2026-04-24T00:00:00Z",
  "version": "default",
  "p_fill_entry_default": 0.80,
  "p_converge_default": 0.85,
  "p_fill_exit_default": 0.80,
  "p_fill_entry_buckets": {},
  "p_converge_buckets": {},
  "p_fill_exit_buckets": {},
  "hold_cost_params": {
    "mode": "linear",
    "k_per_hour": 0
  }
}
EOF

# 重启服务
launchctl restart com.weather.trade_engine
```

---

## 总结

### 当前状态

- ✅ 训练脚本已创建
- ✅ 自动化脚本已创建
- ✅ 定时任务配置已创建
- ⏳ 等待数据积累（当前 3 条，需要 100+）

### 下一步

1. **短期（1-2周）**: 继续使用固定默认值，收集数据
2. **中期（1-3个月）**: 数据足够后，首次训练动态模型
3. **长期（持续）**: 每周自动训练，持续优化

### 关键指标

- **数据量**: 当前 3 → 目标 500+
- **训练频率**: 每周日 3:00
- **模型版本**: 自动管理，保留最近 10 个备份

---

**文档更新时间**: 2026-04-24 17:00
**下次检查**: 数据量达到 100 时
