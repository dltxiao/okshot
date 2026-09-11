"""Command line entry point: mode dispatch, saving and feedback."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

from . import __version__  # noqa: E402
from .config import Config  # noqa: E402
from .monitors import Layout, fallback_layout, get_layout  # noqa: E402
from .portal import Portal, PortalCancelled, PortalError  # noqa: E402
from .save import (  # noqa: E402
    build_filename, cache_dir, crop_png, default_save_dir, hold_image_on_clipboard,
    move_into, notify, play_shutter_sound, spawn_clipboard_holder,
)
from .selection import Rect  # noqa: E402

log = logging.getLogger("macshot")

MODES = ("full", "region", "window", "ui")
EXIT_OK, EXIT_CANCELLED, EXIT_ERROR = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macshot",
        description="macOS style screenshots for GNOME/Wayland "
                    "(Alt+Shift+3 full screen, Alt+Shift+4 region, Alt+Shift+5 window).",
    )
    parser.add_argument("mode", nargs="?", default="region", choices=MODES,
                        help="full = whole desktop, region = drag a rectangle, "
                             "window = pick a window, ui = the desktop's own picker")
    parser.add_argument("-c", "--clipboard", action="store_true",
                        help="copy to the clipboard instead of writing a file "
                             "(macOS: hold Ctrl)")
    parser.add_argument("-d", "--delay", type=float, default=0.0,
                        help="wait N seconds before taking the shot")
    parser.add_argument("-s", "--save-dir", default=None,
                        help="directory for new screenshots (default ~/Desktop)")
    parser.add_argument("--no-sound", action="store_true", help="stay silent")
    parser.add_argument("--no-flash", action="store_true", help="no white flash")
    parser.add_argument("--no-hints", action="store_true",
                        help="hide the on screen hint while selecting")
    parser.add_argument("--notify", action="store_true",
                        help="send a desktop notification when a file was written")
    parser.add_argument("--image", default=None,
                        help="use an existing PNG instead of grabbing the screen "
                             "(mainly for testing)")
    parser.add_argument("--select", default=None, metavar="X,Y,W,H",
                        help="skip the overlay and crop this logical rectangle")
    parser.add_argument("--config", default=None, help="alternative config file")
    parser.add_argument("--hold", type=int, default=None,
                        help="internal: clipboard holder lifetime in seconds")
    parser.add_argument("--preview-selection", default=None, metavar="X,Y,W,H",
                        help="internal: draw a fixed selection (UI development)")
    parser.add_argument("--clipboard-holder", default=None, metavar="PNG",
                        help="internal: own the clipboard for this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"macshot {__version__}")
    return parser


def setup_logging(verbose: bool) -> None:
    from .save import LOG_PATH

    handlers: list[logging.Handler] = []
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.unlink()
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        pass
    if verbose or not handlers:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------- helpers


def parse_select(value: str) -> Rect:
    parts = [float(p) for p in value.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--select needs X,Y,W,H")
    return Rect(*parts)


def image_size(path: Path) -> tuple[int, int]:
    _format, width, height = GdkPixbuf.Pixbuf.get_file_info(str(path))
    if not width or not height:
        raise RuntimeError(f"cannot read image size of {path}")
    return width, height


def resolve_layout(image: Path) -> Layout:
    try:
        return get_layout()
    except Exception as exc:
        log.warning("falling back to a 1:1 monitor layout: %s", exc)
        return fallback_layout(image_size(image))


def take_screenshot(delay: float, interactive: bool = False) -> Path:
    if delay > 0:
        log.info("waiting %.1fs before capture", delay)
        time.sleep(delay)
    portal = Portal()
    path = Path(portal.screenshot(interactive=interactive))
    log.info("portal produced %s", path)
    return path


def remove_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def feed_clipboard(path: Path, config: Config, clipboard_only: bool) -> None:
    """Hand the PNG to the clipboard helper (and keep the cache tidy)."""
    if clipboard_only:
        target = cache_dir() / f"clip-{int(time.time() * 1000)}.png"
        try:
            os.replace(path, target)
        except OSError:
            import shutil

            shutil.copy2(path, target)
            remove_quietly(path)
        path = target
    spawn_clipboard_holder(path, config.clipboard_hold_seconds)
    log.info("clipboard holder started for %s", path)


def deliver(path: Path, args, config: Config) -> Path | None:
    """Move/copy the finished PNG where the user wants it."""
    if args.clipboard:
        feed_clipboard(path, config, clipboard_only=True)
        return None
    destination = move_into(path, config.save_path, build_filename(config.filename_template))
    log.info("saved %s", destination)
    if args.notify:
        notify("已保存截图", str(destination))
    return destination


# ----------------------------------------------------------------------- modes


def flow_full(args, config: Config) -> int:
    source = take_screenshot(args.delay)
    if config.play_sound and not args.no_sound:
        play_shutter_sound()
    deliver(source, args, config)
    return EXIT_OK


def flow_window(args, config: Config) -> int:
    """Window picking is delegated to the desktop's own frozen-screen UI.

    GNOME 50's ``org.gnome.Shell.Introspect`` (window geometry) is behind a
    sender allow-list that third party processes cannot join, so there is no way
    to implement hover-to-highlight ourselves; the desktop UI does it properly.
    """
    source = take_screenshot(args.delay, interactive=True)
    if config.play_sound and not args.no_sound:
        play_shutter_sound()
    deliver(source, args, config)
    return EXIT_OK


def flow_region(args, config: Config) -> int:
    from .overlay import SelectionOverlay

    if args.image:
        source = Path(args.image)
        temporary = None
        log.info("using %s instead of a live capture", source)
    else:
        source = take_screenshot(args.delay)
        temporary = source

    layout = resolve_layout(source)
    size = image_size(source)
    log.debug("layout=%r image=%s", layout, size)

    if args.select:
        selection = parse_select(args.select)
    else:
        overlay = SelectionOverlay(
            source, layout,
            allow_window=config.window_mode != "off",
            hints=config.hints and not args.no_hints,
            flash=config.flash and not args.no_flash,
            logger=log.debug,
            preview_selection=(parse_select(args.preview_selection)
                               if args.preview_selection else None),
        )
        result = overlay.run()
        if result.kind == "window":
            remove_quietly(temporary)
            return flow_window(args, config)
        if result.kind != "region" or result.rect is None:
            log.info("selection cancelled")
            remove_quietly(temporary)
            return EXIT_CANCELLED
        selection = result.rect

    crop = layout.physical_crop(selection, size)
    log.info("selection %s -> crop %s", selection, crop)
    if config.play_sound and not args.no_sound:
        play_shutter_sound()

    if args.clipboard:
        target = cache_dir() / f"clip-{int(time.time() * 1000)}.png"
        crop_png(source, crop, target)
        remove_quietly(temporary)
        feed_clipboard(target, config, clipboard_only=False)
        return EXIT_OK

    destination = move_into(
        _crop_to_temp(source, crop), config.save_path,
        build_filename(config.filename_template),
    )
    remove_quietly(temporary)
    log.info("saved %s", destination)
    if args.notify:
        notify("已保存截图", str(destination))
    return EXIT_OK


def _crop_to_temp(source: Path, crop: Rect) -> Path:
    target = cache_dir() / f"crop-{int(time.time() * 1000)}.png"
    crop_png(source, crop, target)
    return target


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    log.info("macshot %s: %s", __version__, " ".join(argv if argv is not None else sys.argv[1:]))

    if args.clipboard_holder:
        hold_seconds = args.hold or 180
        return hold_image_on_clipboard(Path(args.clipboard_holder), hold_seconds)

    config = Config.load(Path(args.config).expanduser() if args.config else None)
    if args.save_dir:
        config.save_dir = args.save_dir
    if not args.save_dir and config.save_dir == "~/Desktop" and not config.save_path.exists():
        config.save_dir = str(default_save_dir())

    try:
        if args.mode == "full":
            return flow_full(args, config)
        if args.mode in ("window", "ui"):
            return flow_window(args, config)
        return flow_region(args, config)
    except PortalCancelled:
        log.info("cancelled by the user")
        return EXIT_CANCELLED
    except PortalError as exc:
        log.error("%s", exc)
        notify("截图失败", str(exc), icon="dialog-error")
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - last resort
        log.exception("unexpected failure")
        notify("截图失败", str(exc), icon="dialog-error")
        return EXIT_ERROR
