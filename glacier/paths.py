"""XDG locations used by Glacier."""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_ID

HOME = Path.home()


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    return Path(value) if value and os.path.isabs(value) else HOME / fallback


XDG_CONFIG = _xdg("XDG_CONFIG_HOME", ".config")
XDG_DATA = _xdg("XDG_DATA_HOME", ".local/share")
XDG_CACHE = _xdg("XDG_CACHE_HOME", ".cache")
XDG_STATE = _xdg("XDG_STATE_HOME", ".local/state")

CONFIG_DIR = XDG_CONFIG / APP_ID
CONFIG_FILE = CONFIG_DIR / "config.json"

DATA_DIR = XDG_DATA / APP_ID
RUNNERS_DIR = DATA_DIR / "runners"          # GE-Proton and other downloaded Proton builds
RUNTIMES_DIR = DATA_DIR / "runtimes"        # Steam Linux Runtime containers (fallback when Steam is absent)
ICONS_DIR = DATA_DIR / "icons"              # 256px PNGs extracted from game executables
APPS_DIR = DATA_DIR / "apps"                # native Linux helpers Glacier installs (CurseForge AppImage)
STEAM_STUB_DIR = DATA_DIR / "steam-stub"    # empty STEAM_COMPAT_CLIENT_INSTALL_PATH
DEFAULT_PREFIX = DATA_DIR / "prefix"        # STEAM_COMPAT_DATA_PATH (wine prefix lives in ./pfx)

CACHE_DIR = XDG_CACHE / APP_ID
DOWNLOADS_DIR = CACHE_DIR / "downloads"

STATE_DIR = XDG_STATE / APP_ID
LOG_DIR = STATE_DIR / "logs"
SESSION_FILE = STATE_DIR / "session.json"

APPLICATIONS_DIR = XDG_DATA / "applications"
HICOLOR_DIR = XDG_DATA / "icons" / "hicolor"
LOCAL_BIN = HOME / ".local" / "bin"

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, DATA_DIR, RUNNERS_DIR, RUNTIMES_DIR, ICONS_DIR, STEAM_STUB_DIR,
              CACHE_DIR, DOWNLOADS_DIR, STATE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
