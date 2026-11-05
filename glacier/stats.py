"""Play statistics (last played, total playtime), safe to update from several processes."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager

from . import paths

STATS_FILE = paths.STATE_DIR / "stats.json"


@contextmanager
def _locked():
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATS_FILE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(STATS_FILE.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        yield data
        tmp = STATS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, STATS_FILE)


def load() -> dict:
    try:
        data = json.loads(STATS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def touch(uid: str) -> None:
    with _locked() as data:
        data.setdefault(uid, {})["last_played"] = int(time.time())


def add_playtime(seconds: dict[str, float]) -> None:
    if not seconds:
        return
    with _locked() as data:
        for uid, s in seconds.items():
            entry = data.setdefault(uid, {})
            entry["playtime"] = int(entry.get("playtime", 0) + s)
            entry["last_seen"] = int(time.time())


def fmt_playtime(seconds: int) -> str:
    if seconds < 60:
        return "—" if seconds <= 0 else "< 1 min"
    h, m = divmod(seconds // 60, 60)
    return f"{h} h {m} min" if h else f"{m} min"
