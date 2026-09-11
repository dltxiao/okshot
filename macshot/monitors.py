"""Monitor layout, read from Mutter's DisplayConfig D-Bus API.

``org.gnome.Mutter.DisplayConfig.GetCurrentState`` is *not* behind GNOME's
sender allow-list (unlike ``org.gnome.Shell.Introspect``), and it is the only
way to learn the per-monitor fractional scale factor without asking GTK.

Two coordinate spaces matter here:

``stage`` (a.k.a. logical)
    What GTK, ``Gdk.Monitor`` and the pointer use.  Mutter reports a logical
    monitor's *position* in layout units and its *size* divided by the scale
    factor, so a 1920x1200 panel at 1.25 sits at its raw (x, y) but measures
    1536x960.  ``Gdk.Monitor.get_geometry()`` returns exactly this, which is
    what lets the overlay pair a GTK monitor with a layout monitor.

``image``
    Pixels of the PNG the portal hands back.  Mutter renders a screenshot from
    the whole stage with one global scale factor, so with several monitors the
    image is ``stage bounds * stage scale`` -- *not* the union of the physical
    panel rects.  A 1920x1080@1.0 monitor above a 1920x1200@1.25 one produces a
    2400x2550 image (1920x2040 stage, scale 1.25), which is why the stage model
    is tried first and the per-monitor physical model only as a fallback.
"""

from __future__ import annotations

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .selection import Rect  # noqa: E402

MUTTER_BUS = "org.gnome.Mutter.DisplayConfig"
MUTTER_PATH = "/org/gnome/Mutter/DisplayConfig"
MUTTER_IFACE = "org.gnome.Mutter.DisplayConfig"

# (out u serial, out a((ssss)a(siiddada{sv})a{sv}) monitors,
#  out a(iiduba(ssss)a{sv}) logical_monitors, out a{sv} properties)
_CURRENT_STATE_TYPE = ("(ua((ssss)a(siiddada{sv})a{sv})"
                       "a(iiduba(ssss)a{sv})a{sv})")

#: how far an image may deviate from a model before that model is rejected
SIZE_TOLERANCE = 4

#: wl_output transforms that swap a monitor's width and height
_ROTATED_TRANSFORMS = (1, 3, 5, 7)


class Monitor:
    """One logical monitor: its stage rect plus the fractional scale factor."""

    __slots__ = ("name", "x", "y", "width", "height", "scale", "primary")

    def __init__(self, name: str, x: int, y: int, width: int, height: int,
                 scale: float, primary: bool = False) -> None:
        self.name = name
        #: stage position in layout units (NOT divided by the scale factor)
        self.x = x
        self.y = y
        #: logical size (physical panel size divided by the scale factor)
        self.width = width
        self.height = height
        self.scale = scale or 1.0
        self.primary = primary

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"Monitor({self.name!r}, stage=({self.x},{self.y},"
                f"{self.width},{self.height}), scale={self.scale}, "
                f"primary={self.primary})")

    @property
    def logical_rect(self) -> Rect:
        """Rect in stage coordinates; equals ``Gdk.Monitor.get_geometry()``."""
        return Rect(self.x, self.y, self.width, self.height)

    @property
    def physical_rect(self) -> Rect:
        """Panel pixels -- only used by the per-monitor fallback model."""
        return Rect(self.x, self.y, self.width * self.scale, self.height * self.scale)


def _union(rects) -> Rect:
    rects = list(rects)
    x1 = min(r.x for r in rects)
    y1 = min(r.y for r in rects)
    x2 = max(r.x2 for r in rects)
    y2 = max(r.y2 for r in rects)
    return Rect(x1, y1, x2 - x1, y2 - y1)


def _close(a: tuple[float, float], b: tuple[float, float],
           tolerance: float = SIZE_TOLERANCE) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


class Layout:
    """Every logical monitor, plus the mapping onto screenshot pixels."""

    def __init__(self, monitors: list[Monitor]) -> None:
        if not monitors:
            raise ValueError("a layout needs at least one monitor")
        self.monitors = monitors

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Layout({self.monitors!r})"

    @property
    def logical_bounds(self) -> Rect:
        """Bounding box of the stage: the image is a scaled copy of it."""
        return _union(m.logical_rect for m in self.monitors)

    @property
    def physical_bounds(self) -> Rect:
        """Union of the panels in pixels (the single-monitor model)."""
        return _union(m.physical_rect for m in self.monitors)

    # ------------------------------------------------------------- matching

    def monitor_for_logical_point(self, px: float, py: float) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.logical_rect.contains(px, py):
                return monitor
        return None

    def monitor_matching(self, logical: Rect, connector: str | None = None,
                         tolerance: float = SIZE_TOLERANCE) -> Monitor | None:
        """Pair a ``Gdk.Monitor`` with the layout entry it belongs to.

        Connector names are unique and stable so they win; geometry and the
        largest overlap follow.  Picking the wrong monitor would silently paint
        another screen's slice of the frozen image on it.
        """
        if connector:
            for monitor in self.monitors:
                if monitor.name == connector:
                    return monitor

        best, best_score = None, None
        for monitor in self.monitors:
            r = monitor.logical_rect
            score = (abs(r.x - logical.x) + abs(r.y - logical.y)
                     + abs(r.width - logical.width) + abs(r.height - logical.height))
            if best_score is None or score < best_score:
                best, best_score = monitor, score
        if best is not None and best_score is not None and best_score <= tolerance:
            return best

        overlapping = [(m.logical_rect.intersection(logical), m) for m in self.monitors]
        overlapping = [(rect, m) for rect, m in overlapping if rect is not None]
        if overlapping:
            return max(overlapping, key=lambda item: item[0].area)[1]
        return None

    # -------------------------------------------------------------- mapping

    def stage_scale(self, image_size: tuple[int, int],
                    tolerance: float = SIZE_TOLERANCE) -> float | None:
        """Global scale factor, if the image is a scaled copy of the stage."""
        bounds = self.logical_bounds
        if bounds.width <= 0 or bounds.height <= 0:
            return None
        scale = image_size[0] / bounds.width
        if _close((bounds.width * scale, bounds.height * scale), image_size, tolerance):
            return scale
        return None

    def mapping_kind(self, image_size: tuple[int, int]) -> str:
        """Which model explains the pixel buffer: stage, physical or uniform."""
        if self.stage_scale(image_size) is not None:
            return "stage"
        if _close((self.physical_bounds.width, self.physical_bounds.height), image_size):
            return "physical"
        return "uniform"

    def physical_crop(self, logical: Rect, image_size: tuple[int, int]) -> Rect:
        """Crop rectangle in image pixels for a stage-space selection."""
        kind = self.mapping_kind(image_size)
        origin = self.logical_bounds
        if kind == "stage":
            scale = self.stage_scale(image_size) or 1.0
            crop = Rect((logical.x - origin.x) * scale,
                        (logical.y - origin.y) * scale,
                        logical.width * scale, logical.height * scale)
        elif kind == "physical":
            panel_origin = self.physical_bounds
            monitor = self.monitor_for_logical_point(logical.x + logical.width / 2,
                                                    logical.y + logical.height / 2)
            monitor = monitor or self.monitors[0]
            crop = Rect(monitor.x + (logical.x - monitor.x) * monitor.scale,
                        monitor.y + (logical.y - monitor.y) * monitor.scale,
                        logical.width * monitor.scale,
                        logical.height * monitor.scale)
            crop = crop.translated(-panel_origin.x, -panel_origin.y)
        else:
            scale_x = image_size[0] / origin.width
            scale_y = image_size[1] / origin.height
            crop = Rect((logical.x - origin.x) * scale_x,
                        (logical.y - origin.y) * scale_y,
                        logical.width * scale_x, logical.height * scale_y)
        return _clip(crop.round_to_ints(), image_size)


def _clip(rect: Rect, image_size: tuple[int, int]) -> Rect:
    """Keep a crop inside the pixel buffer."""
    x = min(max(0.0, rect.x), max(0, image_size[0] - 1))
    y = min(max(0.0, rect.y), max(0, image_size[1] - 1))
    width = min(rect.width, image_size[0] - x)
    height = min(rect.height, image_size[1] - y)
    return Rect(x, y, max(1.0, width), max(1.0, height))


def logical_size(monitor_specs, by_connector: dict, scale: float,
                 transform: int) -> tuple[int, int] | None:
    """Logical (stage) size of one logical monitor, in layout units.

    A logical monitor holds either a single panel or a mirror group, so the
    panels are combined with ``max`` -- summing them (as a naive reading of
    ``monitor_specs`` suggests) would report a mirrored pair of 1920x1080
    screens as 3840x1080.  Rotated panels swap width and height.
    """
    width = height = 0
    for spec in monitor_specs:
        entry = by_connector.get(spec[0])
        if entry is None:
            continue
        current = next((mode for mode in entry[1] if mode[6].get("is-current")), None)
        if current is None:
            continue
        panel_w, panel_h = current[1], current[2]
        if transform in _ROTATED_TRANSFORMS:
            panel_w, panel_h = panel_h, panel_w
        width = max(width, panel_w)
        height = max(height, panel_h)
    if width <= 0 or height <= 0:
        return None
    return int(round(width / scale)), int(round(height / scale))


def get_layout(timeout_ms: int = 5000) -> Layout:
    """Query Mutter for the current monitor layout."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    if bus is None:
        raise RuntimeError("cannot connect to the session bus")
    reply = bus.call_sync(
        MUTTER_BUS, MUTTER_PATH, MUTTER_IFACE, "GetCurrentState", None,
        GLib.VariantType(_CURRENT_STATE_TYPE),
        Gio.DBusCallFlags.NONE, timeout_ms, None,
    )
    _serial, monitors_raw, logical_raw, _props = reply.unpack()
    by_connector = {entry[0][0]: entry for entry in monitors_raw}
    monitors: list[Monitor] = []
    for x, y, scale, transform, primary, monitor_specs, _logical_props in logical_raw:
        name = monitor_specs[0][0] if monitor_specs else f"monitor-{len(monitors)}"
        size = logical_size(monitor_specs, by_connector, scale, transform)
        if size is None:                      # unknown mode: assume 1080p
            size = int(round(1920 / scale)) if scale > 1 else 1920, 1080
        monitors.append(Monitor(name, int(x), int(y), size[0], size[1],
                                float(scale), bool(primary)))
    return Layout(monitors)


def fallback_layout(image_size: tuple[int, int]) -> Layout:
    """Single 1:1 monitor, used when Mutter's API is unavailable."""
    return Layout([Monitor("screen", 0, 0, image_size[0], image_size[1], 1.0, True)])
