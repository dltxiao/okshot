"""Monitor layout, read from Mutter's DisplayConfig D-Bus API.

``org.gnome.Mutter.DisplayConfig.GetCurrentState`` is *not* behind GNOME's
sender allow-list (unlike ``org.gnome.Shell.Introspect``), and it is the only
way to learn the per-monitor fractional scale factor without asking GTK.  That
scale is what maps a selection made in logical pixels onto the physical pixel
buffer that the portal screenshot returns.
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


class Monitor:
    """One logical monitor: a physical pixel rect plus its scale factor."""

    __slots__ = ("name", "x", "y", "width", "height", "scale", "primary")

    def __init__(self, name: str, x: int, y: int, width: int, height: int,
                 scale: float, primary: bool = False) -> None:
        self.name = name
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.scale = scale or 1.0
        self.primary = primary

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (f"Monitor({self.name!r}, phys=({self.x},{self.y},{self.width},"
                f"{self.height}), scale={self.scale}, primary={self.primary})")

    @property
    def physical_rect(self) -> Rect:
        return Rect(self.x, self.y, self.width, self.height)

    @property
    def logical_rect(self) -> Rect:
        return Rect(self.logical_x, self.logical_y,
                    self.width / self.scale, self.height / self.scale)

    @property
    def logical_x(self) -> float:
        return self.x / self.scale

    @property
    def logical_y(self) -> float:
        return self.y / self.scale

    def physical_crop(self, logical: Rect) -> Rect:
        """Map a global logical rect onto pixels of this monitor's area."""
        return Rect(
            self.x + (logical.x - self.logical_x) * self.scale,
            self.y + (logical.y - self.logical_y) * self.scale,
            logical.width * self.scale,
            logical.height * self.scale,
        ).round_to_ints()


class Layout:
    """Every logical monitor, plus helpers to reason about the whole desktop."""

    def __init__(self, monitors: list[Monitor]) -> None:
        if not monitors:
            raise ValueError("a layout needs at least one monitor")
        self.monitors = monitors

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Layout({self.monitors!r})"

    @property
    def logical_bounds(self) -> Rect:
        return _union(m.logical_rect for m in self.monitors)

    @property
    def physical_bounds(self) -> Rect:
        return _union(m.physical_rect for m in self.monitors)

    def monitor_for_logical_point(self, px: float, py: float) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.logical_rect.contains(px, py):
                return monitor
        return None

    def monitor_matching(self, logical: Rect, tolerance: float = 2.0) -> Monitor | None:
        """Find the monitor whose logical rect matches ``logical`` (as GTK reports it)."""
        best, best_score = None, None
        for monitor in self.monitors:
            r = monitor.logical_rect
            score = (abs(r.x - logical.x) + abs(r.y - logical.y)
                     + abs(r.width - logical.width) + abs(r.height - logical.height))
            if score <= tolerance and (best_score is None or score < best_score):
                best, best_score = monitor, score
        return best

    def physical_crop(self, logical: Rect, image_size: tuple[int, int]) -> Rect:
        """Crop rectangle in screenshot pixels for a global logical selection.

        Falls back to a uniform scale when the pixel buffer disagrees with the
        layout Mutter reported (mixed-DPI setups, non-Mutter compositors, ...).
        """
        if not self._matches_image(image_size):
            bounds = self.logical_bounds
            scale_x = image_size[0] / bounds.width
            scale_y = image_size[1] / bounds.height
            return Rect((logical.x - bounds.x) * scale_x,
                        (logical.y - bounds.y) * scale_y,
                        logical.width * scale_x,
                        logical.height * scale_y).round_to_ints()

        origin = self.physical_bounds
        centre = self.monitor_for_logical_point(logical.x + logical.width / 2,
                                                logical.y + logical.height / 2)
        monitor = centre or self.monitors[0]
        crop = monitor.physical_crop(logical).translated(-origin.x, -origin.y)
        # Never step outside the buffer.
        x = min(max(0.0, crop.x), image_size[0] - 1)
        y = min(max(0.0, crop.y), image_size[1] - 1)
        width = min(crop.width, image_size[0] - x)
        height = min(crop.height, image_size[1] - y)
        return Rect(x, y, max(1.0, width), max(1.0, height))

    def _matches_image(self, image_size: tuple[int, int], tolerance: int = 2) -> bool:
        bounds = self.physical_bounds
        return (abs(bounds.width - image_size[0]) <= tolerance
                and abs(bounds.height - image_size[1]) <= tolerance)


def _union(rects) -> Rect:
    rects = list(rects)
    x1 = min(r.x for r in rects)
    y1 = min(r.y for r in rects)
    x2 = max(r.x2 for r in rects)
    y2 = max(r.y2 for r in rects)
    return Rect(x1, y1, x2 - x1, y2 - y1)


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
    for x, y, scale, _transform, primary, monitor_specs, _logical_props in logical_raw:
        name = monitor_specs[0][0] if monitor_specs else f"monitor-{len(monitors)}"
        width = height = 0
        for spec in monitor_specs:
            entry = by_connector.get(spec[0])
            if entry is None:
                continue
            current = next((m for m in entry[1] if m[6].get("is-current")), None)
            if current is None:
                continue
            width += current[1]
            height = max(height, current[2])
        if width == 0 or height == 0:
            # Last resort: assume the layout entry itself is already physical.
            width, height = int(1920 * scale), int(1080 * scale)
        monitors.append(Monitor(name, int(x), int(y), int(width), int(height),
                                float(scale), bool(primary)))
    return Layout(monitors)


def fallback_layout(image_size: tuple[int, int]) -> Layout:
    """Single 1:1 monitor, used when Mutter's API is unavailable."""
    return Layout([Monitor("screen", 0, 0, image_size[0], image_size[1], 1.0, True)])
