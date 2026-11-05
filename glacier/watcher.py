"""Session watcher: tags game windows (correct icons/grouping) and tracks playtime.

Exactly one watcher runs at a time (flock). The GUI hosts it while open; launches from
the app menu spawn a detached `glacier watch` that exits once the prefix goes idle.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time

from . import desktop, paths, stats

LOCK_FILE = paths.STATE_DIR / "watcher.lock"


class WatcherLock:
    def __init__(self):
        self._fh = None

    def acquire(self) -> bool:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None


def watcher_running() -> bool:
    lock = WatcherLock()
    if lock.acquire():
        lock.release()
        return False
    return True


class WindowTagger:
    """Gives each game window its own desktop identity."""

    def __init__(self):
        self.x = None
        self.tagged: dict[int, str] = {}
        self.available = True
        try:
            from .x11 import X11
            self.x = X11()
        except Exception:
            self.available = False

    def tick(self, procs, exe_map: dict[str, str]) -> list[tuple[int, str]]:
        if not self.x:
            return []
        by_pid = {p.pid: p.exe for p in procs}
        done = []
        try:
            windows = self.x.client_windows()
        except Exception:
            return []
        live = set(windows)
        for w in list(self.tagged):
            if w not in live:
                del self.tagged[w]
        for w in windows:
            if w in self.tagged:
                continue
            pid = self.x.pid(w)
            exe = by_pid.get(pid) if pid else None
            if not exe:
                continue
            uid = exe_map.get(exe)
            if not uid:
                continue
            name = desktop.icon_name(uid)
            self.x.tag(w, name, wm_class=name)
            self.tagged[w] = uid
            done.append((w, uid))
        return done

    def close(self) -> None:
        if self.x:
            self.x.close()
            self.x = None


class PlaytimeTracker:
    def __init__(self):
        self.pending: dict[str, float] = {}
        self.last = time.monotonic()
        self.last_flush = time.monotonic()

    def tick(self, running: set[str]) -> None:
        now = time.monotonic()
        dt = min(now - self.last, 30.0)
        self.last = now
        for uid in running:
            if uid != "battlenet":
                self.pending[uid] = self.pending.get(uid, 0.0) + dt
        if now - self.last_flush > 60:
            self.flush()

    def flush(self) -> None:
        if self.pending:
            stats.add_playtime(self.pending)
            self.pending = {}
        self.last_flush = time.monotonic()


def spawn_detached() -> None:
    """Start `glacier watch` in the background unless a watcher already runs."""
    if watcher_running():
        return
    cmd = desktop.glacier_command() + ["watch"]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def run_daemon(core, idle_exit: float = 30.0, interval: float = 1.0) -> int:
    """Blocking watcher loop for the CLI; returns when the prefix has been idle a while."""
    lock = WatcherLock()
    if not lock.acquire():
        return 0
    tagger = WindowTagger()
    tracker = PlaytimeTracker()
    lib = core.library
    lib.scan()
    last_scan = time.monotonic()
    idle_since = None
    try:
        while True:
            procs = core.engine.processes()
            if time.monotonic() - last_scan > 30:
                lib.scan()
                last_scan = time.monotonic()
            running = lib.refresh_running(procs)
            tagger.tick(procs, lib.exe_map())
            tracker.tick(running)
            if lib.is_idle(procs):
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > idle_exit:
                    break
            else:
                idle_since = None
            time.sleep(interval if running - {"battlenet"} or tagger.tagged else interval * 2)
    finally:
        tracker.flush()
        tagger.close()
        lock.release()
    return 0
