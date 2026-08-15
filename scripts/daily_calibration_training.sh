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

# 3. 分档概率校准漂移巡检（只读报告，不改表；表常数在
#    bayes_basket_target/cross_consensus.py，够格换表要走人工评审）
echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "[$(date +%H:%M:%S)] 分档校准 (band_reliability) 漂移巡检..." | tee -a "$LOG_DIR/calibration_training.log"
BAND_REPORT="$("$PY" scripts/refit_band_reliability.py 2>&1)" || true
echo "$BAND_REPORT" >> "$LOG_DIR/calibration_training_$TIMESTAMP.log"
if echo "$BAND_REPORT" | grep -q '"table_change_suggested": true'; then
  echo "[$(date +%H:%M:%S)] ⚠ 分档校准表与最新证据有漂移，详见 calibration_training_$TIMESTAMP.log" | tee -a "$LOG_DIR/calibration_training.log"
else
  echo "[$(date +%H:%M:%S)] ✓ 分档校准表仍被证据支撑" | tee -a "$LOG_DIR/calibration_training.log"
fi

echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"
echo "每日校准模型训练完成: $(date +%Y%m%d_%H%M%S)" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"

# 清理 30 天前的日志
find "$LOG_DIR" -name "calibration_training_*.log" -mtime +30 -delete 2>/dev/null || true
