"""Annotation model plus Gsk painting for the region overlay.

Everything the user draws is kept as small immutable vector records in *stage*
coordinates (the same space the selection lives in), so a capture can be
annotated and then re-rendered at the export resolution by swapping the mapping.

Rendering deliberately avoids cairo: this PyGObject build has no cairo foreign
struct converters (``gi._gi_cairo`` is missing), so ``PangoCairo`` and
``Gtk.Snapshot.append_cairo`` are unusable.  Gsk can do all of it -- stroked
paths, Pango layouts and, for the mosaic, a mask built from the brush strokes
(``push_mask`` needs two ``pop()`` calls: one ends the mask, one ends the
masked content).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Sequence, Union

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Graphene, Gsk, Gtk  # noqa: E402

#: logical size of one mosaic block; the export scales it like everything else
MOSAIC_BLOCK = 12.0
MIN_WIDTH = 1.0
MAX_WIDTH = 12.0
DEFAULT_WIDTH = 3.0

#: RGB triples, macOS/WeChat flavoured
PALETTE: tuple[tuple[float, float, float], ...] = (
    (1.00, 0.23, 0.19),      # 红
    (1.00, 0.78, 0.17),      # 黄
    (0.20, 0.78, 0.35),      # 绿
    (0.16, 0.55, 1.00),      # 蓝
    (0.10, 0.10, 0.12),      # 黑
    (1.00, 1.00, 1.00),      # 白
)

TOOLS = ("move", "rect", "ellipse", "arrow", "brush", "mosaic", "text")
DRAW_TOOLS = TOOLS[1:]

TOOL_LABELS = {
    "move": "移动/调整选区",
    "rect": "矩形",
    "ellipse": "圆圈",
    "arrow": "箭头",
    "brush": "画笔",
    "mosaic": "马赛克",
    "text": "文字",
}

Point = tuple[float, float]
Color = tuple[float, float, float]


@dataclass(frozen=True)
class Stroke:
    """Freehand polyline: the brush and the mosaic both use one."""

    kind: str                                   # "brush" | "mosaic"
    points: tuple[Point, ...]
    color: Color = PALETTE[0]
    width: float = DEFAULT_WIDTH


@dataclass(frozen=True)
class Box:
    """Rectangle or ellipse, defined by two opposite corners."""

    kind: str                                   # "rect" | "ellipse"
    x1: float
    y1: float
    x2: float
    y2: float
    color: Color = PALETTE[0]
    width: float = DEFAULT_WIDTH


@dataclass(frozen=True)
class Arrow:
    kind: str = "arrow"
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    color: Color = PALETTE[0]
    width: float = DEFAULT_WIDTH


@dataclass(frozen=True)
class Text:
    kind: str = "text"
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    color: Color = PALETTE[0]
    size: float = 22.0


Annotation = Union[Stroke, Box, Arrow, Text]


def text_size(width: float) -> float:
    """Text follows the same width control as the strokes."""
    return 10.0 + max(MIN_WIDTH, min(MAX_WIDTH, width)) * 4.0


def annotation_bounds(annotation: Annotation) -> tuple[float, float, float, float]:
    """Rough bounds (x1, y1, x2, y2) in stage coordinates."""
    if isinstance(annotation, Stroke):
        xs = [p[0] for p in annotation.points] or [0.0]
        ys = [p[1] for p in annotation.points] or [0.0]
        pad = annotation.width / 2
        return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    if isinstance(annotation, Box):
        return (min(annotation.x1, annotation.x2), min(annotation.y1, annotation.y2),
                max(annotation.x1, annotation.x2), max(annotation.y1, annotation.y2))
    if isinstance(annotation, Arrow):
        pad = annotation.width / 2
        return (min(annotation.x1, annotation.x2) - pad, min(annotation.y1, annotation.y2) - pad,
                max(annotation.x1, annotation.x2) + pad, max(annotation.y1, annotation.y2) + pad)
    # text: only the anchor is known without a layout, be generous
    size = annotation.size
    return (annotation.x, annotation.y, annotation.x + size * len(annotation.text) * 0.7,
            annotation.y + size * 1.4)


class AnnotationStack:
    """Undo/redo bookkeeping for the annotation list."""

    def __init__(self) -> None:
        self.items: list[Annotation] = []
        self._redo: list[Annotation] = []

    def add(self, annotation: Annotation) -> Annotation:
        self.items.append(annotation)
        self._redo.clear()
        return annotation

    def pop(self) -> Annotation | None:
        if not self.items:
            return None
        annotation = self.items.pop()
        self._redo.append(annotation)
        return annotation

    def discard_last(self) -> Annotation | None:
        """Drop a just-started shape without touching the redo stack."""
        if not self.items:
            return None
        return self.items.pop()

    def undo(self) -> bool:
        return self.pop() is not None

    def redo(self) -> bool:
        if not self._redo:
            return False
        self.items.append(self._redo.pop())
        return True

    @property
    def can_undo(self) -> bool:
        return bool(self.items)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def clear(self) -> None:
        self.items.clear()
        self._redo.clear()

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def replace_last(self, annotation: Annotation) -> None:
        """Used while dragging: keep the draft in the list so it stays visible."""
        if self.items:
            self.items[-1] = annotation
        else:
            self.items.append(annotation)


@dataclass(frozen=True)
class Mapper:
    """Maps stage coordinates onto the local pixels of a render target."""

    sx: float
    sy: float
    dx: float
    dy: float

    def point(self, x: float, y: float) -> Point:
        return x * self.sx + self.dx, y * self.sy + self.dy

    def length(self, value: float) -> float:
        return value * (self.sx + self.sy) / 2.0

    @property
    def scale(self) -> float:
        return (self.sx + self.sy) / 2.0

    @staticmethod
    def preview(monitor) -> "Mapper":
        """Stage -> window-local logical pixels of one overlay monitor."""
        return Mapper(1.0, 1.0, -monitor.x, -monitor.y)

    @staticmethod
    def identity() -> "Mapper":
        return Mapper(1.0, 1.0, 0.0, 0.0)

    @staticmethod
    def export(layout, crop, image_size: tuple[int, int]) -> "Mapper":
        """Stage -> crop-local pixels of the exported PNG."""
        kind = layout.mapping_kind(image_size)
        origin = layout.logical_bounds
        if kind == "physical":
            # Per-monitor rendering: fall back to the scale of the monitor the
            # crop lives on (the only model where a single factor is not right).
            monitor = layout.monitor_for_logical_point(
                crop.x + crop.width / 2, crop.y + crop.height / 2)
            if monitor is not None:
                scale = monitor.scale
                return Mapper(scale, scale, -monitor.x * scale - crop.x,
                              -monitor.y * scale - crop.y)
        if kind == "uniform":
            sx = image_size[0] / origin.width
            sy = image_size[1] / origin.height
        else:
            scale = layout.stage_scale(image_size) or 1.0
            sx = sy = scale
        return Mapper(sx, sy, -origin.x * sx - crop.x, -origin.y * sy - crop.y)


# ------------------------------------------------------------------- painting


def rgba(color: Color, alpha: float = 1.0) -> Gdk.RGBA:
    return Gdk.RGBA(red=color[0], green=color[1], blue=color[2], alpha=alpha)


def grect(x: float, y: float, width: float, height: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, width, height)


def _stroke_style(width: float) -> Gsk.Stroke:
    stroke = Gsk.Stroke.new(max(0.5, width))
    stroke.set_line_cap(Gsk.LineCap.ROUND)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    return stroke


def polyline_path(points: Sequence[Point], mapper: Mapper) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    mapped = [mapper.point(*p) for p in points]
    if not mapped:
        builder.move_to(0.0, 0.0)
        builder.line_to(0.01, 0.01)
        return builder.to_path()
    builder.move_to(*mapped[0])
    if len(mapped) == 1:
        # a single tap still deserves a round dot
        builder.line_to(mapped[0][0] + 0.01, mapped[0][1])
    else:
        for point in mapped[1:]:
            builder.line_to(*point)
    return builder.to_path()


def box_path(box: Box, mapper: Mapper) -> Gsk.Path:
    x1, y1 = mapper.point(min(box.x1, box.x2), min(box.y1, box.y2))
    x2, y2 = mapper.point(max(box.x1, box.x2), max(box.y1, box.y2))
    builder = Gsk.PathBuilder.new()
    builder.add_rect(grect(x1, y1, max(0.5, x2 - x1), max(0.5, y2 - y1)))
    return builder.to_path()


def ellipse_path(box: Box, mapper: Mapper) -> Gsk.Path:
    """A four-segment cubic approximation, since Gsk has no add_ellipse()."""
    x1, y1 = mapper.point(min(box.x1, box.x2), min(box.y1, box.y2))
    x2, y2 = mapper.point(max(box.x1, box.x2), max(box.y1, box.y2))
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    rx, ry = max(0.5, (x2 - x1) / 2.0), max(0.5, (y2 - y1) / 2.0)
    k = 0.5522847498307936
    builder = Gsk.PathBuilder.new()
    builder.move_to(cx - rx, cy)
    builder.cubic_to(cx - rx, cy - ry * k, cx - rx * k, cy - ry, cx, cy - ry)
    builder.cubic_to(cx + rx * k, cy - ry, cx + rx, cy - ry * k, cx + rx, cy)
    builder.cubic_to(cx + rx, cy + ry * k, cx + rx * k, cy + ry, cx, cy + ry)
    builder.cubic_to(cx - rx * k, cy + ry, cx - rx, cy + ry * k, cx - rx, cy)
    builder.close()
    return builder.to_path()


def arrow_paths(arrow: Arrow, mapper: Mapper) -> tuple[Gsk.Path, Gsk.Path]:
    """Returns (shaft, head) paths; the head is meant to be filled."""
    x1, y1 = mapper.point(arrow.x1, arrow.y1)
    x2, y2 = mapper.point(arrow.x2, arrow.y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = max(8.0, mapper.length(arrow.width) * 3.6)
    spread = math.radians(26)
    shaft = Gsk.PathBuilder.new()
    shaft.move_to(x1, y1)
    shaft.line_to(x2, y2)
    tip = (x2, y2)

    def wing(sign: float) -> Point:
        return (x2 - head * math.cos(angle + sign * spread),
                y2 - head * math.sin(angle + sign * spread))

    head_builder = Gsk.PathBuilder.new()
    head_builder.move_to(*tip)
    head_builder.line_to(*wing(1))
    head_builder.line_to(*wing(-1))
    head_builder.close()
    return shaft.to_path(), head_builder.to_path()


def paint_annotation(snapshot, annotation: Annotation, mapper: Mapper,
                     layout_factory: Callable[[str, float], object] | None = None,
                     *, force_color: Color | None = None) -> None:
    """Draw one annotation into a Gtk.Snapshot.

    ``force_color`` is used for the mosaic mask, which only cares about alpha.
    """
    color = force_color or getattr(annotation, "color", PALETTE[0])
    width = mapper.length(getattr(annotation, "width", DEFAULT_WIDTH))
    if isinstance(annotation, Stroke):
        snapshot.append_stroke(polyline_path(annotation.points, mapper),
                               _stroke_style(width), rgba(color))
    elif isinstance(annotation, Box):
        path = box_path(annotation, mapper) if annotation.kind == "rect" \
            else ellipse_path(annotation, mapper)
        snapshot.append_stroke(path, _stroke_style(width), rgba(color))
    elif isinstance(annotation, Arrow):
        shaft, head = arrow_paths(annotation, mapper)
        snapshot.append_stroke(shaft, _stroke_style(width), rgba(color))
        snapshot.append_fill(head, Gsk.FillRule.WINDING, rgba(color))
    elif isinstance(annotation, Text):
        if layout_factory is None or not annotation.text:
            return
        size = mapper.length(annotation.size)
        layout = layout_factory(annotation.text, size)
        x, y = mapper.point(annotation.x, annotation.y)
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        snapshot.append_layout(layout, rgba(color))
        snapshot.restore()


def build_layer_node(annotations: Iterable[Annotation], mapper: Mapper,
                     size: tuple[float, float],
                     layout_factory: Callable[[str, float], object] | None = None,
                     mosaic_texture=None, base_texture=None):
    """Render the whole annotation stack (optionally over a base image)."""
    width, height = size
    snapshot = Gtk.Snapshot.new()
    full = grect(0, 0, width, height)
    if base_texture is not None:
        snapshot.append_texture(base_texture, full)

    annotations = list(annotations)
    mosaic = [a for a in annotations if isinstance(a, Stroke) and a.kind == "mosaic"]
    if mosaic and mosaic_texture is not None:
        # Mask first (two pops: one closes the mask, one closes the content).
        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        for annotation in mosaic:
            paint_annotation(snapshot, annotation, mapper, layout_factory,
                             force_color=(1.0, 1.0, 1.0))
        snapshot.pop()
        snapshot.append_texture(mosaic_texture, full)
        snapshot.pop()

    for annotation in annotations:
        if isinstance(annotation, Stroke) and annotation.kind == "mosaic":
            continue
        paint_annotation(snapshot, annotation, mapper, layout_factory)
    return snapshot.to_node()


def render_texture(renderer, node, size: tuple[float, float]):
    """Rasterise a node with the window's renderer; None when unavailable."""
    if renderer is None or node is None:
        return None
    width, height = size
    return renderer.render_texture(node, grect(0, 0, width, height))


def save_node_png(renderer, node, size: tuple[float, float], destination) -> bool:
    texture = render_texture(renderer, node, size)
    if texture is None:
        return False
    data = texture.save_to_png_bytes()
    from pathlib import Path

    Path(destination).write_bytes(data.get_data())
    return True


def pixelate_pixbuf(pixbuf, block: float):
    """Cheap mosaic source: average blocks by shrinking, then blow back up."""
    block = max(2, int(round(block)))
    width, height = pixbuf.get_width(), pixbuf.get_height()
    small_w = max(1, width // block)
    small_h = max(1, height // block)
    small = pixbuf.scale_simple(small_w, small_h, GdkPixbuf.InterpType.BILINEAR)
    return small.scale_simple(width, height, GdkPixbuf.InterpType.NEAREST)


def clone_with(annotation: Annotation, **changes) -> Annotation:
    return replace(annotation, **changes)
