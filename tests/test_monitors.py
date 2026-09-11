"""Unit tests for monitor layout maths (no D-Bus needed)."""

import unittest

from macshot.monitors import Layout, Monitor, fallback_layout
from macshot.selection import Rect


class SingleMonitorTests(unittest.TestCase):
    """A 1920x1200 panel driven at a 1.25 scale factor, like the test machine."""

    def setUp(self):
        self.monitor = Monitor("eDP-1", 0, 0, 1920, 1200, 1.25, True)
        self.layout = Layout([self.monitor])

    def test_logical_geometry(self):
        self.assertEqual(self.monitor.logical_rect, Rect(0, 0, 1536, 960))
        self.assertEqual(self.layout.logical_bounds, Rect(0, 0, 1536, 960))
        self.assertEqual(self.layout.physical_bounds, Rect(0, 0, 1920, 1200))

    def test_logical_selection_maps_to_physical_pixels(self):
        crop = self.layout.physical_crop(Rect(100, 80, 600, 400), (1920, 1200))
        self.assertEqual(crop, Rect(125, 100, 750, 500))

    def test_full_logical_selection_covers_the_buffer(self):
        crop = self.layout.physical_crop(Rect(0, 0, 1536, 960), (1920, 1200))
        self.assertEqual(crop, Rect(0, 0, 1920, 1200))

    def test_crop_is_clipped_to_the_buffer(self):
        crop = self.layout.physical_crop(Rect(1500, 950, 200, 200), (1920, 1200))
        self.assertLessEqual(crop.x2, 1920)
        self.assertLessEqual(crop.y2, 1200)
        self.assertGreaterEqual(crop.width, 1)

    def test_scale_one_monitor(self):
        layout = Layout([Monitor("HDMI-1", 0, 0, 1920, 1080, 1.0)])
        self.assertEqual(layout.physical_crop(Rect(10, 20, 30, 40), (1920, 1080)),
                         Rect(10, 20, 30, 40))


class MultiMonitorTests(unittest.TestCase):
    def setUp(self):
        # 1920x1200@1.25 on the left, 2560x1440@1.0 on the right
        self.left = Monitor("eDP-1", 0, 0, 1920, 1200, 1.25, True)
        self.right = Monitor("DP-1", 1920, 0, 2560, 1440, 1.0)
        self.layout = Layout([self.left, self.right])

    def test_desktop_bounds(self):
        # logical x of the right monitor is 1920/1.0, its width 2560 - mutter
        # lays monitors out in physical pixels and each one scales on its own.
        self.assertEqual(self.layout.logical_bounds, Rect(0, 0, 1920 + 2560, 1440))
        self.assertEqual(self.layout.physical_bounds, Rect(0, 0, 1920 + 2560, 1440))

    def test_second_monitor_origin(self):
        self.assertEqual(self.right.logical_x, 1920)

    def test_selection_on_the_second_monitor(self):
        crop = self.layout.physical_crop(Rect(2000, 100, 200, 100), (4480, 1440))
        self.assertEqual(crop, Rect(2000, 100, 200, 100))

    def test_selection_on_the_scaled_monitor_uses_its_own_scale(self):
        crop = self.layout.physical_crop(Rect(100, 100, 100, 100), (4480, 1440))
        self.assertEqual(crop, Rect(125, 125, 125, 125))

    def test_monitor_lookup(self):
        self.assertIs(self.layout.monitor_for_logical_point(100, 100), self.left)
        self.assertIs(self.layout.monitor_for_logical_point(3000, 100), self.right)
        self.assertIsNone(self.layout.monitor_for_logical_point(-10, -10))

    def test_matching_gdk_geometry(self):
        self.assertIs(self.layout.monitor_matching(Rect(1920, 0, 2560, 1440)), self.right)
        self.assertIsNone(self.layout.monitor_matching(Rect(0, 0, 100, 100)))


class FallbackTests(unittest.TestCase):
    def test_uniform_mapping_when_the_buffer_disagrees_with_mutter(self):
        layout = Layout([Monitor("eDP-1", 0, 0, 1920, 1200, 2.0)])
        # 1.25 is what the buffer really is: fall back to the image ratio.
        crop = layout.physical_crop(Rect(0, 0, 1536, 960), (1920, 1200))
        self.assertEqual(crop, Rect(0, 0, 1920, 1200))

    def test_fallback_layout_is_identity(self):
        layout = fallback_layout((800, 600))
        self.assertEqual(layout.physical_crop(Rect(10, 10, 20, 20), (800, 600)),
                         Rect(10, 10, 20, 20))


if __name__ == "__main__":
    unittest.main()
