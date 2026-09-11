"""Tests for naming, saving and cropping, plus a CLI smoke test."""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from macshot.save import build_filename, crop_png, unique_path  # noqa: E402
from macshot.selection import Rect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_pixbuf(width=40, height=40):
    """A deterministic image: red channel = x, green = y, blue = 255."""
    rowstride = width * 3
    data = bytearray(rowstride * height)
    for y in range(height):
        for x in range(width):
            offset = y * rowstride + x * 3
            data[offset] = x % 256
            data[offset + 1] = y % 256
            data[offset + 2] = 255
    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(data)), GdkPixbuf.Colorspace.RGB, False, 8,
        width, height, rowstride,
    )


class FilenameTests(unittest.TestCase):
    def test_macos_style_default(self):
        when = datetime(2026, 2, 14, 15, 4, 5)
        self.assertEqual(build_filename("Screenshot {date} at {time}.png", when),
                         "Screenshot 2026-02-14 at 15.04.05.png")

    def test_chinese_template(self):
        when = datetime(2026, 2, 14, 15, 4, 5)
        self.assertEqual(build_filename("截屏 {date} {time}.png", when),
                         "截屏 2026-02-14 15.04.05.png")

    def test_png_suffix_is_added(self):
        self.assertTrue(build_filename("shot {date}", datetime(2026, 1, 1)).endswith(".png"))


class UniquePathTests(unittest.TestCase):
    def test_appends_a_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = unique_path(directory, "shot.png")
            self.assertEqual(first.name, "shot.png")
            first.write_text("x")
            self.assertEqual(unique_path(directory, "shot.png").name, "shot-2.png")


class CropTests(unittest.TestCase):
    def test_crop_matches_the_source_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.png"
            make_pixbuf(40, 40).savev(str(source), "png", [], [])
            destination = crop_png(source, Rect(10, 20, 15, 10), tmp / "crop.png")
            cropped = GdkPixbuf.Pixbuf.new_from_file(str(destination))
            self.assertEqual((cropped.get_width(), cropped.get_height()), (15, 10))
            pixels = cropped.get_pixels()
            stride = cropped.get_rowstride()
            channels = cropped.get_n_channels()
            # top-left of the crop must be the source pixel (10, 20)
            self.assertEqual(pixels[0], 10)
            self.assertEqual(pixels[1], 20)
            # bottom-right must be (24, 29)
            offset = 9 * stride + 14 * channels
            self.assertEqual(pixels[offset], 24)
            self.assertEqual(pixels[offset + 1], 29)

    def test_crop_outside_the_image_is_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.png"
            make_pixbuf(20, 20).savev(str(source), "png", [], [])
            cropped = GdkPixbuf.Pixbuf.new_from_file(
                str(crop_png(source, Rect(15, 15, 50, 50), tmp / "crop.png")))
            self.assertEqual((cropped.get_width(), cropped.get_height()), (5, 5))


class CliSmokeTests(unittest.TestCase):
    """End to end through the real command line, minus the live capture."""

    def _run(self, tmp: Path, *extra: str):
        source = tmp / "frozen.png"
        make_pixbuf(400, 300).savev(str(source), "png", [], [])
        env = dict(os.environ, XDG_CACHE_HOME=str(tmp / "cache"))
        result = subprocess.run(
            [sys.executable, str(ROOT / "macshot.py"), "region",
             "--image", str(source), "--save-dir", str(tmp / "out"), "--no-sound",
             "--no-flash", *extra],
            capture_output=True, text=True, env=env, timeout=180,
        )
        files = list((tmp / "out").glob("*.png"))
        return result, files

    def test_full_desktop_selection_reproduces_the_image(self):
        from macshot.monitors import get_layout

        try:
            bounds = get_layout().logical_bounds
        except Exception as exc:  # pragma: no cover - no Mutter around
            self.skipTest(f"no monitor layout available: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            result, files = self._run(
                tmp, "--select",
                f"{bounds.x:g},{bounds.y:g},{bounds.width:g},{bounds.height:g}",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(files), 1, files)
            saved = GdkPixbuf.Pixbuf.new_from_file(str(files[0]))
            self.assertEqual((saved.get_width(), saved.get_height()), (400, 300))

    def test_partial_selection_is_scaled_to_the_buffer(self):
        from macshot.monitors import get_layout

        try:
            bounds = get_layout().logical_bounds
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"no monitor layout available: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            result, files = self._run(tmp, "--select", "10,20,100,50")
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = GdkPixbuf.Pixbuf.new_from_file(str(files[0]))
            expected_w = round(100 * 400 / bounds.width)
            expected_h = round(50 * 300 / bounds.height)
            self.assertAlmostEqual(saved.get_width(), expected_w, delta=2)
            self.assertAlmostEqual(saved.get_height(), expected_h, delta=2)

    def test_bad_selection_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, files = self._run(Path(tmp), "--select", "1,2,3")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
