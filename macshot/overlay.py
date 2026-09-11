"""The frozen-screen selection overlay with WeChat style annotations.

The screenshot is taken *before* any window is mapped, so the overlay can draw
the desktop it is about to crop; that also means the overlay itself never shows
up in the capture (exactly how macOS behaves).

One borderless fullscreen window is created per monitor, each drawing its own
slice of the frozen image, so a selection can span monitors and still be cropped
from a single pixel buffer.

Everything is painted with GTK4's ``Gsk`` snapshot nodes instead of cairo: the
cairo foreign struct converters are not registered by every PyGObject build
(``Couldn't find foreign struct converter for 'cairo.Context'``), and Gsk needs
no extra dependency.  The annotation layer uses the very same code for the live
preview and for rasterising the exported PNG, so what you see is what you get.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import (  # noqa: E402
    Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk, Pango,
)

from .annotate import (  # noqa: E402
    DEFAULT_WIDTH, MAX_WIDTH, MIN_WIDTH, MOSAIC_BLOCK, PALETTE, TOOLS,
    TOOL_LABELS, Annotation, AnnotationStack, Arrow, Box, Mapper, Stroke, Text,
    build_layer_node, clone_with, paint_annotation, pixelate_pixbuf,
    save_node_png, text_size,
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
MIN_SHAPE = 3.0              # smaller shapes are dropped as accidental clicks
BRUSH_STEP = 2.0             # minimum distance between brush samples
ICON_SIZE = 18
TOOLBAR_HEIGHT = 40
TOOL_KEYS = ("1", "2", "3", "4", "5", "6", "7")

WHITE = (1.0, 1.0, 1.0)
BLACK = (0.0, 0.0, 0.0)

CSS = """
.macshot-toolbar {
  background: rgba(26,26,28,0.94);
  border-radius: 12px;
  padding: 4px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.45);
}
.macshot-toolbar button {
  min-width: 26px; min-height: 26px; padding: 2px;
  background: rgba(255,255,255,0.10);
  border: 1px solid rgba(255,255,255,0.12);
  box-shadow: none;
}
.macshot-toolbar button:hover { background: rgba(255,255,255,0.24); }
.macshot-toolbar button:checked { background: #2f6fed; border-color: #6aa2ff; }
.macshot-toolbar button.save {
  background: #2f9e63;
  border-color: #46c184;
  color: #ffffff;
  padding: 2px 12px;
  font-weight: bold;
}
.macshot-toolbar button.save:hover { background: #38b573; }
.macshot-toolbar label.width { min-width: 16px; font-weight: bold; color: #f2f2f4; }
.macshot-entry {
  background: rgba(18,18,20,0.96);
  color: #ffffff;
  caret-color: #ffffff;
  border: 1px solid rgba(255,255,255,0.55);
  border-radius: 6px;
  min-height: 26px;
  padding: 2px 6px;
}
"""


@dataclass
class OverlayResult:
    kind: str                      # "region" | "window" | "cancel"
    rect: Rect | None = None
    #: set when annotations were baked into a PNG already
    export_path: str | None = None


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


def _rect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def _rounded(rect: Graphene.Rect, radius: float) -> Gsk.RoundedRect:
    outline = Gsk.RoundedRect()
    outline.init_from_rect(rect, radius)
    return outline


def _stroke_path(snapshot, path, width: float, color, alpha: float = 1.0) -> None:
    stroke = Gsk.Stroke.new(max(0.6, width))
    stroke.set_line_cap(Gsk.LineCap.ROUND)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    snapshot.append_stroke(path, stroke, _rgba(color, alpha))


def _fill_path(snapshot, path, color, alpha: float = 1.0) -> None:
    snapshot.append_fill(path, Gsk.FillRule.WINDING, _rgba(color, alpha))


def _polygon(points) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    builder.move_to(*points[0])
    for point in points[1:]:
        builder.line_to(*point)
    builder.close()
    return builder.to_path()


def _line_path(points) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    builder.move_to(*points[0])
    for point in points[1:]:
        builder.line_to(*point)
    return builder.to_path()


def icon_node(kind: str, size: float, color=WHITE):
    """Vector icons for the toolbar, drawn with the same Gsk helpers."""
    snapshot = Gtk.Snapshot.new()
    pad = size * 0.2
    inner = size - pad * 2
    weight = max(1.4, size * 0.085)

    if kind == "rect":
        builder = Gsk.PathBuilder.new()
        builder.add_rect(_rect(pad, pad + size * 0.05, inner, inner * 0.85))
        _stroke_path(snapshot, builder.to_path(), weight, color)
    elif kind == "ellipse":
        builder = Gsk.PathBuilder.new()
        builder.add_circle(Graphene.Point().init(size / 2, size / 2), inner / 2)
        _stroke_path(snapshot, builder.to_path(), weight, color)
    elif kind == "arrow":
        _stroke_path(snapshot, _line_path([(pad, size - pad), (size - pad, pad)]), weight, color)
        _fill_path(snapshot, _polygon([
            (size - pad, pad), (size - pad - size * 0.32, pad + size * 0.05),
            (size - pad - size * 0.05, pad + size * 0.32)]), color)
    elif kind == "brush":
        builder = Gsk.PathBuilder.new()
        builder.move_to(pad, size * 0.74)
        builder.cubic_to(size * 0.30, size * 0.10, size * 0.58, size * 0.98,
                         size - pad, size * 0.28)
        _stroke_path(snapshot, builder.to_path(), weight * 1.5, color)
    elif kind == "mosaic":
        cell = inner / 3.0
        for row in range(3):
            for column in range(3):
                if (row + column) % 2 == 0:
                    builder = Gsk.PathBuilder.new()
                    builder.add_rect(_rect(pad + column * cell, pad + row * cell,
                                           cell * 0.9, cell * 0.9))
                    _fill_path(snapshot, builder.to_path(), color)
    elif kind == "text":
        builder = Gsk.PathBuilder.new()
        builder.add_rect(_rect(pad, pad, inner, weight * 1.3))
        _fill_path(snapshot, builder.to_path(), color)
        builder = Gsk.PathBuilder.new()
        builder.add_rect(_rect(size / 2 - weight * 0.65, pad, weight * 1.3, inner))
        _fill_path(snapshot, builder.to_path(), color)
    elif kind == "move":                    # a classic arrow cursor
        points = [(0.22, 0.14), (0.22, 0.82), (0.40, 0.63), (0.53, 0.92),
                  (0.65, 0.86), (0.52, 0.58), (0.74, 0.55)]
        _fill_path(snapshot, _polygon([(x * size, y * size) for x, y in points]), color)
    elif kind == "undo":
        builder = Gsk.PathBuilder.new()
        builder.move_to(size - pad, size * 0.62)
        builder.cubic_to(size - pad, pad + size * 0.12, pad + size * 0.12,
                         pad + size * 0.12, pad, size * 0.55)
        _stroke_path(snapshot, builder.to_path(), weight, color)
        _fill_path(snapshot, _polygon([
            (pad - size * 0.06, size * 0.24), (pad + size * 0.30, size * 0.30),
            (pad + size * 0.04, size * 0.62)]), color)
    elif kind == "check":
        _stroke_path(snapshot, _line_path([
            (pad, size * 0.55), (size * 0.42, size - pad), (size - pad, pad)]),
            weight * 1.4, color)
    elif kind == "close":
        _stroke_path(snapshot, _line_path([(pad, pad), (size - pad, size - pad)]), weight, color)
        _stroke_path(snapshot, _line_path([(size - pad, pad), (pad, size - pad)]), weight, color)
    elif kind == "minus":
        _stroke_path(snapshot, _line_path([(pad, size / 2), (size - pad, size / 2)]), weight, color)
    elif kind == "plus":
        _stroke_path(snapshot, _line_path([(pad, size / 2), (size - pad, size / 2)]), weight, color)
        _stroke_path(snapshot, _line_path([(size / 2, pad), (size / 2, size - pad)]), weight, color)
    return snapshot.to_node()


def swatch_node(color, size: float):
    snapshot = Gtk.Snapshot.new()
    builder = Gsk.PathBuilder.new()
    center = Graphene.Point().init(size / 2, size / 2)
    builder.add_circle(center, size * 0.34)
    _fill_path(snapshot, builder.to_path(), color)
    ring = Gsk.PathBuilder.new()
    ring.add_circle(center, size * 0.34)
    _stroke_path(snapshot, ring.to_path(), 1.4, WHITE, 0.85)
    return snapshot.to_node()


class _Surface(Gtk.Widget):
    """A bare widget whose only job is to snapshot the overlay."""

    __gtype_name__ = "MacshotOverlaySurface"

    def __init__(self, overlay: "SelectionOverlay") -> None:
        super().__init__()
        self._overlay = overlay
        self.set_hexpand(True)
        self.set_vexpand(True)

    def do_snapshot(self, snapshot) -> None:  # noqa: D102 - GTK vfunc
        self._overlay.paint(snapshot, self)


@dataclass
class _WindowState:
    window: Gtk.Window
    surface: _Surface
    container: Gtk.Overlay
    monitor: Monitor
    primary: bool
    toolbar: Gtk.Widget | None = None
    buttons: dict = field(default_factory=dict)
    position: tuple[float, float] = (0.0, 0.0)


class SelectionOverlay:
    def __init__(self, image_path: str, layout: Layout, *, allow_window: bool = True,
                 hints: bool = True, flash: bool = True, logger=None,
                 preview_selection: Rect | None = None,
                 export_path: str | None = None,
                 allow_annotations: bool = True,
                 preview_annotations: bool = False,
                 auto_confirm: float | None = None) -> None:
        self.image_path = str(image_path)
        self.layout = layout
        self.allow_window = allow_window
        self.hints = hints
        self.flash = flash
        self.log = logger or (lambda *a, **k: None)
        self.export_path = str(export_path) if export_path else None
        self.allow_annotations = allow_annotations
        self._preview = preview_selection
        self._preview_annotations = preview_annotations
        self._auto_confirm = auto_confirm

        self._pixbuf = GdkPixbuf.Pixbuf.new_from_file(self.image_path)
        self.image_size = (self._pixbuf.get_width(), self._pixbuf.get_height())
        self._bounds = layout.logical_bounds
        self._textures: dict[str, Gdk.Texture] = {}
        self._mosaic_textures: dict[str, Gdk.Texture] = {}
        self._slices: dict[str, GdkPixbuf.Pixbuf] = {}

        self._windows: list[_WindowState] = []
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
        self._css_installed = False

        # annotations
        self.annotations = AnnotationStack()
        self.tool = "move"
        self.color = PALETTE[0]
        self.width = DEFAULT_WIDTH
        self._drawing = False
        self._draft: Annotation | None = None
        self._entry: Gtk.Entry | None = None
        self._entry_anchor: tuple[float, float] | None = None
        self._entry_window: Gtk.Window | None = None
        self._syncing = False
        self._icons: dict[str, Gdk.Texture] = {}
        self._icon_renderer = None

    # ------------------------------------------------------------------ setup

    def _cursor(self, name: str) -> Gdk.Cursor:
        if name not in self._cursors:
            self._cursors[name] = Gdk.Cursor.new_from_name(name, None)
        return self._cursors[name]

    def _on_activate(self, app: Gtk.Application) -> None:
        display = Gdk.Display.get_default()
        gdk_monitors = display.get_monitors()
        count = gdk_monitors.get_n_items()
        self.log("overlay: image %s, mapping=%s, %d GTK monitor(s)", self.image_size,
                 self.layout.mapping_kind(self.image_size), count)
        self._install_css(display)
        for index in range(count):
            gdk_monitor = gdk_monitors.get_item(index)
            geometry = gdk_monitor.get_geometry()
            logical = Rect(geometry.x, geometry.y, geometry.width, geometry.height)
            connector = (gdk_monitor.get_connector()
                         if hasattr(gdk_monitor, "get_connector") else None)
            # The connector name is the most reliable key; geometry (which Mutter
            # reports in the same stage space as DisplayConfig) is the backup.
            monitor = (self.layout.monitor_matching(logical, connector)
                       or self.layout.monitor_matching(logical))
            if monitor is None:
                monitor = self.layout.monitors[min(index, len(self.layout.monitors) - 1)]
            is_primary = monitor.primary or index == 0
            self.log("overlay: GTK monitor %d %s (%s) -> %s, image slice %s",
                     index, logical, connector or "?", monitor.name,
                     self.layout.physical_crop(monitor.logical_rect, self.image_size))

            window = Gtk.ApplicationWindow(application=app)
            window.set_decorated(False)
            window.set_title("macshot")
            surface = _Surface(self)
            container = Gtk.Overlay()
            container.set_child(surface)
            window.set_child(container)
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
            self._windows.append(_WindowState(window, surface, container, monitor, is_primary))

        if self._preview is not None:            # development aid: show a fixed box
            self._sel = self._preview
            self._phase = "adjusting"
            if self._preview_annotations:
                self._add_sample_annotations()
            self._sync_toolbar()
            self._redraw()
        if self._auto_confirm:
            GLib.timeout_add(int(self._auto_confirm * 1000), self._auto_confirm_tick)
        elif self.hints:
            # Nothing moves while the pointer is still, so the hint needs its
            # own repaint once the idle delay has passed.
            GLib.timeout_add(int(HINT_DELAY * 1000) + 80, self._hint_tick)

        GLib.timeout_add_seconds(SAFETY_TIMEOUT, self._on_safety_timeout)
        self.log("overlay: %d window(s), image %s, export=%s", len(self._windows),
                 self.image_size, self.export_path)

    @staticmethod
    def _install_css(display) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:  # pragma: no cover - theming is cosmetic
            pass

    def _ensure_fullscreen(self, window: Gtk.Window, monitor) -> bool:
        # A window that is not resizable cannot be made fullscreen by the
        # compositor, so the request is (re)issued once the surface is mapped.
        if not window.is_fullscreen():
            window.fullscreen_on_monitor(monitor)
        GLib.timeout_add(600, self._log_window_state, window)
        GLib.timeout_add(650, self._resync_toolbar)
        return GLib.SOURCE_REMOVE

    def _resync_toolbar(self) -> bool:
        """Re-place the toolbar once the windows have their real size."""
        self._sync_toolbar()
        self._redraw()
        return GLib.SOURCE_REMOVE

    def _log_window_state(self, window: Gtk.Window) -> bool:
        self.log("overlay window: fullscreen=%s size=%dx%d active=%s",
                 window.is_fullscreen(), window.get_width(), window.get_height(),
                 window.is_active())
        return GLib.SOURCE_REMOVE

    def _add_sample_annotations(self) -> None:
        """Development aid: one annotation of every kind, for smoke tests."""
        sel = self._preview or self._sel
        if sel is None:
            return
        x, y, w, h = sel.x, sel.y, sel.width, sel.height
        self.annotations.clear()
        self.annotations.add(Box("rect", x + w * 0.05, y + h * 0.06,
                                 x + w * 0.34, y + h * 0.30, PALETTE[0], 4))
        self.annotations.add(Box("ellipse", x + w * 0.42, y + h * 0.06,
                                 x + w * 0.72, y + h * 0.30, PALETTE[2], 4))
        self.annotations.add(Arrow(x1=x + w * 0.06, y1=y + h * 0.88,
                                   x2=x + w * 0.40, y2=y + h * 0.55,
                                   color=PALETTE[3], width=5))
        self.annotations.add(Stroke("brush", tuple(
            (x + w * 0.06 + i * w * 0.012, y + h * 0.55 + h * 0.10
             * math.sin(i / 3.0)) for i in range(28)), PALETTE[1], 4))
        self.annotations.add(Stroke("mosaic", tuple(
            (x + w * 0.44 + i * w * 0.011, y + h * 0.45) for i in range(22)), width=26))
        self.annotations.add(Text(x=x + w * 0.44, y=y + h * 0.60,
                                  text="标注 Annotation", color=PALETTE[5],
                                  size=text_size(6)))
        self.log("overlay: %d sample annotation(s) for preview", len(self.annotations))

    def _auto_confirm_tick(self) -> bool:
        self._confirm()
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
        self.log("overlay: finishing (%s%s)", result.kind,
                 f", {result.rect}" if result.rect else "")
        self._result = result
        if self._app is not None:
            self._app.quit()

    def _on_safety_timeout(self) -> bool:
        self.log("overlay: safety timeout reached, cancelling")
        self._finish(OverlayResult("cancel"))
        return GLib.SOURCE_REMOVE

    def _redraw(self) -> None:
        self._sync_toolbar()
        for state in self._windows:
            state.surface.queue_draw()

    # --------------------------------------------------------------- windows

    def _state_of(self, window: Gtk.Window) -> _WindowState:
        for state in self._windows:
            if state.window is window:
                return state
        return self._windows[0]

    def _monitor_of(self, window: Gtk.Window) -> Monitor:
        return self._state_of(window).monitor

    def _global(self, window: Gtk.Window, x: float, y: float) -> tuple[float, float]:
        """Window-local coordinates -> desktop (stage) coordinates."""
        monitor = self._monitor_of(window)
        return monitor.x + x, monitor.y + y

    def _local(self, monitor: Monitor, rect: Rect) -> Rect:
        """Desktop (stage) coordinates -> this window's local coordinates."""
        return Rect(rect.x - monitor.x, rect.y - monitor.y,
                    rect.width, rect.height)

    # ------------------------------------------------------------ annotations

    def _set_tool(self, tool: str) -> None:
        if tool not in TOOLS or not self.allow_annotations:
            return
        self._cancel_text(commit=False)
        self.tool = tool
        self._drawing = False
        self._draft = None
        for state in self._windows:
            self._update_toolbar_state(state)
        self._redraw()

    def _set_color(self, color) -> None:
        self.color = color
        for state in self._windows:
            self._update_toolbar_state(state)
        self._redraw()

    def _set_width(self, width: float) -> None:
        self.width = max(MIN_WIDTH, min(MAX_WIDTH, width))
        for state in self._windows:
            self._update_toolbar_state(state)
        self._redraw()

    def _begin_annotation(self, gx: float, gy: float) -> None:
        tool = self.tool
        if tool in ("brush", "mosaic"):
            self.annotations.add(Stroke(tool, ((gx, gy),), self.color, self.width))
        elif tool in ("rect", "ellipse"):
            self.annotations.add(Box(tool, gx, gy, gx, gy, self.color, self.width))
        elif tool == "arrow":
            self.annotations.add(Arrow(x1=gx, y1=gy, x2=gx, y2=gy,
                                       color=self.color, width=self.width))

    def _update_annotation(self, gx: float, gy: float) -> None:
        if not self.annotations.items:
            return
        last = self.annotations.items[-1]
        if isinstance(last, Stroke):
            px, py = last.points[-1]
            if abs(gx - px) < BRUSH_STEP and abs(gy - py) < BRUSH_STEP:
                return
            self.annotations.replace_last(clone_with(last, points=last.points + ((gx, gy),)))
        elif isinstance(last, Box):
            self.annotations.replace_last(clone_with(last, x2=gx, y2=gy))
        elif isinstance(last, Arrow):
            self.annotations.replace_last(clone_with(last, x2=gx, y2=gy))

    def _finish_annotation(self) -> None:
        if not self.annotations.items:
            return
        last = self.annotations.items[-1]
        if isinstance(last, Stroke):
            if len(last.points) == 1:
                return                       # a tap: keep the dot
            return
        length = 0.0
        if isinstance(last, Box):
            length = max(abs(last.x2 - last.x1), abs(last.y2 - last.y1))
        elif isinstance(last, Arrow):
            length = ((last.x2 - last.x1) ** 2 + (last.y2 - last.y1) ** 2) ** 0.5
        if length < MIN_SHAPE:               # an accidental click, drop it
            self.annotations.discard_last()

    # ------------------------------------------------------------- text tool

    def _begin_text(self, window: Gtk.Window, x: float, y: float,
                    gx: float, gy: float) -> None:
        self._cancel_text(commit=False)
        entry = Gtk.Entry()
        entry.set_placeholder_text("输入文字，回车确认")
        entry.set_width_chars(12)
        entry.add_css_class("macshot-entry")
        entry.connect("activate", lambda *_a: self._commit_text())
        entry.connect("key-pressed", self._on_entry_key)
        state = self._state_of(window)
        state.container.add_overlay(entry)
        entry.set_halign(Gtk.Align.START)
        entry.set_valign(Gtk.Align.START)
        entry.set_margin_start(int(x))
        entry.set_margin_top(max(0, int(y)))
        entry.grab_focus()
        self._entry = entry
        self._entry_anchor = (gx, gy)
        self._entry_window = window
        self._redraw()

    def _on_entry_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if Gdk.keyval_name(keyval) == "Escape":
            self._cancel_text(commit=False)
            return True
        return False

    def _commit_text(self) -> None:
        entry, anchor = self._entry, self._entry_anchor
        if entry is None or anchor is None:
            return
        text = entry.get_text().strip()
        self._drop_entry()
        if text:
            self.annotations.add(Text(x=anchor[0], y=anchor[1], text=text,
                                      color=self.color, size=text_size(self.width)))
        self._redraw()

    def _cancel_text(self, commit: bool = True) -> None:
        if self._entry is None:
            return
        if commit:
            self._commit_text()
            return
        self._drop_entry()
        self._redraw()

    def _drop_entry(self) -> None:
        entry = self._entry
        self._entry = None
        self._entry_anchor = None
        if entry is not None:
            parent = entry.get_parent()
            if parent is not None:
                parent.remove_overlay(entry)
        window = self._entry_window
        self._entry_window = None
        if window is not None:
            window.grab_focus()

    # ------------------------------------------------------------------ input

    def _on_pressed(self, _gesture, n_press: int, x: float, y: float,
                    window: Gtk.Window) -> None:
        self._last_activity = time.monotonic()
        gx, gy = self._global(window, x, y)
        self.log("overlay: press #%d at stage (%.0f, %.0f), phase=%s tool=%s",
                 n_press, gx, gy, self._phase, self.tool)
        if self._entry is not None:
            self._commit_text()
        self._anchor = (gx, gy)
        self._last = (gx, gy)
        self._moved = False

        if self._phase == "adjusting" and self._sel is not None:
            if self.tool == "move":
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
            else:
                if not self._sel.contains(gx, gy):
                    return                    # annotations stay inside the crop
                if self.tool == "text":
                    self._begin_text(window, x, y, gx, gy)
                    return
                self._drawing = True
                self._begin_annotation(gx, gy)
                self._redraw()
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

        if self._drawing:
            self._update_annotation(gx, gy)
            self._redraw()
            return
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
            if self.tool != "move" and self._sel is not None \
                    and self._sel.contains(gx, gy):
                window.set_cursor(self._cursor("crosshair"))
            else:
                window.set_cursor(self._cursor(cursor_for_handle(handle)))

    def _on_released(self, _gesture, n_press: int, x: float, y: float,
                     window: Gtk.Window) -> None:
        if self._drawing:
            self._drawing = False
            self._finish_annotation()
            self._redraw()
            return
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
        control = bool(state & Gdk.ModifierType.CONTROL_MASK)

        if self._entry is not None:
            # The entry owns the keyboard while it is open.
            if name == "Escape":
                self._cancel_text(commit=False)
                return True
            return False
        if control and name in ("z", "Z"):
            if shift:
                self.annotations.redo()
            else:
                self.annotations.undo()
            self._redraw()
            return True
        if name == "Escape":
            self._finish(OverlayResult("cancel"))
            return True
        if name in ("Return", "KP_Enter"):
            self._confirm()
            return True
        if name == "space" and self.allow_window and self._phase in ("idle", "adjusting"):
            self._finish(OverlayResult("window"))
            return True
        if self.allow_annotations and name in TOOL_KEYS:
            self._set_tool(TOOLS[TOOL_KEYS.index(name)])
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

    # ----------------------------------------------------------------- export

    def _crop_rect(self) -> Rect:
        assert self._sel is not None
        return self.layout.physical_crop(self._sel, self.image_size)

    def _layout_factory(self, window: Gtk.Window):
        def factory(text: str, size: float):
            layout = window.create_pango_layout("")
            layout.set_text(text, -1)
            layout.set_font_description(
                Pango.FontDescription.from_string(f"Sans {max(6, int(round(size)))}"))
            return layout
        return factory

    def _mosaic_texture(self, image_key: str, pixbuf, block: float) -> Gdk.Texture | None:
        texture = self._mosaic_textures.get(image_key)
        if texture is None:
            try:
                texture = Gdk.Texture.new_for_pixbuf(pixelate_pixbuf(pixbuf, block))
            except Exception as exc:  # pragma: no cover - defensive
                self.log("mosaic unavailable: %s", exc)
                return None
            self._mosaic_textures[image_key] = texture
        return texture

    def export_annotated(self) -> str | None:
        """Bake the annotations into the cropped PNG; returns the path."""
        if not self.annotations.items or self.export_path is None or self._sel is None:
            return None
        window = self._windows[0].window
        native = window.get_native()
        renderer = native.get_renderer() if native is not None else None
        if renderer is None:  # pragma: no cover - only without a compositor
            self.log("no renderer available, skipping annotations")
            return None

        crop = self._crop_rect()
        size = (int(crop.width), int(crop.height))
        base = self._pixbuf.new_subpixbuf(int(crop.x), int(crop.y), size[0], size[1])

        mosaic_texture = None
        if any(isinstance(a, Stroke) and a.kind == "mosaic" for a in self.annotations):
            scale = self.layout.stage_scale(self.image_size) or 1.0
            mosaic_texture = self._mosaic_texture(f"export:{size}", base, MOSAIC_BLOCK * scale)

        mapper = Mapper.export(self.layout, crop, self.image_size)
        node = build_layer_node(
            self.annotations.items, mapper, size, self._layout_factory(window),
            mosaic_texture=mosaic_texture,
            base_texture=Gdk.Texture.new_for_pixbuf(base),
        )
        if save_node_png(renderer, node, size, self.export_path):
            self.log("overlay: exported annotated %s (%dx%d, %d annotation(s))",
                     self.export_path, size[0], size[1], len(self.annotations))
            return self.export_path
        self.log("overlay: export failed")
        return None

    def _confirm(self) -> None:
        if self._sel is None or self._sel.is_empty():
            return
        self._cancel_text()
        self._sel = self._sel.round_to_ints()
        export_path = self.export_annotated()
        self._result = OverlayResult("region", self._sel, export_path)
        self.log("overlay: selection %s, %d annotation(s)", self._sel,
                 len(self.annotations))
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

    # --------------------------------------------------------------- toolbar

    def _icon(self, window: Gtk.Window, kind: str) -> Gtk.Texture:
        if kind in self._icons:
            return self._icons[kind]
        native = window.get_native()
        renderer = native.get_renderer() if native is not None else None
        if renderer is not None:
            self._icon_renderer = renderer
        node = icon_node(kind, ICON_SIZE * 2)          # 2x for crisp scaling
        texture = None
        if self._icon_renderer is not None:
            from .annotate import render_texture
            texture = render_texture(self._icon_renderer, node,
                                     (ICON_SIZE * 2, ICON_SIZE * 2))
        if texture is None:  # pragma: no cover - fallback keeps labels usable
            return None
        self._icons[kind] = texture
        return texture

    def _icon_widget(self, window: Gtk.Window, kind: str) -> Gtk.Widget:
        texture = self._icon(window, kind)
        if texture is None:  # pragma: no cover
            return Gtk.Label(label=TOOL_LABELS.get(kind, kind)[:1])
        image = Gtk.Image.new_from_paintable(texture)
        image.set_pixel_size(ICON_SIZE)
        return image

    def _swatch_widget(self, window: Gtk.Window, color) -> Gtk.Widget:
        from .annotate import render_texture

        native = window.get_native()
        renderer = native.get_renderer() if native is not None else None
        if renderer is not None:
            self._icon_renderer = renderer
        texture = None
        if self._icon_renderer is not None:
            size = ICON_SIZE * 2
            texture = render_texture(self._icon_renderer, swatch_node(color, size),
                                     (size, size))
        if texture is None:  # pragma: no cover - cosmetic fallback
            return Gtk.Label(label="●")
        image = Gtk.Image.new_from_paintable(texture)
        image.set_pixel_size(ICON_SIZE)
        return image

    def _toolbar_needed(self) -> bool:
        return (self.allow_annotations and self._sel is not None
                and not self._sel.is_empty() and self._phase != "flash")

    def _build_toolbar(self, state: _WindowState) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.add_css_class("macshot-toolbar")
        box.set_can_focus(False)
        # A fixed height keeps the placement maths away from GtkOverlay's
        # context dependent (and here unstable) height-for-width reporting.
        box.set_size_request(-1, TOOLBAR_HEIGHT)
        buttons: dict = {}

        def add_toggle(key: str, kind: str, tooltip: str, group=None) -> Gtk.ToggleButton:
            button = Gtk.ToggleButton()
            button.set_can_focus(False)
            button.set_tooltip_text(tooltip)
            button.set_child(self._icon_widget(state.window, kind))
            if group is not None:
                button.set_group(group)
            button.connect("toggled", self._on_toggle, key)
            box.append(button)
            buttons[key] = button
            return button

        for index, tool in enumerate(TOOLS):
            add_toggle(f"tool:{tool}", tool,
                       f"{TOOL_LABELS[tool]}（{index + 1}）")

        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        first = None
        for index, color in enumerate(PALETTE):
            button = Gtk.ToggleButton()
            button.set_can_focus(False)
            button.set_tooltip_text(f"颜色 {index + 1}")
            button.set_child(self._swatch_widget(state.window, color))
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._on_toggle, f"color:{index}")
            box.append(button)
            buttons[f"color:{index}"] = button

        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        thinner = Gtk.Button()
        thinner.set_can_focus(False)
        thinner.set_tooltip_text("细一点 / 字号小一点")
        thinner.set_child(self._icon_widget(state.window, "minus"))
        thinner.connect("clicked", lambda *_a: self._set_width(self.width - 1))
        box.append(thinner)
        width_label = Gtk.Label(label=str(int(self.width)))
        width_label.add_css_class("width")
        box.append(width_label)
        thicker = Gtk.Button()
        thicker.set_can_focus(False)
        thicker.set_tooltip_text("粗一点 / 字号大一点")
        thicker.set_child(self._icon_widget(state.window, "plus"))
        thicker.connect("clicked", lambda *_a: self._set_width(self.width + 1))
        box.append(thicker)
        buttons["width"] = width_label

        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        undo = Gtk.Button()
        undo.set_can_focus(False)
        undo.set_tooltip_text("撤销（Ctrl+Z）")
        undo.set_child(self._icon_widget(state.window, "undo"))
        undo.connect("clicked", lambda *_a: (self.annotations.undo(), self._redraw()))
        box.append(undo)
        save = Gtk.Button(label="保存")
        save.add_css_class("save")
        save.set_can_focus(False)
        save.set_tooltip_text("保存（回车）")
        save.connect("clicked", lambda *_a: self._confirm())
        box.append(save)
        cancel = Gtk.Button()
        cancel.set_can_focus(False)
        cancel.set_tooltip_text("取消（Esc）")
        cancel.set_child(self._icon_widget(state.window, "close"))
        cancel.connect("clicked", lambda *_a: self._finish(OverlayResult("cancel")))
        box.append(cancel)

        state.toolbar = box
        state.buttons = buttons
        state.container.add_overlay(box)
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.START)
        self._update_toolbar_state(state)

    def _on_toggle(self, button: Gtk.ToggleButton, key: str) -> None:
        if self._syncing or not button.get_active():
            return
        if key.startswith("tool:"):
            self._set_tool(key.split(":", 1)[1])
        elif key.startswith("color:"):
            self._set_color(PALETTE[int(key.split(":", 1)[1])])

    def _update_toolbar_state(self, state: _WindowState) -> None:
        if state.toolbar is None:
            return
        self._syncing = True
        try:
            for key, button in state.buttons.items():
                if key.startswith("tool:"):
                    button.set_active(key.split(":", 1)[1] == self.tool)
                elif key.startswith("color:"):
                    button.set_active(PALETTE[int(key.split(":", 1)[1])] == self.color)
                elif key == "width":
                    button.set_text(str(int(self.width)))
        finally:
            self._syncing = False

    def _target_state(self) -> _WindowState:
        """The window that carries the toolbar: the selection's own monitor."""
        assert self._sel is not None
        point = self.layout.monitor_for_logical_point(
            self._sel.x + self._sel.width / 2, self._sel.y2)
        if point is not None:
            for state in self._windows:
                if state.monitor is point:
                    return state
        for state in self._windows:
            if state.primary:
                return state
        return self._windows[0]

    def _sync_toolbar(self) -> None:
        if not self._windows:
            return
        if not self._toolbar_needed():
            for state in self._windows:
                if state.toolbar is not None:
                    state.toolbar.set_visible(False)
            return

        target = self._target_state()
        if not self.allow_annotations:
            return
        for state in self._windows:
            if state.toolbar is None:
                if state is not target:
                    continue
                self._build_toolbar(state)
            visible = state is target
            state.toolbar.set_visible(visible)
            if not visible:
                continue
            local = self._local(state.monitor, self._sel)
            # Before the surface is mapped its size is 0; fall back to the window
            # so the very first placement is not clamped into the top-left corner.
            win_w = state.window.get_width() or state.surface.get_width() or 1920
            win_h = state.window.get_height() or state.surface.get_height() or 1080
            natural_w = state.toolbar.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            x = min(max(0.0, local.x), max(0.0, win_w - natural_w - 8))
            y = local.y2 + 10
            if y + TOOLBAR_HEIGHT > win_h - 8:
                y = max(8.0, local.y - TOOLBAR_HEIGHT - 10)   # flip above
            x, y = int(x), int(y)
            if state.position != (x, y):          # never re-invalidate in a loop
                state.position = (x, y)
                state.toolbar.set_margin_start(x)
                state.toolbar.set_margin_top(y)
                self.log("toolbar: %s local=%s win=%dx%d width=%d -> (%d, %d)",
                         state.monitor.name, local, win_w, win_h, natural_w, x, y)

    # -------------------------------------------------------------- painting

    def _texture_for(self, monitor: Monitor) -> Gdk.Texture:
        key = f"{monitor.name}:{monitor.x},{monitor.y}"
        texture = self._textures.get(key)
        if texture is None:
            texture = Gdk.Texture.new_for_pixbuf(self._slice_pixbuf(monitor))
            self._textures[key] = texture
        return texture

    def _slice_pixbuf(self, monitor: Monitor):
        key = f"slice:{monitor.name}:{monitor.x},{monitor.y}"
        slice_ = self._slices.get(key)
        if slice_ is None:
            source = self.layout.physical_crop(monitor.logical_rect, self.image_size)
            slice_ = self._pixbuf.new_subpixbuf(
                int(source.x), int(source.y),
                max(1, int(source.width)), max(1, int(source.height)))
            self._slices[key] = slice_
        return slice_

    def _window_for_surface(self, widget: Gtk.Widget) -> Gtk.Window:
        for state in self._windows:
            if state.surface is widget:
                return state.window
        return self._windows[0].window

    def paint(self, snapshot, widget: Gtk.Widget) -> None:
        state = None
        for candidate in self._windows:
            if candidate.surface is widget:
                state = candidate
                break
        if state is None:  # pragma: no cover - defensive
            return
        monitor, primary = state.monitor, state.primary
        width, height = widget.get_width(), widget.get_height()
        full = _rect(0, 0, width, height)
        snapshot.append_color(_rgba(BLACK, 1.0), full)
        snapshot.append_scaled_texture(self._texture_for(monitor),
                                       Gsk.ScalingFilter.TRILINEAR, full)

        if self._phase != "flash":
            self._paint_annotations(snapshot, widget, monitor, width, height)

        if self._phase == "flash":
            self._paint_flash(snapshot, width, height, monitor)
            return

        if self._sel is not None and not self._sel.is_empty():
            self._paint_selection(snapshot, widget, width, height, monitor, self._sel)
        elif primary and self.hints and time.monotonic() - self._last_activity > HINT_DELAY:
            # Only the primary monitor carries the cheat sheet, like the top bar.
            self._paint_hint(snapshot, widget, width, height)

    def _paint_annotations(self, snapshot, widget: Gtk.Widget, monitor: Monitor,
                           width: int, height: int) -> None:
        if not self.annotations.items:
            return
        mapper = Mapper.preview(monitor)
        selection = self._local(monitor, self._sel) if self._sel is not None else None
        snapshot.save()
        if selection is not None:
            snapshot.push_clip(_rect(selection.x, selection.y,
                                     selection.width, selection.height))
        mosaic = [a for a in self.annotations
                  if isinstance(a, Stroke) and a.kind == "mosaic"]
        if mosaic:
            slice_ = self._slice_pixbuf(monitor)
            ratio = slice_.get_width() / max(1.0, monitor.width)
            texture = self._mosaic_texture(
                f"preview:{monitor.name}:{monitor.x},{monitor.y}", slice_,
                MOSAIC_BLOCK * ratio)
            if texture is not None:
                snapshot.push_mask(Gsk.MaskMode.ALPHA)
                for annotation in mosaic:
                    paint_annotation(snapshot, annotation, mapper,
                                     self._layout_factory(self._window_for_surface(widget)),
                                     force_color=WHITE)
                snapshot.pop()
                snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.NEAREST,
                                              _rect(0, 0, width, height))
                snapshot.pop()
        factory = self._layout_factory(self._window_for_surface(widget))
        for annotation in self.annotations:
            if isinstance(annotation, Stroke) and annotation.kind == "mosaic":
                continue
            if self._drawing and annotation is self.annotations.items[-1]:
                continue                      # the draft is painted after the dim
            paint_annotation(snapshot, annotation, mapper, factory)
        if selection is not None:
            snapshot.pop()
        snapshot.restore()

    def _paint_selection(self, snapshot, widget, width: int, height: int,
                         monitor: Monitor, selection: Rect) -> None:
        local = self._local(monitor, selection)
        dragging = self._phase == "dragging"
        visible = local.intersection(Rect(0, 0, width, height))

        if not dragging:
            # macOS darkens everything outside the selection once you let go.
            # This is what makes every monitor look covered, so it is drawn even
            # on screens the selection never touches.
            self._paint_outside(snapshot, width, height, local)

        if visible is None:
            # A neighbouring monitor: end the draft here, nothing else to draw.
            self._paint_draft(snapshot, widget, monitor)
            return

        if not dragging:
            for _name, hx, hy in handle_points(local):
                self._paint_square(snapshot, hx, hy, HANDLE_SIZE)

        outer = _rect(local.x - 1.5, local.y - 1.5, local.width + 3.0, local.height + 3.0)
        snapshot.append_border(_rounded(outer, 0.0), [3.0] * 4,
                               [_rgba(BLACK, 0.55)] * 4)
        inner = _rect(local.x - 0.75, local.y - 0.75, local.width + 1.5, local.height + 1.5)
        snapshot.append_border(_rounded(inner, 0.0), [1.5] * 4,
                               [_rgba(WHITE, 0.95)] * 4)

        self._paint_draft(snapshot, widget, monitor)
        self._paint_badge(snapshot, widget, width, height, local, size_label(selection))

    def _paint_draft(self, snapshot, widget, monitor: Monitor) -> None:
        """The shape currently being dragged, painted above the dimming."""
        if not self._drawing or not self.annotations.items:
            return
        paint_annotation(snapshot, self.annotations.items[-1], Mapper.preview(monitor),
                         self._layout_factory(self._window_for_surface(widget)))

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
        # Flash the captured region only: with several monitors each window
        # would otherwise turn its whole screen white.
        local = self._local(monitor, self._sel)
        visible = local.intersection(Rect(0, 0, width, height))
        if visible is None:
            return
        snapshot.append_color(_rgba(WHITE, alpha),
                              _rect(visible.x, visible.y, visible.width, visible.height))

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
