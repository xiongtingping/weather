#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="/usr/local/libexec/weather-service-control"

usage() {
  echo "用法：bash scripts/weather_service_control.sh restart {monitor|bot}" >&2
}

if [ "$#" -ne 2 ] || [ "$1" != "restart" ]; then
  usage
  exit 64
fi

case "$2" in
  monitor) action="restart-monitor" ;;
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

if [ ! -x "$HELPER" ]; then
  echo "免密控制入口尚未安装。请执行一次：" >&2
  echo "cd '$ROOT' && sudo bash scripts/install_passwordless_service_control.sh" >&2
  exit 1
fi

# -n is intentional: daily control must fail closed instead of prompting for a
# password when the sudoers rule is missing or does not match exactly.
if /usr/bin/sudo -n "$HELPER" "$action"; then
  echo "已重启 monitor 守护进程。"
else
  rc=$?
  echo "monitor 免密重启失败；不会回退到交互式 sudo。请重新执行一次安装命令：" >&2
  echo "cd '$ROOT' && sudo bash scripts/install_passwordless_service_control.sh" >&2
  exit "$rc"
fi
