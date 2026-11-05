"""Persistent JSON configuration."""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import paths

DEFAULTS: dict[str, Any] = {
    "runner": "",              # Runner id; empty = pick automatically
    "prefix": str(paths.DEFAULT_PREFIX),
    "library_dir": "",         # Host folder exposed to Windows as a drive for game installs
    "library_drive": "d",
    "use_runtime": True,       # Run Proton inside the Steam Linux Runtime container
    "language": "",            # Battle.net locale (enUS, deDE, ...); empty = from $LANG
    "region": "",              # us / eu / kr / tw / cn; empty = from product.db, else us

    # Global launch options (per-game overrides live in "games")
    "mangohud": False,
    "gamemode": False,
    "gamescope": False,
    "gamescope_args": "-f",
    "dxvk_hud": False,
    "wayland": False,
    "hdr": False,
    "disable_fsync": False,
    "disable_esync": False,
    "proton_log": False,
    "wrapper": "",             # Extra command prefix, e.g. "obs-gamecapture"
    "env": {},                 # Extra environment variables

    # Battle.net / prefix tweaks
    "bnet_hw_accel": True,
    "dll_tweaks": True,
    "restart_bnet_on_env_change": True,

    # Desktop integration
    "desktop_entries": True,   # Create app-menu entries for installed games automatically
    "tray": True,
    "close_to_tray": False,
    "show_all_games": False,
    "setup_done": False,
    "auto_update_ge": False,
    "check_updates": True,

    "games": {},
}

GAME_DEFAULTS: dict[str, Any] = {
    "launch_mode": "bnet",     # "bnet" = via Battle.net --exec, "direct" = run the game exe
    "launch_code": "",         # Override for the Battle.net launch code
    "args": "",                # Extra args (direct mode) appended to the command line
    "env": {},
    "mangohud": None,          # None = inherit global
    "gamemode": None,
    "gamescope": None,
    "last_played": 0,
    "hidden": False,
}


class Config:
    """Thread-safe, auto-merging wrapper around config.json."""

    def __init__(self, path: Path = paths.CONFIG_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            data = copy.deepcopy(DEFAULTS)
            try:
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    data.update(raw)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                # Keep a copy of the unreadable file instead of silently dropping it.
                try:
                    self.path.rename(self.path.with_suffix(".broken.json"))
                except OSError:
                    pass
            if not isinstance(data.get("games"), dict):
                data["games"] = {}
            if not isinstance(data.get("env"), dict):
                data["env"] = {}
            self.data = data

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
            os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(key, default)

    def update(self, **values: Any) -> None:
        with self._lock:
            self.data.update(values)
            self.save()

    # ---- per-game settings -------------------------------------------------
    def game(self, uid: str) -> dict[str, Any]:
        with self._lock:
            merged = copy.deepcopy(GAME_DEFAULTS)
            merged.update(self.data["games"].get(uid, {}))
            return merged

    def set_game(self, uid: str, **values: Any) -> None:
        with self._lock:
            entry = self.data["games"].setdefault(uid, {})
            entry.update(values)
            # Drop keys equal to defaults to keep the file tidy.
            for key in list(entry):
                if key in GAME_DEFAULTS and entry[key] == GAME_DEFAULTS[key]:
                    del entry[key]
            if not entry:
                del self.data["games"][uid]
            self.save()

    @property
    def prefix(self) -> Path:
        return Path(os.path.expanduser(self["prefix"] or str(paths.DEFAULT_PREFIX)))

    def effective(self, uid: str | None, key: str) -> Any:
        """Per-game value if set (not None), else the global value."""
        if uid:
            value = self.game(uid).get(key)
            if value is not None:
                return value
        return self[key]
