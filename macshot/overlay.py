"""The frozen-screen selection overlay.

The screenshot is taken *before* any window is mapped, so the overlay can draw
the desktop it is about to crop; that also means the overlay itself never shows
up in the capture (exactly how macOS behaves).

One borderless fullscreen window is created per monitor, each drawing its own
slice of the frozen image, so a selection can span monitors and still be cropped
from a single pixel buffer.

Everything is painted with GTK4's ``Gsk`` snapshot nodes instead of cairo: the
cairo foreign struct converters are not registered by every PyGObject build
(``Couldn't find foreign struct converter for 'cairo.Context'``), and Gsk needs
no extra dependency.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import (  # noqa: E402
    Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk, Pango,
)

from .monitors import Layout, Monitor  # noqa: E402
from .selection import (  # noqa: E402
    HANDLES, Rect, clamp_rect, cursor_for_handle, handle_points, hit_test,
    rect_from_points, resize_rect, size_label,
)

MIN_SELECTION = 3.0          # logical px, smaller drags are treated as a click
HANDLE_TOLERANCE = 6.0       # logical px around an edge/corner
HANDLE_SIZE = 7.0
FLASH_SECONDS = 0.18
HINT_DELAY = 1.2             # seconds of inactivity before the hint fades in
SAFETY_TIMEOUT = 600         # never leave the user stuck behind an overlay

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)


@dataclass
class OverlayResult:
    kind: str                      # "region" | "window" | "cancel"
    rect: Rect | None = None


def _rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def hint_text(allow_window: bool) -> str:
    """The one line cheat sheet shown while nothing is selected."""
    language = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
                or os.environ.get("LANG") or "")
    chinese = language.lower().startswith("zh")
    if chinese:
        parts = ["拖动选择区域"]
        if allow_window:
            parts.append("空格：选择窗口")
        parts.append("Esc：取消")
    else:
        parts = ["drag to select"]
        if allow_window:
            parts.append("space: window")
        parts.append("esc: cancel")
    return "    ".join(parts)


def _rgba(triple, alpha: float) -> Gdk.RGBA:
    return Gdk.RGBA(red=triple[0], green=triple[1], blue=triple[2], alpha=alpha)


def _rounded(rect: Graphene.Rect, radius: float) -> Gsk.RoundedRect:
    outline = Gsk.RoundedRect()
    outline.init_from_rect(rect, radius)
    return outline


class _Surface(Gtk.Widget):
    """A bare widget whose only job is to snapshot the overlay."""

    __gtype_name__ = "MacshotOverlaySurface"

    def __init__(self, overlay: "SelectionOverlay", monitor: Monitor,
                 primary: bool) -> None:
        super().__init__()
        self._overlay = overlay
        self._monitor = monitor
        self._primary = primary
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot) -> None:  # noqa: D102 - GTK vfunc
        self._overlay.paint(snapshot, self, self._monitor, self._primary)


class SelectionOverlay:
    def __init__(self, image_path: str, layout: Layout, *, allow_window: bool = True,
                 hints: bool = True, flash: bool = True, logger=None,
                 preview_selection: Rect | None = None) -> None:
        self.image_path = str(image_path)
        self.layout = layout
        self.allow_window = allow_window
        self.hints = hints
        self.flash = flash
        self.log = logger or (lambda *a, **k: None)
        self._preview = preview_selection

        self._pixbuf = GdkPixbuf.Pixbuf.new_from_file(self.image_path)
        self.image_size = (self._pixbuf.get_width(), self._pixbuf.get_height())
        self._bounds = layout.logical_bounds
        self._textures: dict[str, Gdk.Texture] = {}

        self._windows: list[tuple[Gtk.Window, _Surface, Monitor, bool]] = []
        self._result: OverlayResult | None = None
        self._phase = "idle"
        self._sel: Rect | None = None
        self._anchor = (0.0, 0.0)
        self._last = (0.0, 0.0)
        self._handle: str | None = None
        self._moved = False
        self._started = time.monotonic()
        self._last_activity = time.monotonic()
        self._flash_started: float | None = None
        self._app: Gtk.Application | None = None
        self._cursors: dict[str, Gdk.Cursor] = {}

    # ------------------------------------------------------------------ setup

    def _cursor(self, name: str) -> Gdk.Cursor:
        if name not in self._cursors:
            self._cursors[name] = Gdk.Cursor.new_from_name(name, None)
        return self._cursors[name]

    def _on_activate(self, app: Gtk.Application) -> None:
        display = Gdk.Display.get_default()
        gdk_monitors = display.get_monitors()
        count = gdk_monitors.get_n_items()
        for index in range(count):
            gdk_monitor = gdk_monitors.get_item(index)
            geometry = gdk_monitor.get_geometry()
            logical = Rect(geometry.x, geometry.y, geometry.width, geometry.height)
            monitor = self.layout.monitor_matching(logical)
            if monitor is None:
                monitor = self.layout.monitors[min(index, len(self.layout.monitors) - 1)]
            is_primary = monitor.primary or index == 0

            window = Gtk.ApplicationWindow(application=app)
            window.set_decorated(False)
            window.set_title("macshot")
            surface = _Surface(self, monitor, is_primary)
            window.set_child(surface)
            window.set_cursor(self._cursor("crosshair"))

            click = Gtk.GestureClick()
            click.set_button(1)
            click.connect("pressed", self._on_pressed, window)
            click.connect("released", self._on_released, window)
            surface.add_controller(click)

            motion = Gtk.EventControllerMotion()
            motion.connect("motion", self._on_motion, window)
            surface.add_controller(motion)

            key = Gtk.EventControllerKey()
            key.connect("key-pressed", self._on_key, window)
            window.add_controller(key)

            window.present()
            # Going fullscreen before the surface exists is ignored on Wayland.
            window.fullscreen_on_monitor(gdk_monitor)
            GLib.idle_add(self._ensure_fullscreen, window, gdk_monitor)
            self._windows.append((window, surface, monitor, is_primary))

        if self._preview is not None:            # development aid: show a fixed box
            self._sel = self._preview
            self._phase = "adjusting"
            self._redraw()
        elif self.hints:
            # Nothing moves while the pointer is still, so the hint needs its
            # own repaint once the idle delay has passed.
            GLib.timeout_add(int(HINT_DELAY * 1000) + 80, self._hint_tick)

        GLib.timeout_add_seconds(SAFETY_TIMEOUT, self._on_safety_timeout)
        self.log("overlay: %d window(s), image %s", len(self._windows), self.image_size)

    def _ensure_fullscreen(self, window: Gtk.Window, monitor) -> bool:
        # A window that is not resizable cannot be made fullscreen by the
        # compositor, so the request is (re)issued once the surface is mapped.
        if not window.is_fullscreen():
            window.fullscreen_on_monitor(monitor)
        GLib.timeout_add(600, self._log_window_state, window)
        return GLib.SOURCE_REMOVE

    def _log_window_state(self, window: Gtk.Window) -> bool:
        self.log("overlay window: fullscreen=%s size=%dx%d active=%s",
                 window.is_fullscreen(), window.get_width(), window.get_height(),
                 window.is_active())
        return GLib.SOURCE_REMOVE

    def _hint_tick(self) -> bool:
        if self._sel is None and self._phase == "idle":
            self._redraw()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------- lifecycle

    def run(self) -> OverlayResult:
        app = Gtk.Application(application_id="dev.macshot.SelectionOverlay",
                              flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._app = app
        app.connect("activate", self._on_activate)
        try:
            app.run([])
        except Exception as exc:  # pragma: no cover - no display etc.
            self.log("overlay failed: %s", exc)
            return OverlayResult("cancel")
        return self._result or OverlayResult("cancel")

    def _finish(self, result: OverlayResult) -> None:
        self._result = result
        if self._app is not None:
            self._app.quit()

    def _on_safety_timeout(self) -> bool:
        self.log("overlay: safety timeout reached, cancelling")
        self._finish(OverlayResult("cancel"))
        return GLib.SOURCE_REMOVE

    def _redraw(self) -> None:
        for _window, surface, _monitor, _primary in self._windows:
            surface.queue_draw()

    # ---------------------------------------------------------------- input

    def _monitor_of(self, window: Gtk.Window) -> Monitor:
        for candidate, _surface, monitor, _primary in self._windows:
            if candidate is window:
                return monitor
        return self.layout.monitors[0]

    def _global(self, window: Gtk.Window, x: float, y: float) -> tuple[float, float]:
        monitor = self._monitor_of(window)
        return monitor.logical_x + x, monitor.logical_y + y

    def _local(self, monitor: Monitor, rect: Rect) -> Rect:
        return Rect(rect.x - monitor.logical_x, rect.y - monitor.logical_y,
                    rect.width, rect.height)

    def _on_pressed(self, _gesture, n_press: int, x: float, y: float,
                    window: Gtk.Window) -> None:
        self._last_activity = time.monotonic()
        gx, gy = self._global(window, x, y)
        self._anchor = (gx, gy)
        self._last = (gx, gy)
        self._moved = False

        if self._phase == "adjusting" and self._sel is not None:
            if self._sel.contains(gx, gy) and n_press >= 2:
                self._confirm()
                return
            handle = hit_test(self._sel, gx, gy, HANDLE_TOLERANCE)
            if handle == "move":
                self._phase = "moving"
                return
            if handle in HANDLES:
                self._phase = "resizing"
                self._handle = handle
                return
        self._begin_drag(gx, gy)

    def _begin_drag(self, gx: float, gy: float) -> None:
        self._phase = "dragging"
        self._handle = None
        self._anchor = (gx, gy)
        self._last = (gx, gy)
        self._sel = Rect(gx, gy, 0.0, 0.0)
        self._redraw()

    def _on_motion(self, _controller, x: float, y: float, window: Gtk.Window) -> None:
        gx, gy = self._global(window, x, y)

        if self._phase == "dragging":
            self._sel = rect_from_points(self._anchor[0], self._anchor[1], gx, gy)
            if abs(gx - self._anchor[0]) > 1 or abs(gy - self._anchor[1]) > 1:
                self._moved = True
            self._redraw()
        elif self._phase in ("moving", "resizing") and self._sel is not None:
            dx, dy = gx - self._last[0], gy - self._last[1]
            if dx or dy:
                self._moved = True
            if self._phase == "moving":
                self._sel = clamp_rect(self._sel.translated(dx, dy), self._bounds)
            else:
                self._sel = resize_rect(self._sel, self._handle or "se", dx, dy,
                                        min_size=MIN_SELECTION, bounds=self._bounds)
            self._last = (gx, gy)
            self._redraw()
        else:
            handle = None
            if self._sel is not None and self._phase == "adjusting":
                handle = hit_test(self._sel, gx, gy, HANDLE_TOLERANCE)
            window.set_cursor(self._cursor(cursor_for_handle(handle)))

    def _on_released(self, _gesture, n_press: int, x: float, y: float,
                     window: Gtk.Window) -> None:
        if self._phase == "dragging":
            if self._sel is None or self._sel.width < MIN_SELECTION \
                    or self._sel.height < MIN_SELECTION:
                self._phase = "idle"
                self._sel = None
            else:
                self._sel = self._sel.round_to_ints()
                self._phase = "adjusting"
        elif self._phase in ("moving", "resizing"):
            if self._phase == "moving" and not self._moved:
                # A plain click inside the selection confirms it, like macOS.
                self._confirm()
                return
            if self._sel is not None:
                self._sel = self._sel.round_to_ints()
            self._phase = "adjusting"
        elif self._phase == "idle":
            self._phase = "idle"
        self._redraw()

    def _on_key(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType,
                _window: Gtk.Window) -> bool:
        self._last_activity = time.monotonic()
        name = Gdk.keyval_name(keyval)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)

        if name == "Escape":
            self._finish(OverlayResult("cancel"))
            return True
        if name in ("Return", "KP_Enter"):
            self._confirm()
            return True
        if name == "space" and self.allow_window and self._phase in ("idle", "adjusting"):
            self._finish(OverlayResult("window"))
            return True
        if self._sel is None:
            return False

        step = 10.0 if shift else 1.0
        moved = False
        if name == "Left":
            self._sel = clamp_rect(self._sel.translated(-step, 0), self._bounds)
            moved = True
        elif name == "Right":
            self._sel = clamp_rect(self._sel.translated(step, 0), self._bounds)
            moved = True
        elif name == "Up":
            self._sel = clamp_rect(self._sel.translated(0, -step), self._bounds)
            moved = True
        elif name == "Down":
            self._sel = clamp_rect(self._sel.translated(0, step), self._bounds)
            moved = True
        if moved:
            self._phase = "adjusting"
            self._redraw()
            return True
        return False

    def _confirm(self) -> None:
        if self._sel is None or self._sel.is_empty():
            return
        self._sel = self._sel.round_to_ints()
        self._result = OverlayResult("region", self._sel)
        self.log("overlay: selection %s", self._sel)
        if not self.flash:
            self._finish(self._result)
            return
        self._phase = "flash"
        self._flash_started = time.monotonic()
        GLib.timeout_add(16, self._tick)
        self._redraw()

    def _tick(self) -> bool:
        if self._flash_started is None:
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self._flash_started >= FLASH_SECONDS:
            self._finish(self._result or OverlayResult("cancel"))
            return GLib.SOURCE_REMOVE
        self._redraw()
        return GLib.SOURCE_CONTINUE

    # -------------------------------------------------------------- painting

    def _texture_for(self, monitor: Monitor) -> Gdk.Texture:
        key = f"{monitor.name}:{monitor.x},{monitor.y}"
        texture = self._textures.get(key)
        if texture is None:
            source = self.layout.physical_crop(monitor.logical_rect, self.image_size)
            slice_ = self._pixbuf.new_subpixbuf(
                int(source.x), int(source.y),
                max(1, int(source.width)), max(1, int(source.height)),
            )
            texture = Gdk.Texture.new_for_pixbuf(slice_)
            self._textures[key] = texture
        return texture

    def paint(self, snapshot, widget: Gtk.Widget, monitor: Monitor,
              primary: bool) -> None:
        width, height = widget.get_width(), widget.get_height()
        full = _rect(0, 0, width, height)
        snapshot.append_color(_rgba(BLACK, 1.0), full)
        snapshot.append_scaled_texture(self._texture_for(monitor),
                                       Gsk.ScalingFilter.TRILINEAR, full)

        if self._phase == "flash":
            self._paint_flash(snapshot, width, height, monitor)
            return

        if self._sel is not None and not self._sel.is_empty():
            self._paint_selection(snapshot, widget, width, height, monitor, self._sel)
        elif self.hints and time.monotonic() - self._last_activity > HINT_DELAY:
            self._paint_hint(snapshot, widget, width, height)

    def _paint_selection(self, snapshot, widget, width: int, height: int,
                         monitor: Monitor, selection: Rect) -> None:
        local = self._local(monitor, selection)
        dragging = self._phase == "dragging"

        if not dragging:
            # macOS darkens everything outside the selection once you let go.
            self._paint_outside(snapshot, width, height, local)
            for _name, hx, hy in handle_points(local):
                self._paint_square(snapshot, hx, hy, HANDLE_SIZE)

        outer = _rect(local.x - 1.5, local.y - 1.5, local.width + 3.0, local.height + 3.0)
        snapshot.append_border(_rounded(outer, 0.0), [3.0] * 4,
                               [_rgba(BLACK, 0.55)] * 4)
        inner = _rect(local.x - 0.75, local.y - 0.75, local.width + 1.5, local.height + 1.5)
        snapshot.append_border(_rounded(inner, 0.0), [1.5] * 4,
                               [_rgba(WHITE, 0.95)] * 4)

        self._paint_badge(snapshot, widget, width, height, local, size_label(selection))

    def _paint_outside(self, snapshot, width: int, height: int, local: Rect) -> None:
        color = _rgba(BLACK, 0.45)
        top = max(0.0, min(local.y, height))
        bottom = max(0.0, min(local.y2, height))
        left = max(0.0, min(local.x, width))
        right = max(0.0, min(local.x2, width))
        snapshot.append_color(color, _rect(0, 0, width, top))
        snapshot.append_color(color, _rect(0, bottom, width, max(0.0, height - bottom)))
        snapshot.append_color(color, _rect(0, top, left, max(0.0, bottom - top)))
        snapshot.append_color(color, _rect(right, top, max(0.0, width - right),
                                           max(0.0, bottom - top)))

    def _paint_square(self, snapshot, cx: float, cy: float, size: float) -> None:
        rect = _rect(cx - size / 2, cy - size / 2, size, size)
        snapshot.append_color(_rgba(BLACK, 0.5),
                              _rect(rect.origin.x - 1, rect.origin.y - 1,
                                    size + 2, size + 2))
        snapshot.append_color(_rgba(WHITE, 0.95), rect)

    def _paint_flash(self, snapshot, width: int, height: int, monitor: Monitor) -> None:
        if self._flash_started is None or self._sel is None:
            return
        elapsed = time.monotonic() - self._flash_started
        alpha = max(0.0, 1.0 - elapsed / FLASH_SECONDS) * 0.85
        local = self._local(monitor, self._sel)
        snapshot.append_color(_rgba(WHITE, alpha), _rect(0, 0, width, height))

    def _paint_hint(self, snapshot, widget, width: int, height: int) -> None:
        self._paint_badge(snapshot, widget, 0, 0, None, hint_text(self.allow_window),
                          size=15, center_in=(width, height), bottom_offset=80,
                          dim=False)

    def _paint_badge(self, snapshot, widget, width: int, height: int, local: Rect | None,
                     text: str, size: int = 13, center_in=None,
                     bottom_offset: float = 0.0, dim: bool = True) -> None:
        layout = widget.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string(f"Sans {size}"))
        text_w, text_h = layout.get_pixel_size()
        pad_x, pad_y = 9.0, 5.0
        box_w, box_h = text_w + pad_x * 2, text_h + pad_y * 2
        if center_in is not None:
            x = (center_in[0] - box_w) / 2
            y = center_in[1] - box_h - bottom_offset
        else:
            assert local is not None
            x = local.x
            y = local.y2 + 8
            if y + box_h > height:
                y = max(0.0, local.y - box_h - 8)
            x = max(0.0, min(x, width - box_w))
            y = max(0.0, min(y, height - box_h))
        box = _rect(x, y, box_w, box_h)
        if dim:
            snapshot.push_rounded_clip(_rounded(box, 6.0))
            snapshot.append_color(_rgba((0.1, 0.1, 0.1), 0.82), box)
            snapshot.pop()
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x + pad_x, y + pad_y))
        snapshot.append_layout(layout, _rgba(WHITE, 0.95))
        snapshot.restore()
