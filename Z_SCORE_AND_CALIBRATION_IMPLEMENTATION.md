# z-score 降低和自动校准训练实施报告

## 实施日期
2026-04-24

## 任务 1: 降低 z-score 至 1.28

### 修改内容

**文件**: `/Users/xiong/weather/.env`

**添加配置**:
```bash
# === Signal LCB 优化 (2026-04-24) ===
# 降低 z-score 从 1.64 (95% 置信度) 到 1.28 (90% 置信度)
# 预期通过率从 42.9% 提升至 ~55%
PM_EDGE_TRADEABILITY_SIGNAL_Z=1.28
```

### 预期效果

**当前状态 (z=1.64)**:
- 通过率: 42.9%
- LCB 惩罚: alpha - 0.164 * sigma
- 需要 alpha ≥ 0.514 才能通过 lcb ≥ 0.35

**修改后 (z=1.28)**:
- 预期通过率: ~55%
- LCB 惩罚: alpha - 0.128 * sigma
- 需要 alpha ≥ 0.478 才能通过 lcb ≥ 0.35

**改善**:
- 通过率提升: +12.1 百分点
- LCB 惩罚降低: 22%
- 更多市场变得可交易

### 生效方式

配置已添加到 `.env` 文件，需要重启运行中的进程才能生效。

**检测到的运行中进程**:
- PID 35423: auto-trade-job-worker
- PID 37174: market-refresh-only
- PID 22503: scan-weather-today
- PID 41396: auto-trade-job-worker

**重启方法**:
```bash
# 方法 1: 如果使用 supervisor/pm2
supervisorctl restart all
# 或
pm2 restart all

# 方法 2: 手动重启
pkill -f "scan_weather_market.py"
# 然后重新启动你的服务
```

**验证生效**:
```bash
# 查看决策日志，确认 z-score 已更新
tail -f logs/combined-*.log | grep "signal_z"
```

---

## 任务 2: 设置定期校准训练

### 创建的文件

#### 1. 训练脚本
**文件**: `/Users/xiong/weather/scripts/daily_calibration_training.sh`

**功能**:
- 每天训练 Platt 校准模型 (q_raw → q_calibrated)
- 每天训练 Quote 校准模型 (q_calibrated → q_edge)
- 使用最近 60 天数据（从 30 天增加到 60 天）
- 训练成功后自动 Promote 到生产环境
- 记录详细日志
- 自动清理 30 天前的旧日志

**执行权限**: 已添加 (`chmod +x`)

#### 2. 安装文档
**文件**: `/Users/xiong/weather/CRON_CALIBRATION_SETUP.md`

**内容**: 详细的 crontab 配置说明和监控指南

### 安装 Crontab

**步骤 1**: 编辑 crontab
```bash
crontab -e
```

**步骤 2**: 添加以下行
```bash
# 每日校准模型训练 (Platt + Quote)
0 2 * * * /Users/xiong/weather/scripts/daily_calibration_training.sh >> /Users/xiong/weather/logs/cron_calibration.log 2>&1
```

**步骤 3**: 保存并验证
```bash
crontab -l
```

### 手动测试

在安装 crontab 之前，建议先手动运行测试：

```bash
cd /Users/xiong/weather
./scripts/daily_calibration_training.sh
```

检查输出，确保：
1. 训练脚本能正常运行
2. 有足够的已结算事件数据
3. 训练成功并生成 bundle

### 预期效果

**当前状态**:
- Platt 校准: 48 个样本 (2026-04-13 训练，11 天前)
- Quote 校准: 37 个样本 (2026-04-13 训练，11 天前)
- 训练窗口: 30 天

**修改后**:
- 训练频率: 每天凌晨 2 点
- 训练窗口: 60 天（增加 1 倍）
- 样本增长: 每天自动累计新的已结算事件
- 自动部署: 训练成功后自动 Promote

**预计 1-2 周后**:
- Platt 校准: 100+ 个样本
- Quote 校准: 80+ 个样本
- 校准质量: 随样本增加而改善

### 监控和日志

**主日志**:
```bash
tail -f /Users/xiong/weather/logs/calibration_training.log
```

**Cron 日志**:
```bash
tail -f /Users/xiong/weather/logs/cron_calibration.log
```

**详细训练日志**:
```bash
ls -lt /Users/xiong/weather/logs/calibration_training_*.log | head -5
```

### 监控指标

定期检查：
1. **训练成功率**: 是否每天都成功训练
2. **样本数增长**: 是否持续增加
3. **评估指标**: Brier Score, ECE 是否保持稳定或改善
4. **Promote 成功**: 是否成功部署到生产环境

---

## 总结

### 已完成

1. ✅ 降低 z-score 从 1.64 → 1.28
   - 配置已添加到 `.env`
   - 需要重启进程生效

2. ✅ 创建每日校准训练脚本
   - 脚本: `scripts/daily_calibration_training.sh`
   - 文档: `CRON_CALIBRATION_SETUP.md`
   - 训练窗口: 60 天
   - 自动 Promote

### 待执行

1. ⏳ 重启交易进程（使 z-score 生效）
   ```bash
   # 根据你的进程管理方式选择
   supervisorctl restart all
   # 或
   pm2 restart all
   # 或手动重启
   ```

2. ⏳ 安装 crontab（启用每日训练）
   ```bash
   crontab -e
   # 添加: 0 2 * * * /Users/xiong/weather/scripts/daily_calibration_training.sh >> /Users/xiong/weather/logs/cron_calibration.log 2>&1
   ```

3. ⏳ 手动测试训练脚本（可选但推荐）
   ```bash
   cd /Users/xiong/weather
   ./scripts/daily_calibration_training.sh
   ```

### 预期改善

**短期 (1-2 天)**:
- 通过率从 42.9% 提升至 ~55%
- 更多市场变得可交易
- 决策数量增加

**中期 (1-2 周)**:
- 校准样本数增长至 100+
- 校准质量改善
- 预测准确性提升

**长期 (1-2 月)**:
- 持续累计数据
- 校准模型越来越准确
- 系统整体性能提升

---

**实施时间**: 2026-04-24
**下次检查**: 2026-04-25（验证 z-score 生效和首次训练结果）
