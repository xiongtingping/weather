#!/bin/bash
# 每日校准模型训练脚本
# 自动训练 Platt 和 Quote 校准模型，增加样本数

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime"
  exit 1
}

mkdir -p "$LOG_DIR"

echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"
echo "开始每日校准模型训练: $TIMESTAMP" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"

cd "$PROJECT_DIR"

# 1. 训练 Platt 校准模型 (q_raw → q_calibrated)
echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "[$(date +%H:%M:%S)] 训练 Platt 校准模型..." | tee -a "$LOG_DIR/calibration_training.log"

"$PY" scripts/train_q_calibrator.py \
  --days 60 \
  --write-ssot \
  2>&1 | tee -a "$LOG_DIR/calibration_training_$TIMESTAMP.log"

if [ $? -eq 0 ]; then
  echo "[$(date +%H:%M:%S)] ✓ Platt 校准训练成功" | tee -a "$LOG_DIR/calibration_training.log"

  # Promote 到生产环境
  echo "[$(date +%H:%M:%S)] Promote Platt 校准模型..." | tee -a "$LOG_DIR/calibration_training.log"
  "$PY" scripts/promote_q_calibrator.py 2>&1 | tee -a "$LOG_DIR/calibration_training_$TIMESTAMP.log"

  if [ $? -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] ✓ Platt 校准 Promote 成功" | tee -a "$LOG_DIR/calibration_training.log"
  else
    echo "[$(date +%H:%M:%S)] ✗ Platt 校准 Promote 失败" | tee -a "$LOG_DIR/calibration_training.log"
  fi
else
  echo "[$(date +%H:%M:%S)] ✗ Platt 校准训练失败" | tee -a "$LOG_DIR/calibration_training.log"
fi

# 2. 训练 Quote 校准模型 (q_calibrated → q_edge)
echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "[$(date +%H:%M:%S)] 训练 Quote 校准模型..." | tee -a "$LOG_DIR/calibration_training.log"

"$PY" scripts/train_q_quote_calibrator.py \
  --days 60 \
  2>&1 | tee -a "$LOG_DIR/calibration_training_$TIMESTAMP.log"

if [ $? -eq 0 ]; then
  echo "[$(date +%H:%M:%S)] ✓ Quote 校准训练成功" | tee -a "$LOG_DIR/calibration_training.log"

  # Promote 到生产环境
  echo "[$(date +%H:%M:%S)] Promote Quote 校准模型..." | tee -a "$LOG_DIR/calibration_training.log"
  "$PY" scripts/promote_q_quote_calibrator.py 2>&1 | tee -a "$LOG_DIR/calibration_training_$TIMESTAMP.log"

  if [ $? -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] ✓ Quote 校准 Promote 成功" | tee -a "$LOG_DIR/calibration_training.log"
  else
    echo "[$(date +%H:%M:%S)] ✗ Quote 校准 Promote 失败" | tee -a "$LOG_DIR/calibration_training.log"
  fi
else
  echo "[$(date +%H:%M:%S)] ✗ Quote 校准训练失败" | tee -a "$LOG_DIR/calibration_training.log"
fi

# 3/4. 分档校准 (band_reliability) 与中心/宽度 (center_width) 的每日重拟合
#    已于 2026-08-26 收编进常驻 worker 的托管扫描（scripts/
#    calibration_refit_sweep.sh，HOSTED_SWEEPS 里的 calibration_refit）。
#    动因：cron 在机器睡眠时静默跳过且不补跑，08-26 就实际丢了一轮，
#    两层的证据时钟随之停摆；托管调度是"到期即跑"，错过的分钟会在下一个
#    空闲缝隙补上。本脚本只保留 Platt/Quote 两段——它们没有在役消费者，
#    且 ~25 分钟的运行时长放进托管互斥会饿死生产周期。

echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"
echo "每日校准模型训练完成: $(date +%Y%m%d_%H%M%S)" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"

# 清理 30 天前的日志
find "$LOG_DIR" -name "calibration_training_*.log" -mtime +30 -delete 2>/dev/null || true
