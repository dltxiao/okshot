"""GNOME custom keybinding management for macshot.

Wayland gives applications no way to grab global shortcuts themselves, so the
supported route is a ``org.gnome.settings-daemon.plugins.media-keys`` custom
keybinding that runs the ``macshot`` launcher.  This module adds/removes those
entries without touching keybindings owned by other applications.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
PREFIX = "macshot-"

#: (id, label, accelerator, cli arguments)
BINDINGS = (
    ("macshot-full", "macshot：全屏截图", "<Alt><Shift>3", "full"),
    ("macshot-region", "macshot：区域截图", "<Alt><Shift>4", "region"),
    ("macshot-window", "macshot：窗口截图", "<Alt><Shift>5", "window"),
    ("macshot-full-clipboard", "macshot：全屏截图到剪贴板",
     "<Ctrl><Alt><Shift>3", "full --clipboard"),
    ("macshot-region-clipboard", "macshot：区域截图到剪贴板",
     "<Ctrl><Alt><Shift>4", "region --clipboard"),
    ("macshot-window-clipboard", "macshot：窗口截图到剪贴板",
     "<Ctrl><Alt><Shift>5", "window --clipboard"),
)

#: schemas that may compete with the accelerators above
CONFLICT_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
)


def _media_keys() -> Gio.Settings:
    return Gio.Settings.new(MEDIA_KEYS_SCHEMA)


def launcher_path() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "macshot.py")


def _entry(path: str) -> Gio.Settings:
    return Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)


def active_bindings() -> list[tuple[str, str, str, str]]:
    """Return (path, name, command, binding) for every macshot entry."""
    found = []
    for path in _media_keys().get_strv("custom-keybindings"):
        if PREFIX not in path:
            continue
        settings = _entry(path)
        found.append((path, settings.get_string("name"),
                      settings.get_string("command"), settings.get_string("binding")))
    return found


def find_conflicts() -> list[str]:
    """Accelerators that other GNOME keybindings already use."""
    wanted = {binding for _id, _label, binding, _args in BINDINGS}
    conflicts = []
    for schema_id in CONFLICT_SCHEMAS:
        try:
            schema = Gio.SettingsSchemaSource.get_default().lookup(schema_id, True)
        except Exception:
            continue
        if schema is None:
            continue
        settings = Gio.Settings.new(schema_id)
        for key in schema.list_keys():
            if settings.get_value(key).get_type_string() != "as":
                continue
            for binding in settings.get_strv(key):
                if binding in wanted:
                    conflicts.append(f"{schema_id} {key} = {binding}")
    return conflicts


def install(launcher: str | None = None, *, verbose: bool = True) -> list[str]:
    launcher = launcher or launcher_path()
    media_keys = _media_keys()
    existing = [p for p in media_keys.get_strv("custom-keybindings") if PREFIX not in p]
    added = [f"{BASE_PATH}{name}/" for name, _label, _binding, _args in BINDINGS]
    media_keys.set_strv("custom-keybindings", existing + added)
    for (name, label, binding, args), path in zip(BINDINGS, added):
        settings = _entry(path)
        settings.set_string("name", label)
        settings.set_string("command", f"{launcher} {args}")
        settings.set_string("binding", binding)
        if verbose:
            print(f"  {binding:22s} -> {label}")
    Gio.Settings.sync()
    return added


def remove(*, verbose: bool = True) -> None:
    media_keys = _media_keys()
    kept = []
    removed = []
    for path in media_keys.get_strv("custom-keybindings"):
        if PREFIX in path:
            removed.append(path)
            for key in ("name", "command", "binding"):
                _entry(path).reset(key)
        else:
            kept.append(path)
    media_keys.set_strv("custom-keybindings", kept)
    Gio.Settings.sync()
    if verbose:
        print(f"removed {len(removed)} macshot keybinding(s)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv[0] if argv else "show"
    if action == "install":
        conflicts = find_conflicts()
        if conflicts:
            print("警告：以下快捷键已被系统占用，请改用其它组合或先解除占用：")
            for line in conflicts:
                print(f"  - {line}")
        install()
        print("macshot 快捷键已安装（Alt+Shift+3/4/5，加 Ctrl 表示只复制到剪贴板）")
    elif action == "remove":
        remove()
    elif action == "show":
        entries = active_bindings()
        if not entries:
            print("macshot 快捷键未安装")
        for path, name, command, binding in entries:
            print(f"{binding:22s} {name}  ({command})")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
