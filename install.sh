#!/usr/bin/env bash
# macshot installer.
#
#   ./install.sh            install the launcher, the desktop entry and the
#                           Alt+Shift+3/4/5 (Ctrl = clipboard only) keybindings
#   ./install.sh --keys     only (re)install the keybindings
#   ./uninstall.sh          undo everything
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$PROJECT_DIR/macshot.py"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "缺少 python3"
python3 -c 'import gi; gi.require_version("Gtk", "4.0")' 2>/dev/null \
  || die "缺少 GTK4 的 Python 绑定（sudo apt install python3-gi gir1.2-gtk-4.0）"
python3 -c 'import cairo' 2>/dev/null \
  || echo "提示：未检测到 python3-cairo，截图覆盖层不需要它，可忽略。"

[ -f "$LAUNCHER" ] || die "找不到 $LAUNCHER"
chmod +x "$LAUNCHER"

if [ "${1:-}" != "--keys" ]; then
  echo "==> 安装启动器到 $BIN_DIR/macshot"
  mkdir -p "$BIN_DIR"
  ln -sf "$LAUNCHER" "$BIN_DIR/macshot"

  echo "==> 安装图标与桌面项"
  mkdir -p "$ICON_DIR" "$APPS_DIR"
  install -m 644 "$PROJECT_DIR/packaging/macshot.svg" "$ICON_DIR/macshot.svg"
  sed "s|@EXEC@|$LAUNCHER|g" "$PROJECT_DIR/packaging/macshot.desktop.in" \
    > "$APPS_DIR/macshot.desktop"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true

  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "提示：$BIN_DIR 不在 PATH 中，直接使用绝对路径 $LAUNCHER 即可。" ;;
  esac
fi

echo "==> 安装快捷键"
python3 -m macshot.hotkeys install

echo
echo "完成。快捷键："
echo "  Alt+Shift+3  全屏截图        Ctrl+Alt+Shift+3  全屏 → 剪贴板"
echo "  Alt+Shift+4  区域截图        Ctrl+Alt+Shift+4  区域 → 剪贴板"
echo "  Alt+Shift+5  窗口截图        Ctrl+Alt+Shift+5  窗口 → 剪贴板"
echo
echo "区域截图：拖动选择 → 松开后出现标注工具栏（矩形/圆圈/箭头/画笔/马赛克/文字，"
echo "          快捷键 1-7，Ctrl+Z 撤销，+/- 调粗细，颜色可选），方向键微调选区，"
echo "          回车或点击选区内部保存，Esc 取消，空格切换到窗口选择。"
echo "截图保存在 ~/Desktop，文件名形如 'Screenshot 2026-02-14 at 15.04.05.png'。"
echo "日志：~/.cache/macshot/macshot.log"
