"""Unit tests for the pure selection geometry."""

import unittest

from macshot.selection import (
    HANDLES, Rect, clamp_rect, cursor_for_handle, handle_points, hit_test,
    rect_from_points, resize_rect, size_label,
)


class RectTests(unittest.TestCase):
    def test_derived_values(self):
        rect = Rect(10, 20, 30, 40)
        self.assertEqual((rect.x2, rect.y2), (40, 60))
        self.assertEqual(rect.area, 1200)
        self.assertFalse(rect.is_empty())
        self.assertTrue(rect.contains(10, 20))
        self.assertTrue(rect.contains(40, 60))
        self.assertFalse(rect.contains(40.5, 60))

    def test_round_to_ints_keeps_a_pixel(self):
        rect = Rect(1.4, 2.6, 0.2, 0.1).round_to_ints()
        self.assertEqual(rect.width, 1)
        self.assertEqual(rect.height, 1)

    def test_intersection(self):
        self.assertIsNone(Rect(0, 0, 10, 10).intersection(Rect(20, 20, 5, 5)))
        self.assertEqual(Rect(0, 0, 10, 10).intersection(Rect(5, 5, 10, 10)),
                         Rect(5, 5, 5, 5))


class RectFromPointsTests(unittest.TestCase):
    def test_normalises_direction(self):
        for a, b in (((10, 10), (30, 40)), ((30, 40), (10, 10)),
                     ((30, 10), (10, 40)), ((10, 40), (30, 10))):
            self.assertEqual(rect_from_points(a[0], a[1], b[0], b[1]),
                             Rect(10, 10, 20, 30))


class ClampTests(unittest.TestCase):
    def test_move_inside(self):
        self.assertEqual(clamp_rect(Rect(5, 5, 10, 10), Rect(0, 0, 100, 100)),
                         Rect(5, 5, 10, 10))

    def test_pushed_back_inside(self):
        self.assertEqual(clamp_rect(Rect(-5, 95, 10, 10), Rect(0, 0, 100, 100)),
                         Rect(0, 90, 10, 10))

    def test_shrinks_when_larger_than_bounds(self):
        clamped = clamp_rect(Rect(0, 0, 200, 200), Rect(0, 0, 100, 100))
        self.assertEqual(clamped, Rect(0, 0, 100, 100))


class ResizeTests(unittest.TestCase):
    def test_east_edge(self):
        self.assertEqual(resize_rect(Rect(0, 0, 10, 10), "e", 5, 5),
                         Rect(0, 0, 15, 10))

    def test_north_west_corner(self):
        self.assertEqual(resize_rect(Rect(10, 10, 10, 10), "nw", -5, -5),
                         Rect(5, 5, 15, 15))

    def test_minimum_size_is_respected(self):
        rect = resize_rect(Rect(0, 0, 10, 10), "e", -50, 0, min_size=2)
        self.assertEqual(rect.width, 2)
        self.assertEqual(rect.x, 0)

    def test_minimum_size_west_keeps_right_edge(self):
        rect = resize_rect(Rect(0, 0, 10, 10), "w", 50, 0, min_size=2)
        self.assertEqual(rect.width, 2)
        self.assertEqual(rect.x2, 10)

    def test_bounds_clip_resizing(self):
        rect = resize_rect(Rect(0, 0, 10, 10), "se", 200, 200, bounds=Rect(0, 0, 50, 50))
        self.assertEqual(rect, Rect(0, 0, 50, 50))

    def test_move_handle(self):
        self.assertEqual(resize_rect(Rect(0, 0, 10, 10), "move", 5, 5),
                         Rect(5, 5, 10, 10))


class HitTestTests(unittest.TestCase):
    def setUp(self):
        self.rect = Rect(100, 100, 200, 150)

    def test_corners(self):
        self.assertEqual(hit_test(self.rect, 100, 100, 6), "nw")
        self.assertEqual(hit_test(self.rect, 300, 100, 6), "ne")
        self.assertEqual(hit_test(self.rect, 100, 250, 6), "sw")
        self.assertEqual(hit_test(self.rect, 300, 250, 6), "se")

    def test_edges(self):
        self.assertEqual(hit_test(self.rect, 200, 100, 6), "n")
        self.assertEqual(hit_test(self.rect, 200, 250, 6), "s")
        self.assertEqual(hit_test(self.rect, 100, 175, 6), "w")
        self.assertEqual(hit_test(self.rect, 300, 175, 6), "e")

    def test_inside_and_outside(self):
        self.assertEqual(hit_test(self.rect, 200, 175, 6), "move")
        self.assertIsNone(hit_test(self.rect, 500, 500, 6))
        self.assertIsNone(hit_test(self.rect, 90, 175, 6))

    def test_every_handle_is_placed_on_the_rect(self):
        points = dict((name, (x, y)) for name, x, y in handle_points(self.rect))
        self.assertEqual(set(points), set(HANDLES))
        self.assertEqual(points["nw"], (100, 100))
        self.assertEqual(points["se"], (300, 250))


class CursorTests(unittest.TestCase):
    def test_known_handles(self):
        self.assertEqual(cursor_for_handle(None), "crosshair")
        self.assertEqual(cursor_for_handle("nw"), "nwse-resize")
        self.assertEqual(cursor_for_handle("ne"), "nesw-resize")
        self.assertEqual(cursor_for_handle("n"), "ns-resize")
        self.assertEqual(cursor_for_handle("w"), "ew-resize")
        self.assertEqual(cursor_for_handle("move"), "move")

    def test_label(self):
        self.assertEqual(size_label(Rect(0, 0, 1280, 719.6)), "1280 × 720")


if __name__ == "__main__":
    unittest.main()
