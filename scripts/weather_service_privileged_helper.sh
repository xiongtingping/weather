#!/bin/sh
set -efu

# This file is installed as a root-owned executable. Keep the accepted command
# surface closed: sudoers delegates only these exact argv combinations.
if [ "$#" -ne 1 ]; then
  echo "usage: weather-service-control restart-monitor" >&2
  exit 64
fi

case "$1" in
  restart-monitor)
    exec /bin/launchctl kickstart -k system/com.weather.monitor.daemon
    ;;
  *)
    echo "weather-service-control: action not allowed: $1" >&2
    exit 64
    ;;
esac
