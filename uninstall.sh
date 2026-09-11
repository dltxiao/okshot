#!/usr/bin/env bash
# Remove the macshot keybindings, launcher, icon and desktop entry.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

echo "==> 移除快捷键"
python3 -m macshot.hotkeys remove || true

echo "==> 移除启动器与桌面项"
[ -L "$BIN_DIR/macshot" ] && rm -f "$BIN_DIR/macshot" || true
rm -f "$APPS_DIR/macshot.desktop" "$ICON_DIR/macshot.svg"
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true

echo "完成（配置 ~/.config/macshot 与日志 ~/.cache/macshot 未删除）。"
