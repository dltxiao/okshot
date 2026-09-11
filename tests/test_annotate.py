"""Tests for the annotation model and its Gsk rendering."""

import math
import unittest

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, GdkPixbuf, Gtk  # noqa: E402

from macshot.annotate import (  # noqa: E402
    MOSAIC_BLOCK, PALETTE, AnnotationStack, Arrow, Box, Mapper, Stroke, Text,
    annotation_bounds, build_layer_node, pixelate_pixbuf, save_node_png,
    text_size,
)
from macshot.monitors import Layout, Monitor  # noqa: E402
from macshot.selection import Rect  # noqa: E402


class AnnotationStackTests(unittest.TestCase):
    def setUp(self):
        self.stack = AnnotationStack()
        self.stroke = Stroke("brush", ((0, 0), (10, 10)))

    def test_add_undo_redo(self):
        self.assertFalse(self.stack.can_undo)
        self.stack.add(self.stroke)
        self.assertEqual(len(self.stack), 1)
        self.assertTrue(self.stack.can_undo)
        self.assertTrue(self.stack.undo())
        self.assertEqual(len(self.stack), 0)
        self.assertTrue(self.stack.can_redo)
        self.assertTrue(self.stack.redo())
        self.assertEqual(len(self.stack), 1)

    def test_new_annotation_clears_redo(self):
        self.stack.add(self.stroke)
        self.stack.undo()
        self.stack.add(Box("rect", 0, 0, 5, 5))
        self.assertFalse(self.stack.can_redo)

    def test_replace_last_keeps_the_draft_in_place(self):
        self.stack.add(self.stroke)
        bigger = Box("rect", 0, 0, 9, 9)
        self.stack.replace_last(bigger)
        self.assertEqual(len(self.stack), 1)
        self.assertIs(next(iter(self.stack)), bigger)

    def test_undo_on_empty_stack_is_harmless(self):
        self.assertFalse(self.stack.undo())
        self.assertFalse(self.stack.redo())


class MapperTests(unittest.TestCase):
    def test_preview_maps_stage_to_window_local(self):
        monitor = Monitor("eDP-1", 0, 1080, 1536, 960, 1.25, True)
        mapper = Mapper.preview(monitor)
        self.assertEqual(mapper.point(300, 1200), (300.0, 120.0))
        self.assertEqual(mapper.length(4), 4.0)

    def test_export_uses_the_global_stage_scale(self):
        external = Monitor("HDMI-1", 0, 0, 1920, 1080, 1.0)
        laptop = Monitor("eDP-1", 0, 1080, 1536, 960, 1.25, True)
        layout = Layout([external, laptop])
        image = (2400, 2550)
        crop = layout.physical_crop(Rect(300, 1200, 500, 300), image)
        mapper = Mapper.export(layout, crop, image)
        # stage (300, 1200) is the crop's own origin
        self.assertEqual(mapper.point(300, 1200), (0.0, 0.0))
        self.assertEqual(mapper.point(500, 1400), (250.0, 250.0))
        self.assertEqual(mapper.length(10), 12.5)

    def test_export_handles_a_negative_origin_layout(self):
        laptop = Monitor("eDP-1", 0, 0, 1536, 960, 1.25, True)
        left = Monitor("DP-1", -1920, 0, 1920, 1080, 1.0)
        layout = Layout([laptop, left])
        image = (4320, 1350)          # stage 3456x1080 at 1.25
        crop = layout.physical_crop(Rect(-1800, 100, 100, 100), image)
        mapper = Mapper.export(layout, crop, image)
        self.assertEqual(mapper.point(-1800, 100), (0.0, 0.0))


class GeometryTests(unittest.TestCase):
    def test_text_size_follows_the_width_control(self):
        self.assertEqual(text_size(1), 14.0)
        self.assertEqual(text_size(12), 58.0)
        self.assertLess(text_size(2), text_size(8))

    def test_bounds_of_each_shape(self):
        self.assertEqual(annotation_bounds(Box("rect", 10, 20, 40, 60)),
                         (10, 20, 40, 60))
        self.assertEqual(annotation_bounds(Arrow(x1=0, y1=0, x2=10, y2=0, width=4)),
                         (-2, -2, 12, 2))
        xs = annotation_bounds(Stroke("brush", ((5, 5), (15, 5)), width=2))
        self.assertEqual(xs, (4, 4, 16, 6))
        text = annotation_bounds(Text(x=100, y=50, text="abc", size=20))
        self.assertEqual(text[0], 100)
        self.assertGreater(text[2], 100)


class PixelateTests(unittest.TestCase):
    def test_blocks_become_uniform(self):
        width = height = 48
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        pixbuf.fill(0x203040)
        blocky = pixelate_pixbuf(pixbuf, 8)
        self.assertEqual((blocky.get_width(), blocky.get_height()), (width, height))
        pixels = blocky.get_pixels()
        stride = blocky.get_rowstride()
        channels = blocky.get_n_channels()
        values = {pixels[y * stride + x * channels] for y in range(0, 16) for x in range(0, 16)}
        self.assertEqual(len(values), 1)

    def test_small_images_do_not_crash(self):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 4, 4)
        pixbuf.fill(0x808080)
        blocky = pixelate_pixbuf(pixbuf, 12)
        self.assertEqual((blocky.get_width(), blocky.get_height()), (4, 4))


def _gradient(width: int, height: int):
    """A smooth gradient: every pixel differs, so pixelation is measurable."""
    stride = width * 3
    data = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * 3
            data[offset] = 30 + (x * 120 // max(1, width))
            data[offset + 1] = 60 + (y * 120 // max(1, height))
            data[offset + 2] = 150
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, False, 8,
        width, height, stride)


def _pixel(pixbuf, x, y):
    pixels = pixbuf.get_pixels()
    stride = pixbuf.get_rowstride()
    channels = pixbuf.get_n_channels()
    offset = y * stride + x * channels
    return tuple(pixels[offset:offset + 3])


def with_renderer(callback):
    """Run ``callback(renderer, layout_factory)`` inside a tiny GTK window."""
    if not Gtk.init_check():
        raise unittest.SkipTest("no display available")
    result = {}
    application = Gtk.Application(application_id="dev.macshot.anntest",
                                 flags=__import__("gi").repository.Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate(app):
        window = Gtk.ApplicationWindow(application=app)
        window.set_decorated(False)
        window.set_default_size(1, 1)
        window.set_opacity(0.0)
        window.present()

        def layout_factory(text, size):
            layout = window.create_pango_layout("")
            layout.set_text(text, -1)
            from gi.repository import Pango
            layout.set_font_description(
                Pango.FontDescription.from_string(f"Sans {int(round(size))}"))
            return layout

        def work():
            try:
                result["value"] = callback(window.get_native().get_renderer(), layout_factory)
            except Exception as exc:  # pragma: no cover - surfaced by the test
                result["error"] = exc
            app.quit()
            return False

        GLib.timeout_add(400, work)

    application.connect("activate", on_activate)
    GLib.timeout_add_seconds(20, application.quit)
    application.run([])
    if "error" in result:
        raise result["error"]
    return result.get("value")


class RenderTests(unittest.TestCase):
    """Rasterisation checks; each one runs inside a throwaway 1x1 window."""

    WIDTH, HEIGHT = 240, 160

    def _render(self, annotations, mosaic=None, base=None):
        def callback(renderer, layout_factory):
            from gi.repository import Gdk
            size = (self.WIDTH, self.HEIGHT)
            mosaic_texture = Gdk.Texture.new_for_pixbuf(mosaic) if mosaic is not None else None
            base_texture = Gdk.Texture.new_for_pixbuf(base) if base is not None else None
            node = build_layer_node(annotations, Mapper.identity(), size, layout_factory,
                                    mosaic_texture=mosaic_texture, base_texture=base_texture)
            import pathlib
            target = pathlib.Path("/tmp/macshot_anno_test.png")
            self.assertTrue(save_node_png(renderer, node, size, target))
            return GdkPixbuf.Pixbuf.new_from_file(str(target))
        return with_renderer(callback)

    def test_base_image_survives_the_render_untouched(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        rendered = self._render([], base=base)
        worst = 0
        for y in range(0, self.HEIGHT, 5):
            for x in range(0, self.WIDTH, 5):
                a, b = _pixel(rendered, x, y), _pixel(base, x, y)
                worst = max(worst, max(abs(a[i] - b[i]) for i in range(3)))
        self.assertEqual(worst, 0)

    def test_stroke_only_paints_where_it_was_drawn(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        stroke = Stroke("brush", ((20, 20), (200, 20)), PALETTE[0], 6)
        rendered = self._render([stroke], base=base)
        self.assertNotEqual(_pixel(rendered, 120, 20), _pixel(base, 120, 20))
        self.assertEqual(_pixel(rendered, 120, 120), _pixel(base, 120, 120))

    def test_rectangle_paints_its_outline_not_its_inside(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        box = Box("rect", 40, 40, 200, 120, PALETTE[1], 4)
        rendered = self._render([box], base=base)
        self.assertNotEqual(_pixel(rendered, 40, 80), _pixel(base, 40, 80))
        self.assertEqual(_pixel(rendered, 120, 80), _pixel(base, 120, 80))

    def test_text_paints_cjk_glyphs(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        text = Text(x=20, y=50, text="中文标注", color=PALETTE[5], size=28)
        rendered = self._render([text], base=base)
        changed = sum(1 for y in range(40, 100) for x in range(20, 140)
                      if _pixel(rendered, x, y) != _pixel(base, x, y))
        self.assertGreater(changed, 50)
        # white glyphs somewhere in the text area
        self.assertTrue(any(_pixel(rendered, x, y)[0] > 200
                            for y in range(40, 100) for x in range(20, 140)))

    def test_mosaic_is_confined_to_the_masked_stroke(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        stroke = Stroke("mosaic", ((30, 80), (210, 80)), width=20)
        mosaic = pixelate_pixbuf(base, MOSAIC_BLOCK)
        rendered = self._render([stroke], mosaic=mosaic, base=base)

        # inside the stroke: neighbouring pixels of a block are identical
        row = {_pixel(rendered, x, 80) for x in range(48, 60)}
        self.assertEqual(len(row), 1, row)
        # and the pixels are the pixelated image, not the original detail
        matching = sum(1 for x in range(34, 206)
                       if _pixel(rendered, x, 80) == _pixel(mosaic, x, 80))
        self.assertGreater(matching, 160, matching)
        self.assertGreater(len({_pixel(base, x, 80) for x in range(48, 60)}), 1)
        # outside the stroke the base is untouched
        self.assertEqual(_pixel(rendered, 54, 30), _pixel(base, 54, 30))

    def test_mosaic_does_not_swallow_later_annotations(self):
        base = _gradient(self.WIDTH, self.HEIGHT)
        mosaic = pixelate_pixbuf(base, MOSAIC_BLOCK)
        annotations = [
            Stroke("mosaic", ((20, 20), (220, 140)), width=30),
            Box("rect", 40, 40, 200, 120, PALETTE[0], 5),
        ]
        rendered = self._render(annotations, mosaic=mosaic, base=base)
        self.assertNotEqual(_pixel(rendered, 40, 80), _pixel(base, 40, 80))


if __name__ == "__main__":
    unittest.main()
