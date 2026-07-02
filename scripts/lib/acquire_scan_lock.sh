#!/bin/bash
# 全局扫描锁机制
# 用法: source scripts/lib/acquire_scan_lock.sh

SCAN_LOCK_DIR="${ROOT:-/Users/xiong/weather}/data/scan_global.lock"
SCAN_LOCK_PID_FILE="$SCAN_LOCK_DIR/pid"
SCAN_LOCK_OWNER_FILE="$SCAN_LOCK_DIR/owner"
SCAN_LOCK_ACQUIRED=0

acquire_scan_lock() {
  local owner="${1:-unknown}"
  local wait_sec="${2:-0}"
  local poll_sec="${3:-2}"

  local waited=0
  while ! mkdir "$SCAN_LOCK_DIR" 2>/dev/null; do
    local lock_pid=""
    local lock_owner=""

    if [ -f "$SCAN_LOCK_PID_FILE" ]; then
      lock_pid="$(cat "$SCAN_LOCK_PID_FILE" 2>/dev/null || true)"
    fi
    if [ -f "$SCAN_LOCK_OWNER_FILE" ]; then
      lock_owner="$(cat "$SCAN_LOCK_OWNER_FILE" 2>/dev/null || true)"
    fi

    # 检查进程是否还在运行
    if [ -n "$lock_pid" ] && ! kill -0 "$lock_pid" 2>/dev/null; then
      # 进程已死，清理锁
      rm -rf "$SCAN_LOCK_DIR" 2>/dev/null || true
      continue
    fi

    # 如果不等待，直接返回失败
    if [ "$wait_sec" -le 0 ]; then
      echo "[scan_lock] Another scan is running (owner=${lock_owner:-unknown} pid=${lock_pid:-unknown}), skip" >&2
      return 1
    fi

    # 等待锁释放
    if [ "$waited" -eq 0 ]; then
      echo "[scan_lock] Waiting for scan lock (owner=${lock_owner:-unknown} pid=${lock_pid:-unknown} max=${wait_sec}s)" >&2
    fi

    if [ "$waited" -ge "$wait_sec" ]; then
      echo "[scan_lock] Lock timeout after ${waited}s (owner=${lock_owner:-unknown} pid=${lock_pid:-unknown})" >&2
      return 1
    fi

    sleep "$poll_sec"
    waited=$((waited + poll_sec))
  done

  # 获取锁成功
  echo "$$" > "$SCAN_LOCK_PID_FILE"
  echo "$owner" > "$SCAN_LOCK_OWNER_FILE" 2>/dev/null || true
  SCAN_LOCK_ACQUIRED=1

  if [ "$waited" -gt 0 ]; then
    echo "[scan_lock] Lock acquired after ${waited}s wait (owner=$owner)" >&2
  fi

  return 0
}

release_scan_lock() {
  if [ "$SCAN_LOCK_ACQUIRED" = "1" ]; then
    local cur_pid=""
    if [ -f "$SCAN_LOCK_PID_FILE" ]; then
      cur_pid="$(cat "$SCAN_LOCK_PID_FILE" 2>/dev/null || true)"
    fi

    # 只有当前进程才能释放锁
    if [ -z "$cur_pid" ] || [ "$cur_pid" = "$$" ]; then
      rm -rf "$SCAN_LOCK_DIR" 2>/dev/null || true
      SCAN_LOCK_ACQUIRED=0
    fi
  fi
}

# 自动在脚本退出时释放锁
trap release_scan_lock EXIT
