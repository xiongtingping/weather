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

# 3. 分档概率校准自动换表（拟合+激活；准入规则在
#    bayes_basket_target/band_reliability_store.py，规则不变表自动变。
#    已服务的工件钉住各自评审时的表版本，换表不改历史工件的数学）
echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "[$(date +%H:%M:%S)] 分档校准 (band_reliability) 自动重拟合..." | tee -a "$LOG_DIR/calibration_training.log"
#    各 stage 独立拟合，一个失败不带走其余；成败以退出码为准而不是
#    以有没有 SUMMARY 行为准——部分失败时失败的 stage 也会打自己那行
#    SUMMARY（标 FAILED），照旧按有无 SUMMARY 判就会把部分失败读成成功。
BAND_RC=0
BAND_REPORT="$("$PY" scripts/refit_band_reliability.py --commit --summary 2>&1)" || BAND_RC=$?
echo "$BAND_REPORT" >> "$LOG_DIR/calibration_training_$TIMESTAMP.log"
if echo "$BAND_REPORT" | grep -q '^SUMMARY '; then
  echo "$BAND_REPORT" | grep '^SUMMARY ' | sed 's/^SUMMARY /    /' | tee -a "$LOG_DIR/calibration_training.log"
fi
if [ "$BAND_RC" -eq 0 ]; then
  echo "[$(date +%H:%M:%S)] ✓ 三阶段分档校准已按最新证据重拟合" | tee -a "$LOG_DIR/calibration_training.log"
else
  echo "[$(date +%H:%M:%S)] ✗ 分档校准重拟合失败 (退出码 $BAND_RC)，详见 calibration_training_$TIMESTAMP.log" | tee -a "$LOG_DIR/calibration_training.log"
fi

# 4. 分布中心/宽度两标量自动换代（拟合+激活；准入是留出前向窗，
#    规则在 bayes_basket_target/center_width_store.py）。
#
#    CW_SINCE 是服务算术最后一次改变的**时刻**（ISO-8601，非日期），
#    **改服务算术时必须同步前移**。下界必须落在 decision_at_utc 上而不是
#    event_date 上：P0 对两天后的事件提前两天决策，所以按 event_date 卡会把
#    本该排除的决策放进来——实测 event_date 2026-08-19 的决策最早来自 08-17。
#    这两个标量度量的是"服务出去的分布有多错"，所以那一天之前的日子描述的
#    是没人再服务的分布。不设下界时旧日子会压倒新日子，让这一层把它本来在
#    补偿的东西原样重新拟合出来：2026-08-18 把宽度改成按扣偏差后的 σ 定标
#    之后，无界拟合与 21 天滚动窗都原样复现了旧的 λ=0.825，也就是那个缺陷
#    的补偿值。滚动窗解决不了这个问题——它要等窗口自然走过变更日才干净。
#    锚定到变更日则当天就干净：证据不足时这一层自己回落到恒等并攒够再上。
CW_SINCE="2026-08-18T09:20:00Z"
echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "[$(date +%H:%M:%S)] 中心/宽度 (center_width) 自动重拟合 (证据自 $CW_SINCE 起)..." | tee -a "$LOG_DIR/calibration_training.log"
CW_REPORT="$("$PY" scripts/refit_center_width.py --commit --summary --since "$CW_SINCE" 2>&1)" || true
echo "$CW_REPORT" >> "$LOG_DIR/calibration_training_$TIMESTAMP.log"
if echo "$CW_REPORT" | grep -q '^SUMMARY '; then
  echo "$CW_REPORT" | grep '^SUMMARY ' | sed 's/^SUMMARY /    /' | tee -a "$LOG_DIR/calibration_training.log"
  echo "[$(date +%H:%M:%S)] ✓ 三阶段中心/宽度已按最新证据重拟合" | tee -a "$LOG_DIR/calibration_training.log"
else
  echo "[$(date +%H:%M:%S)] ✗ 中心/宽度重拟合失败，详见 calibration_training_$TIMESTAMP.log" | tee -a "$LOG_DIR/calibration_training.log"
fi

echo "" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"
echo "每日校准模型训练完成: $(date +%Y%m%d_%H%M%S)" | tee -a "$LOG_DIR/calibration_training.log"
echo "========================================" | tee -a "$LOG_DIR/calibration_training.log"

# 清理 30 天前的日志
find "$LOG_DIR" -name "calibration_training_*.log" -mtime +30 -delete 2>/dev/null || true
