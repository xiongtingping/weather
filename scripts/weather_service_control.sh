#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "用法：bash scripts/weather_service_control.sh restart {monitor|bot}" >&2
}

if [ "$#" -ne 2 ] || [ "$1" != "restart" ]; then
  usage
  exit 64
fi

case "$2" in
  monitor)
    uid="$(id -u)"
    monitor_service="gui/${uid}/com.weather.monitor.daemon"
    bash "$ROOT/scripts/install_monitor_launchdaemon.sh"
    echo "已重启用户域 monitor 守护进程。"
    exit 0
    ;;
  bot)
    uid="$(id -u)"
    bot_service="gui/${uid}/com.weather.trade_engine"
    if ! /bin/launchctl print "$bot_service" >/dev/null 2>&1; then
      echo "当前交易引擎尚未加载：$bot_service" >&2
      echo "请先执行：cd '$ROOT' && PM_LAUNCHD_ONLY=com.weather.trade_engine bash scripts/install_launchagent.sh" >&2
      exit 1
    fi
    /bin/launchctl kickstart -k "$bot_service"
    echo "已重启 bot 交易引擎。"
    exit 0
    ;;
  *) usage; exit 64 ;;
esac
