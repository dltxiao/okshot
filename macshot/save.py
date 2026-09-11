"""Saving, cropping, clipboard and feedback helpers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from .selection import Rect  # noqa: E402

log = logging.getLogger("macshot")

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "macshot"
LOG_PATH = CACHE_DIR / "macshot.log"


def cache_dir() -> Path:
    """Writable scratch space; falls back to the system temp dir."""
    global CACHE_DIR
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR
    except OSError:
        import tempfile

        CACHE_DIR = Path(tempfile.gettempdir()) / "macshot"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR


def default_save_dir() -> Path:
    """The desktop directory, falling back to the home directory."""
    try:
        import gi as _gi

        _gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        if path:
            return Path(path)
    except Exception:  # pragma: no cover - non GLib environments
        pass
    return Path.home()


def build_filename(template: str, when: datetime | None = None) -> str:
    """``Screenshot {date} at {time}.png`` -> ``Screenshot 2026-02-14 at 15.04.05.png``."""
    when = when or datetime.now()
    name = (template
            .replace("{date}", when.strftime("%Y-%m-%d"))
            .replace("{time}", when.strftime("%H.%M.%S"))
            .replace("{datetime}", when.strftime("%Y-%m-%d %H.%M.%S")))
    if not name.lower().endswith(".png"):
        name += ".png"
    return name


def unique_path(directory: Path, filename: str) -> Path:
    """Avoid clobbering: ``shot.png`` -> ``shot-2.png``."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free filename in {directory}")


def move_into(source: Path, directory: Path, filename: str) -> Path:
    """Move a freshly produced PNG into its final place, renaming it macOS style."""
    destination = unique_path(directory, filename)
    if source.resolve() == destination.resolve():  # pragma: no cover - same file
        return destination
    try:
        os.replace(source, destination)
    except OSError:
        # Different filesystem, or the portal's folder is not writable: copy
        # first and only then try to clean the original up.
        shutil.copy2(source, destination)
        try:
            source.unlink()
        except OSError as exc:
            log.warning("could not remove the portal's temporary file %s: %s", source, exc)
    return destination


def crop_png(source: Path, rect: Rect, destination: Path) -> Path:
    """Crop ``source`` to ``rect`` (pixels, top-left origin) and write a PNG."""
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(source))
    box = rect.round_to_ints()
    x = max(0, min(int(box.x), pixbuf.get_width() - 1))
    y = max(0, min(int(box.y), pixbuf.get_height() - 1))
    width = max(1, min(int(box.width), pixbuf.get_width() - x))
    height = max(1, min(int(box.height), pixbuf.get_height() - y))
    cropped = pixbuf.new_subpixbuf(x, y, width, height)
    destination.parent.mkdir(parents=True, exist_ok=True)
    cropped.savev(str(destination), "png", [], [])
    return destination


# --------------------------------------------------------------------- feedback

_SOUND_CANDIDATES = ("screen-capture", "camera-shutter")


def play_shutter_sound() -> None:
    """Play the desktop's screenshot sound, best effort."""
    for event in _SOUND_CANDIDATES:
        try:
            subprocess.Popen(
                ["canberra-gtk-play", "-i", event],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except FileNotFoundError:
            return


def notify(title: str, body: str = "", icon: str = "dialog-information") -> None:
    try:
        subprocess.Popen(["notify-send", "-a", "macshot", "-i", icon, title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:  # pragma: no cover
        pass


# -------------------------------------------------------------------- clipboard


def spawn_clipboard_holder(path: Path, hold_seconds: int) -> None:
    """Hand ``path`` to the clipboard in a detached helper process.

    GNOME's Wayland compositor only lets a focused client that has seen real
    input claim the selection, so a hotkey-launched process cannot do it
    directly.  The helper talks X11 (through XWayland) instead, where Mutter's
    X11<->Wayland clipboard bridge takes care of the rest.
    """
    command = [sys.executable, "-m", "macshot", "--clipboard-holder", str(path),
               "--hold", str(hold_seconds)]
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    # Pick the backend before the interpreter starts: Gdk reads GDK_BACKEND when
    # it opens its first display, and the X11 route through XWayland is the one
    # that works without keyboard focus.
    env["GDK_BACKEND"] = "x11" if env.get("DISPLAY") else "wayland"
    try:
        subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def hold_image_on_clipboard(path: Path, hold_seconds: int) -> int:
    """Clipboard owner loop, meant to run in the detached helper process.

    Returns a process exit code.  Tries X11 first (works without focus), then
    falls back to the native Wayland clipboard.
    """
    backend = os.environ.get("GDK_BACKEND", "x11")
    if backend != "wayland" and os.environ.get("DISPLAY"):
        try:
            return _hold_image_x11(path, hold_seconds)
        except Exception as exc:
            log.warning("x11 clipboard helper unavailable (%s), trying Wayland", exc)
    try:
        return _hold_image_wayland(path, hold_seconds)
    except Exception as exc:
        log.error("could not put the screenshot on the clipboard: %s", exc)
        _remove_temp(path)
        return 1


def _hold_image_x11(path: Path, hold_seconds: int) -> int:
    import gi as _gi

    _gi.require_version("Gtk", "3.0")
    _gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk as Gdk3
    from gi.repository import GLib as GLib3
    from gi.repository import Gtk as Gtk3

    if not os.environ.get("DISPLAY"):
        raise RuntimeError("no X11 display available")
    os.environ.setdefault("GDK_BACKEND", "x11")

    # gtk_init() parses sys.argv, which holds macshot's own options here.
    saved_argv = sys.argv
    sys.argv = sys.argv[:1]
    try:
        Gtk3.init([])
    finally:
        sys.argv = saved_argv

    display = Gdk3.Display.get_default()
    if not type(display).__name__.startswith("X11"):
        raise RuntimeError(f"not talking to X11 (got {type(display).__name__})")

    pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
    clipboard = Gtk3.Clipboard.get(Gdk3.SELECTION_CLIPBOARD)
    clipboard.set_image(pixbuf)
    # NB: no clipboard.store() here - that hands the selection to Mutter's
    # CLIPBOARD_MANAGER, which then keeps serving whatever it had cached before.
    log.info("clipboard: holding %s (X11 helper, %ds)", path, hold_seconds)
    GLib3.timeout_add_seconds(max(5, hold_seconds),
                              lambda: (Gtk3.main_quit(), False)[1])
    Gtk3.main()
    _remove_temp(path)
    return 0


def _hold_image_wayland(path: Path, hold_seconds: int) -> int:
    import gi as _gi

    _gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk as Gdk4
    from gi.repository import Gio as Gio4
    from gi.repository import GLib as GLib4
    from gi.repository import Gtk as Gtk4

    loop = GLib4.MainLoop()
    state = {"done": False}
    app = Gtk4.Application(application_id="dev.macshot.Clipboard",
                           flags=Gio4.ApplicationFlags.NON_UNIQUE)

    def on_activate(application):
        window = Gtk4.ApplicationWindow(application=application)
        window.set_default_size(1, 1)
        window.set_opacity(0.0)
        window.present()

        def claim():
            clipboard = Gdk4.Display.get_default().get_clipboard()
            data = path.read_bytes()
            provider = Gdk4.ContentProvider.new_for_bytes("image/png", GLib4.Bytes.new(data))
            clipboard.set_content(provider)
            GLib4.timeout_add_seconds(max(5, hold_seconds), lambda: (loop.quit(), False)[1])
            return False

        GLib4.timeout_add(300, claim)

    app.connect("activate", on_activate)
    app.run([])
    loop.quit()
    state["done"] = True
    _remove_temp(path)
    return 0


def _remove_temp(path: Path) -> None:
    try:
        if path.exists() and CACHE_DIR in path.parents:
            path.unlink()
    except OSError:  # pragma: no cover
        pass
