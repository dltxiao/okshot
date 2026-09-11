"""macshot configuration file handling."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "macshot"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    """User visible settings; every one of them can be overridden on the CLI."""

    #: where new screenshots land ("~", "$HOME" and "~/..." are expanded)
    save_dir: str = "~/Desktop"
    #: strftime-ish template; {date} and {time} are macOS flavoured
    filename_template: str = "Screenshot {date} at {time}.png"
    #: macOS plays the camera shutter; canberra-gtk-play does the same here
    play_sound: bool = True
    #: quick white flash over the captured area, like macOS
    flash: bool = True
    #: show the "drag / space / esc" hint while nothing is selected
    hints: bool = True
    #: how long the helper process keeps serving the clipboard (seconds)
    clipboard_hold_seconds: int = 180
    #: "gnome" uses the desktop's own window picker (recommended); "off" disables
    window_mode: str = "gnome"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        config = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return config
        except (OSError, ValueError):
            return config
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key in known:
                setattr(config, key, value)
        return config

    def save(self, path: Path | None = None) -> Path:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    @property
    def save_path(self) -> Path:
        return Path(os.path.expandvars(self.save_dir)).expanduser()
