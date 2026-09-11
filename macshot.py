#!/usr/bin/env python3
"""Launcher for macshot (works from a checkout or through a symlink)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(os.path.abspath(__file__))))

from macshot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
