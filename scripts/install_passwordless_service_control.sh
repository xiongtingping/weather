#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/scripts/weather_service_privileged_helper.sh"
HELPER_DIR="/usr/local/libexec"
HELPER="$HELPER_DIR/weather-service-control"
TARGET_USER="${SUDO_USER:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "需要一次管理员授权完成安装：" >&2
  echo "cd '$ROOT' && sudo bash scripts/install_passwordless_service_control.sh" >&2
  exit 1
fi

if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  echo "无法确定普通用户；请从 xiong 账户使用 sudo 运行本脚本。" >&2
  exit 1
fi
if [[ ! "$TARGET_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "拒绝不安全的用户名：$TARGET_USER" >&2
  exit 1
fi
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  echo "用户不存在：$TARGET_USER" >&2
  exit 1
fi
if [ ! -f "$SOURCE" ]; then
  echo "缺少固定 helper：$SOURCE" >&2
  exit 1
fi
/bin/sh -n "$SOURCE"

SUDOERS_FILE="/etc/sudoers.d/weather-service-control-${TARGET_USER}"
TMP_SUDOERS="$(mktemp /tmp/weather-service-control-sudoers.XXXXXX)"
trap 'rm -f "$TMP_SUDOERS"' EXIT

cat >"$TMP_SUDOERS" <<EOF
# Managed by $ROOT/scripts/install_passwordless_service_control.sh
# Only the root-owned helper and this exact argument are passwordless.
$TARGET_USER ALL=(root) NOPASSWD: $HELPER restart-monitor
EOF
chmod 0440 "$TMP_SUDOERS"
/usr/sbin/visudo -cf "$TMP_SUDOERS"

/usr/bin/install -d -o root -g wheel -m 0755 "$HELPER_DIR"
/usr/bin/install -o root -g wheel -m 0755 "$SOURCE" "$HELPER"
/usr/bin/install -o root -g wheel -m 0440 "$TMP_SUDOERS" "$SUDOERS_FILE"
/usr/sbin/visudo -cf "$SUDOERS_FILE"

helper_owner="$(/usr/bin/stat -f '%Su:%Sg' "$HELPER")"
helper_mode="$(/usr/bin/stat -f '%Lp' "$HELPER")"
if [ "$helper_owner" != "root:wheel" ] || [ "$helper_mode" != "755" ]; then
  echo "helper 权限校验失败：${helper_owner} mode=${helper_mode}" >&2
  exit 1
fi

echo "已为 $TARGET_USER 安装最小权限控制入口。"
echo "日常命令：bash '$ROOT/scripts/weather_service_control.sh' restart monitor"
echo "bot 属于用户域，无需 sudo：bash '$ROOT/scripts/weather_service_control.sh' restart bot"
