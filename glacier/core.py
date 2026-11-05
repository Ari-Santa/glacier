"""Facade tying config, engine, Battle.net and library together, plus the setup flow."""

from __future__ import annotations

import threading

from . import paths, proton
from .battlenet import BattleNet
from .config import Config
from .engine import Engine, EngineError, ProgressFn
from .library import Library


class Core:
    def __init__(self, cfg: Config | None = None):
        paths.ensure_dirs()
        self.cfg = cfg or Config()
        self.engine = Engine(self.cfg)
        self.bnet = BattleNet(self.engine)
        self.library = Library(self.engine, self.bnet)

    # ------------------------------------------------------------------ runners
    def ensure_runner(self, progress: ProgressFn | None = None,
                      cancel: threading.Event | None = None) -> proton.Runner:
        runner = self.engine.runner()
        if runner is None:
            if progress:
                progress("No Proton found; fetching the latest GE-Proton", -1)
            releases = proton.list_ge_releases(5)
            if not releases:
                raise EngineError("Could not find a GE-Proton release to download")
            runner = proton.install_ge(releases[0], progress, cancel)
            self.engine.runners(refresh=True)
            self.cfg.update(runner=runner.id)
        missing = self.engine.runtime_missing()
        if missing:
            if progress:
                progress(f"Fetching Steam Linux Runtime for {runner.name}", -1)
            proton.install_runtime(missing, progress, cancel)
        return runner

    def install_ge(self, release: proton.Release, select: bool = True,
                   progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> proton.Runner:
        runner = proton.install_ge(release, progress, cancel)
        self.engine.runners(refresh=True)
        if select:
            self.cfg.update(runner=runner.id)
            missing = self.engine.runtime_missing()
            if missing:
                proton.install_runtime(missing, progress, cancel)
        return runner

    def update_ge(self, progress: ProgressFn | None = None,
                  cancel: threading.Event | None = None) -> proton.Runner | None:
        """Install the newest GE-Proton if it is not present yet. Returns it when new."""
        releases = proton.list_ge_releases(3)
        if not releases:
            return None
        newest = releases[0]
        if newest.tag in proton.installed_ge_tags():
            return None
        current = self.engine.runner()
        select = bool(current and current.is_ge)
        return self.install_ge(newest, select=select, progress=progress, cancel=cancel)

    # ------------------------------------------------------------------ setup
    def setup(self, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> None:
        """Everything needed to go from nothing to a running Battle.net, unattended."""
        self.ensure_runner(progress, cancel)
        self.engine.ensure_prefix(progress)
        if not self.bnet.installed():
            self.bnet.install(progress, cancel)
        self.library.scan()
        if progress:
            progress("Creating app menu entries", -1)
        self.library.sync_menu_entries()
        self.cfg.update(setup_done=True)
        if progress:
            progress("Battle.net is ready — log in to start playing", 1.0)
