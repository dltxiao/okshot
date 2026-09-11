"""Unit tests for monitor layout maths (no D-Bus needed).

Numbers in ``DualMonitorStackedTests`` come from a real Ubuntu 26.04 / GNOME 50
laptop plus an external monitor, where the portal screenshot was measured at
2400x2550 pixels -- stage bounds 1920x2040 times the global 1.25 scale factor.
"""

import unittest

from macshot.monitors import Layout, Monitor, fallback_layout, logical_size
from macshot.selection import Rect


def fake_monitor(name, width, height):
    """One DisplayConfig monitor entry: ((ssss) spec, [modes], a{sv} props)."""
    mode = (f"{width}x{height}@60", width, height, 60.0, 1.0, [1.0],
            {"is-current": True})
    return (name, "vendor", "0x0000", "0x00000000"), [mode], {}


def spec(name):
    return (name, "vendor", "0x0000", "0x00000000")


class SingleMonitorTests(unittest.TestCase):
    """A 1920x1200 panel driven at a 1.25 scale factor."""

    def setUp(self):
        self.monitor = Monitor("eDP-1", 0, 0, 1536, 960, 1.25, True)
        self.layout = Layout([self.monitor])

    def test_logical_geometry(self):
        self.assertEqual(self.monitor.logical_rect, Rect(0, 0, 1536, 960))
        self.assertEqual(self.layout.logical_bounds, Rect(0, 0, 1536, 960))
        self.assertEqual(self.layout.physical_bounds, Rect(0, 0, 1920, 1200))

    def test_stage_model_is_used(self):
        self.assertEqual(self.layout.mapping_kind((1920, 1200)), "stage")
        self.assertEqual(self.layout.stage_scale((1920, 1200)), 1.25)

    def test_logical_selection_maps_to_image_pixels(self):
        crop = self.layout.physical_crop(Rect(100, 80, 600, 400), (1920, 1200))
        self.assertEqual(crop, Rect(125, 100, 750, 500))

    def test_full_logical_selection_covers_the_image(self):
        crop = self.layout.physical_crop(Rect(0, 0, 1536, 960), (1920, 1200))
        self.assertEqual(crop, Rect(0, 0, 1920, 1200))

    def test_crop_is_clipped_to_the_buffer(self):
        crop = self.layout.physical_crop(Rect(1500, 950, 200, 200), (1920, 1200))
        self.assertLessEqual(crop.x2, 1920)
        self.assertLessEqual(crop.y2, 1200)
        self.assertGreaterEqual(crop.width, 1)


class DualMonitorStackedTests(unittest.TestCase):
    """1920x1080@1.0 external on top, 1920x1200@1.25 laptop below (measured)."""

    def setUp(self):
        self.external = Monitor("HDMI-1", 0, 0, 1920, 1080, 1.0)
        self.laptop = Monitor("eDP-1", 0, 1080, 1536, 960, 1.25, True)
        self.layout = Layout([self.external, self.laptop])
        self.image = (2400, 2550)          # what the portal actually produced

    def test_stage_bounds_match_the_measured_image(self):
        self.assertEqual(self.layout.logical_bounds, Rect(0, 0, 1920, 2040))
        self.assertEqual(self.layout.mapping_kind(self.image), "stage")
        self.assertEqual(self.layout.stage_scale(self.image), 1.25)

    def test_image_slices_per_monitor(self):
        self.assertEqual(
            self.layout.physical_crop(self.external.logical_rect, self.image),
            Rect(0, 0, 2400, 1350))
        # the laptop slice starts at 1080 * 1.25; the GNOME top bar sits there
        self.assertEqual(
            self.layout.physical_crop(self.laptop.logical_rect, self.image),
            Rect(0, 1350, 1920, 1200))

    def test_monitor_lookup_uses_stage_coordinates(self):
        self.assertIs(self.layout.monitor_for_logical_point(100, 100), self.external)
        self.assertIs(self.layout.monitor_for_logical_point(100, 1200), self.laptop)
        self.assertIsNone(self.layout.monitor_for_logical_point(100, 3000))

    def test_gdk_geometry_pairs_with_the_right_monitor(self):
        # Gdk.Monitor.get_geometry() reports stage rects, not physical ones
        self.assertIs(self.layout.monitor_matching(Rect(0, 0, 1920, 1080)), self.external)
        self.assertIs(self.layout.monitor_matching(Rect(0, 1080, 1536, 960)), self.laptop)

    def test_connector_name_wins(self):
        self.assertIs(
            self.layout.monitor_matching(Rect(0, 1080, 1536, 960), "eDP-1"), self.laptop)
        # even with a bogus geometry the connector resolves it
        self.assertIs(
            self.layout.monitor_matching(Rect(0, 0, 10, 10), "eDP-1"), self.laptop)

    def test_selection_on_the_laptop_maps_through_the_global_scale(self):
        crop = self.layout.physical_crop(Rect(100, 1200, 200, 100), self.image)
        self.assertEqual(crop, Rect(125, 1500, 250, 125))
        self.assertGreaterEqual(crop.y, 1350)      # inside the laptop slice

    def test_selection_on_the_external_monitor(self):
        crop = self.layout.physical_crop(Rect(100, 100, 200, 100), self.image)
        self.assertEqual(crop, Rect(125, 125, 250, 125))
        self.assertLess(crop.y2, 1350)

    def test_selection_spanning_both_screens(self):
        crop = self.layout.physical_crop(Rect(0, 1000, 1920, 200), self.image)
        self.assertEqual(crop, Rect(0, 1250, 2400, 250))

    def test_physical_model_still_works_if_mutter_renders_per_monitor(self):
        # A per-monitor buffer would be the union of the panels: 1920x2280
        buffer_size = (1920, 2280)
        self.assertEqual(self.layout.mapping_kind(buffer_size), "physical")
        self.assertEqual(
            self.layout.physical_crop(self.laptop.logical_rect, buffer_size),
            Rect(0, 1080, 1920, 1200))
        self.assertEqual(
            self.layout.physical_crop(self.external.logical_rect, buffer_size),
            Rect(0, 0, 1920, 1080))


class SideBySideTests(unittest.TestCase):
    """Laptop 1.25 on the left, a 1:1 2560x1440 monitor to its right."""

    def setUp(self):
        self.left = Monitor("eDP-1", 0, 0, 1536, 960, 1.25, True)
        self.right = Monitor("DP-1", 1920, 0, 2560, 1440, 1.0)
        self.layout = Layout([self.left, self.right])

    def test_bounds(self):
        self.assertEqual(self.layout.logical_bounds, Rect(0, 0, 4480, 1440))
        self.assertEqual(self.layout.physical_bounds, Rect(0, 0, 1920 + 2560, 1440))

    def test_uniform_stage_scale(self):
        image = (4480, 1440)
        self.assertEqual(self.layout.mapping_kind(image), "stage")
        self.assertEqual(self.layout.stage_scale(image), 1.0)

    def test_selections_on_each_monitor(self):
        self.assertEqual(self.layout.physical_crop(Rect(100, 100, 100, 100), (4480, 1440)),
                         Rect(100, 100, 100, 100))
        self.assertEqual(self.layout.physical_crop(Rect(2000, 100, 200, 100), (4480, 1440)),
                         Rect(2000, 100, 200, 100))

    def test_monitor_lookup(self):
        self.assertIs(self.layout.monitor_for_logical_point(100, 100), self.left)
        self.assertIs(self.layout.monitor_for_logical_point(3000, 100), self.right)

    def test_overlap_is_used_when_geometry_is_off(self):
        # a 40px disagreement with every monitor: the largest overlap decides
        self.assertIs(self.layout.monitor_matching(Rect(0, 20, 1500, 940)), self.left)


class NegativeOriginTests(unittest.TestCase):
    """A monitor placed to the left of the primary one has negative coordinates."""

    def setUp(self):
        self.laptop = Monitor("eDP-1", 0, 0, 1536, 960, 1.25, True)
        self.left = Monitor("DP-1", -1920, 0, 1920, 1080, 1.0)
        self.layout = Layout([self.laptop, self.left])

    def test_bounds_start_negative(self):
        self.assertEqual(self.layout.logical_bounds, Rect(-1920, 0, 3456, 1080))

    def test_selection_on_the_left_monitor(self):
        image = (4320, 1350)                 # 3456 x 1080 stage at 1.25
        self.assertEqual(self.layout.mapping_kind(image), "stage")
        crop = self.layout.physical_crop(Rect(-1800, 100, 100, 100), image)
        self.assertEqual(crop, Rect(150, 125, 125, 125))

    def test_primary_monitor_starts_at_the_right_offset(self):
        image = (4320, 1350)
        crop = self.layout.physical_crop(Rect(0, 0, 200, 100), image)
        self.assertEqual(crop, Rect(2400, 0, 250, 125))


class LogicalSizeTests(unittest.TestCase):
    """How a logical monitor's stage size is derived from Mutter's reply."""

    def setUp(self):
        self.by_connector = {
            entry[0][0]: entry
            for entry in (fake_monitor("eDP-1", 1920, 1200),
                          fake_monitor("HDMI-1", 1920, 1080),
                          fake_monitor("DP-1", 2560, 1440))
        }

    def test_scaled_panel(self):
        self.assertEqual(logical_size([spec("eDP-1")], self.by_connector, 1.25, 0),
                         (1536, 960))

    def test_unscaled_panel(self):
        self.assertEqual(logical_size([spec("HDMI-1")], self.by_connector, 1.0, 0),
                         (1920, 1080))

    def test_rotated_panel_swaps_width_and_height(self):
        # transform 1 = 90 degrees clockwise
        self.assertEqual(logical_size([spec("eDP-1")], self.by_connector, 1.25, 1),
                         (960, 1536))
        self.assertEqual(logical_size([spec("HDMI-1")], self.by_connector, 1.0, 3),
                         (1080, 1920))

    def test_upside_down_is_not_a_rotation(self):
        self.assertEqual(logical_size([spec("HDMI-1")], self.by_connector, 1.0, 2),
                         (1920, 1080))

    def test_mirrored_pair_is_not_summed(self):
        # one logical monitor holding two panels shows the same content once
        self.assertEqual(
            logical_size([spec("HDMI-1"), spec("DP-1")], self.by_connector, 1.0, 0),
            (2560, 1440))

    def test_unknown_panel_is_ignored(self):
        self.assertEqual(logical_size([spec("DP-9")], self.by_connector, 1.0, 0), None)


class FallbackTests(unittest.TestCase):
    def test_uniform_mapping_when_no_model_fits(self):
        layout = Layout([Monitor("eDP-1", 0, 0, 1536, 960, 2.0)])
        self.assertEqual(layout.mapping_kind((1000, 800)), "uniform")
        self.assertEqual(layout.physical_crop(Rect(0, 0, 1536, 960), (1000, 800)),
                         Rect(0, 0, 1000, 800))

    def test_fallback_layout_is_identity(self):
        layout = fallback_layout((800, 600))
        self.assertEqual(layout.physical_crop(Rect(10, 10, 20, 20), (800, 600)),
                         Rect(10, 10, 20, 20))


if __name__ == "__main__":
    unittest.main()
