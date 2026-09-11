"""Pure selection geometry.

Everything in here works on ``Rect`` objects and plain numbers so it can be
unit-tested without a display.  Two coordinate spaces are used throughout:

``logical``
    What GTK, ``Gdk.Monitor`` and the pointer use: device independent pixels,
    already divided by the fractional scale factor of the monitor.
``physical``
    What the screenshot pixel buffer uses: 1920x1200 for a 1920x1200 panel even
    when the desktop is drawn at a 1.25 scale factor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# Handle identifiers, ordered like the visual layout
HANDLES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")

_RESIZE_CURSORS = {
    "nw": "nwse-resize", "se": "nwse-resize",
    "ne": "nesw-resize", "sw": "nesw-resize",
    "n": "ns-resize", "s": "ns-resize",
    "e": "ew-resize", "w": "ew-resize",
    "move": "move",
}


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    # -- derived ---------------------------------------------------------
    @property
    def x2(self) -> float:
        return self.x + self.width

    @property
    def y2(self) -> float:
        return self.y + self.height

    @property
    def area(self) -> float:
        return self.width * self.height

    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x2 and self.y <= py <= self.y2

    def intersection(self, other: "Rect") -> "Rect | None":
        x1 = max(self.x, other.x)
        y1 = max(self.y, other.y)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def round_to_ints(self) -> "Rect":
        """Snap to whole pixels, always keeping a non-empty box."""
        x = float(round(self.x))
        y = float(round(self.y))
        w = float(max(1, round(self.width)))
        h = float(max(1, round(self.height)))
        return Rect(x, y, w, h)

    def translated(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.width, self.height)


def rect_from_points(x1: float, y1: float, x2: float, y2: float) -> Rect:
    """Build a normalised rect from two arbitrary corners."""
    return Rect(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))


def clamp_rect(rect: Rect, bounds: Rect) -> Rect:
    """Keep ``rect`` inside ``bounds`` without changing its size (if it fits)."""
    width = min(rect.width, bounds.width)
    height = min(rect.height, bounds.height)
    x = min(max(rect.x, bounds.x), bounds.x2 - width)
    y = min(max(rect.y, bounds.y), bounds.y2 - height)
    return Rect(x, y, width, height)


def resize_rect(rect: Rect, handle: str, dx: float, dy: float, *,
                min_size: float = 1.0, bounds: Rect | None = None) -> Rect:
    """Move one edge/corner of ``rect`` by (dx, dy)."""
    x1, y1, x2, y2 = rect.x, rect.y, rect.x2, rect.y2
    if handle == "move":
        moved = rect.translated(dx, dy)
        return clamp_rect(moved, bounds) if bounds else moved
    if "n" in handle:
        y1 += dy
    if "s" in handle:
        y2 += dy
    if "w" in handle:
        x1 += dx
    if "e" in handle:
        x2 += dx
    if x2 - x1 < min_size:
        if "w" in handle:
            x1 = x2 - min_size
        else:
            x2 = x1 + min_size
    if y2 - y1 < min_size:
        if "n" in handle:
            y1 = y2 - min_size
        else:
            y2 = y1 + min_size
    result = Rect(x1, y1, x2 - x1, y2 - y1)
    if bounds is not None:
        # Clip instead of translate: resizing should never move the fixed edge.
        nx1 = max(result.x, bounds.x)
        ny1 = max(result.y, bounds.y)
        nx2 = min(result.x2, bounds.x2)
        ny2 = min(result.y2, bounds.y2)
        result = Rect(nx1, ny1, max(min_size, nx2 - nx1), max(min_size, ny2 - ny1))
    return result


def hit_test(rect: Rect, px: float, py: float, tolerance: float) -> str | None:
    """Return the handle under the pointer, ``'move'`` inside, else ``None``."""
    near_left = abs(px - rect.x) <= tolerance
    near_right = abs(px - rect.x2) <= tolerance
    near_top = abs(py - rect.y) <= tolerance
    near_bottom = abs(py - rect.y2) <= tolerance
    inside_x = rect.x - tolerance <= px <= rect.x2 + tolerance
    inside_y = rect.y - tolerance <= py <= rect.y2 + tolerance
    if not (inside_x and inside_y):
        return None

    if near_left and near_top:
        return "nw"
    if near_right and near_top:
        return "ne"
    if near_left and near_bottom:
        return "sw"
    if near_right and near_bottom:
        return "se"
    if near_top:
        return "n"
    if near_bottom:
        return "s"
    if near_left:
        return "w"
    if near_right:
        return "e"
    if rect.contains(px, py):
        return "move"
    return None


def cursor_for_handle(handle: str | None) -> str:
    if handle is None:
        return "crosshair"
    return _RESIZE_CURSORS.get(handle, "crosshair")


def handle_points(rect: Rect) -> Iterable[tuple[str, float, float]]:
    """Centre point of every resize handle."""
    cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
    return (
        ("nw", rect.x, rect.y),
        ("n", cx, rect.y),
        ("ne", rect.x2, rect.y),
        ("e", rect.x2, cy),
        ("se", rect.x2, rect.y2),
        ("s", cx, rect.y2),
        ("sw", rect.x, rect.y2),
        ("w", rect.x, cy),
    )


def size_label(rect: Rect) -> str:
    """macOS style ``1280 × 720`` badge text."""
    return f"{int(round(rect.width))} × {int(round(rect.height))}"
