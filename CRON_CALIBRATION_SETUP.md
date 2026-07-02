# 每日校准模型训练 - Crontab 配置

## 安装说明

1. 编辑 crontab：
```bash
crontab -e
```

2. 添加以下行（每天凌晨 2 点运行）：
```bash
# 每日校准模型训练 (Platt + Quote)
0 2 * * * /Users/xiong/weather/scripts/daily_calibration_training.sh >> /Users/xiong/weather/logs/cron_calibration.log 2>&1
```

3. 保存并退出

4. 验证 crontab 已添加：
```bash
crontab -l
```

## 手动运行测试

```bash
cd /Users/xiong/weather
./scripts/daily_calibration_training.sh
```

## 查看日志

```bash
# 查看主日志
tail -f /Users/xiong/weather/logs/calibration_training.log

# 查看 cron 日志
tail -f /Users/xiong/weather/logs/cron_calibration.log

# 查看详细训练日志
ls -lt /Users/xiong/weather/logs/calibration_training_*.log | head -5
```

## 预期效果

- **训练频率**: 每天凌晨 2 点
- **训练窗口**: 最近 60 天数据
- **样本增长**: 每天自动累计新的已结算事件
- **自动 Promote**: 训练成功后自动部署到生产环境

## 当前状态

- Platt 校准: 48 个样本 (2026-04-13 训练)
- Quote 校准: 37 个样本 (2026-04-13 训练)

预计 1-2 周后样本数将增长至 100+ 个。

## 监控建议

定期检查训练日志，确保：
1. 训练成功完成
2. 样本数持续增长
3. 评估指标（Brier Score, ECE）保持稳定或改善

## 故障排查

如果训练失败，检查：
1. 数据库连接是否正常
2. event_runs 表是否有新的 settlement_actual 数据
3. Python 环境是否正确
4. 磁盘空间是否充足
