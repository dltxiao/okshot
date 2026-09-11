"""Tool interaction tests: drive the overlay's input handlers directly.

These stay display free by stubbing the two things that need a compositor --
the toolbar sync and the surface's redraw -- so the drawing state machine
(which tool produces which annotation, what gets rejected, undo, ...) is
covered by fast unit tests.
"""

import unittest

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkPixbuf  # noqa: E402

from macshot.annotate import PALETTE, TOOLS, Arrow, Box, Stroke, Text  # noqa: E402
from macshot.monitors import Layout, Monitor  # noqa: E402
from macshot.overlay import SelectionOverlay, _WindowState  # noqa: E402
from macshot.selection import Rect  # noqa: E402

SEL = Rect(100.0, 100.0, 400.0, 300.0)


class _Surface:
    def __init__(self):
        self.draws = 0

    def queue_draw(self):
        self.draws += 1


class _Window:
    """Stand-in for the GTK window: only the cursor is touched by the tests."""

    def __init__(self):
        self.cursor = None

    def set_cursor(self, cursor):
        self.cursor = cursor


class _Entry:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text

    def get_parent(self):
        return None


def build_overlay():
    monitor = Monitor("eDP-1", 0, 0, 1536, 960, 1.25, True)
    layout = Layout([monitor])
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 64, 64)
    pixbuf.fill(0x404040)
    # bypass __init__'s file loading by pointing it at a temporary PNG
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "frozen.png"
    pixbuf.savev(str(path), "png", [], [])
    overlay = SelectionOverlay(str(path), layout, logger=lambda *a, **k: None)
    overlay._sync_toolbar = lambda: None            # needs real widgets
    overlay._windows = [_WindowState(_Window(), _Surface(), None, monitor, True)]
    overlay._sel = SEL
    overlay._phase = "adjusting"
    return overlay


def press(overlay, x, y, n=1):
    overlay._on_pressed(None, n, x, y, overlay._windows[0].window)


def motion(overlay, x, y):
    overlay._on_motion(None, x, y, overlay._windows[0].window)


def release(overlay, x, y, n=1):
    overlay._on_released(None, n, x, y, overlay._windows[0].window)


class DrawingToolTests(unittest.TestCase):
    def setUp(self):
        self.overlay = build_overlay()
        self.overlay.color = PALETTE[3]
        self.overlay.width = 5.0

    def draw(self, tool, points):
        self.overlay.tool = tool
        press(self.overlay, *points[0])
        for point in points[1:]:
            motion(self.overlay, *point)
        release(self.overlay, *points[-1])

    def test_brush_records_a_polyline(self):
        self.draw("brush", [(150, 150), (170, 160), (190, 180), (210, 200)])
        self.assertEqual(len(self.overlay.annotations), 1)
        stroke = self.overlay.annotations.items[0]
        self.assertIsInstance(stroke, Stroke)
        self.assertEqual(stroke.kind, "brush")
        self.assertEqual(stroke.points[0], (150.0, 150.0))
        self.assertEqual(stroke.points[-1], (210.0, 200.0))
        self.assertEqual(stroke.color, PALETTE[3])
        self.assertEqual(stroke.width, 5.0)

    def test_brush_drops_samples_that_are_too_close(self):
        self.draw("brush", [(150, 150), (150.2, 150.1), (180, 180)])
        points = self.overlay.annotations.items[0].points
        self.assertEqual(len(points), 2, points)

    def test_single_click_still_leaves_a_dot(self):
        self.draw("brush", [(150, 150), (150, 150)])
        self.assertEqual(len(self.overlay.annotations), 1)
        self.assertEqual(len(self.overlay.annotations.items[0].points), 1)

    def test_rectangle_from_two_corners(self):
        self.draw("rect", [(120, 130), (300, 260)])
        box = self.overlay.annotations.items[0]
        self.assertIsInstance(box, Box)
        self.assertEqual((box.x1, box.y1, box.x2, box.y2), (120, 130, 300, 260))

    def test_ellipse_and_arrow(self):
        self.draw("ellipse", [(120, 130), (300, 260)])
        self.draw("arrow", [(120, 130), (300, 260)])
        kinds = [a.kind for a in self.overlay.annotations]
        self.assertEqual(kinds, ["ellipse", "arrow"])
        self.assertIsInstance(self.overlay.annotations.items[1], Arrow)

    def test_mosaic_uses_the_mosaic_kind(self):
        self.draw("mosaic", [(150, 150), (250, 150)])
        self.assertEqual(self.overlay.annotations.items[0].kind, "mosaic")

    def test_tiny_shapes_are_dropped(self):
        self.draw("rect", [(150, 150), (151, 151)])
        self.draw("arrow", [(150, 150), (151, 150)])
        self.assertEqual(len(self.overlay.annotations), 0)

    def test_drawing_outside_the_selection_is_ignored(self):
        self.draw("brush", [(20, 20), (40, 40)])
        self.assertEqual(len(self.overlay.annotations), 0)
        self.assertEqual(self.overlay._sel, SEL)      # the selection is intact

    def test_move_tool_still_moves_the_selection(self):
        self.overlay.tool = "move"
        press(self.overlay, 300, 300)
        motion(self.overlay, 320, 310)
        release(self.overlay, 320, 310)
        self.assertEqual(len(self.overlay.annotations), 0)
        self.assertEqual(self.overlay._sel.x, SEL.x + 20)
        self.assertEqual(self.overlay._sel.y, SEL.y + 10)


class UndoAndKeysTests(unittest.TestCase):
    def setUp(self):
        self.overlay = build_overlay()
        self.confirmed = []
        self.overlay._confirm = lambda: self.confirmed.append(True)

    def key(self, name, state=Gdk.ModifierType(0)):
        return self.overlay._on_key(None, Gdk.keyval_from_name(name), 0, state, None)

    def add_stroke(self):
        self.overlay.tool = "brush"
        press(self.overlay, 150, 150)
        motion(self.overlay, 200, 200)
        release(self.overlay, 200, 200)

    def test_ctrl_z_undoes_and_ctrl_shift_z_redoes(self):
        self.add_stroke()
        self.assertEqual(len(self.overlay.annotations), 1)
        self.assertTrue(self.key("z", Gdk.ModifierType.CONTROL_MASK))
        self.assertEqual(len(self.overlay.annotations), 0)
        self.assertTrue(self.key("Z", Gdk.ModifierType.CONTROL_MASK
                                 | Gdk.ModifierType.SHIFT_MASK))
        self.assertEqual(len(self.overlay.annotations), 1)

    def test_number_keys_switch_tools(self):
        for index, tool in enumerate(TOOLS):
            self.key(str(index + 1))
            self.assertEqual(self.overlay.tool, tool)

    def test_enter_confirms(self):
        self.assertTrue(self.key("Return"))
        self.assertEqual(self.confirmed, [True])

    def test_arrow_keys_nudge_the_selection(self):
        self.key("Right")
        self.assertEqual(self.overlay._sel.x, SEL.x + 1)
        self.key("Down", Gdk.ModifierType.SHIFT_MASK)
        self.assertEqual(self.overlay._sel.y, SEL.y + 10)


class TextToolTests(unittest.TestCase):
    def setUp(self):
        self.overlay = build_overlay()
        self.overlay.tool = "text"
        self.overlay.color = PALETTE[1]
        self.overlay.width = 4.0

    def test_commit_creates_a_text_annotation(self):
        self.overlay._entry = _Entry("  中文标注  ")
        self.overlay._entry_anchor = (200.0, 220.0)
        self.overlay._commit_text()
        self.assertEqual(len(self.overlay.annotations), 1)
        text = self.overlay.annotations.items[0]
        self.assertIsInstance(text, Text)
        self.assertEqual(text.text, "中文标注")
        self.assertEqual((text.x, text.y), (200.0, 220.0))
        self.assertEqual(text.color, PALETTE[1])
        self.assertEqual(text.size, 26.0)          # 10 + 4 * 4
        self.assertIsNone(self.overlay._entry)

    def test_empty_text_is_discarded(self):
        self.overlay._entry = _Entry("   ")
        self.overlay._entry_anchor = (200.0, 220.0)
        self.overlay._commit_text()
        self.assertEqual(len(self.overlay.annotations), 0)

    def test_escape_cancels_the_entry(self):
        self.overlay._entry = _Entry("draft")
        self.overlay._entry_anchor = (10.0, 10.0)
        handled = self.overlay._on_key(None, Gdk.keyval_from_name("Escape"), 0,
                                       Gdk.ModifierType(0), None)
        self.assertTrue(handled)
        self.assertIsNone(self.overlay._entry)
        self.assertEqual(len(self.overlay.annotations), 0)

    def test_typing_is_left_to_the_entry(self):
        self.overlay._entry = _Entry("abc")
        self.overlay._entry_anchor = (10.0, 10.0)
        handled = self.overlay._on_key(None, Gdk.keyval_from_name("a"), 0,
                                       Gdk.ModifierType(0), None)
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
