"""Glacier main window."""

from __future__ import annotations

import os
import signal
import sys
import time

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow,
                             QMenu, QMessageBox, QProgressBar, QPushButton, QStackedWidget,
                             QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import APP_ID, APP_NAME, __version__, desktop, paths, proton, selfupdate
from ..core import Core
from ..curseforge import CurseForge, sync_games
from ..engine import EngineError
from ..library import BNET_UID
from ..watcher import PlaytimeTracker, WatcherLock, WindowTagger, spawn_detached
from . import theme
from .pages import AddonsPage, BattleNetPage, LibraryPage, LogsPage, ProtonPage, SettingsPage
from .tasks import TaskManager
from .widgets import app_icon, button, icon, label, pixmap_from_file

SERVER_NAME = f"{APP_ID}-gui-{os.getuid()}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.core = Core()
        self.curseforge = CurseForge()
        self.glacier_update: selfupdate.Release | None = selfupdate.available()
        self._announced_update = ""
        self.tasks = TaskManager()
        self.tasks.changed.connect(self._tasks_changed)
        self.tasks.error.connect(self._task_error)
        self.lock = WatcherLock()
        self.tagger: WindowTagger | None = None
        self.tracker = PlaytimeTracker()
        self._db_mtime = 0.0
        self._running: set[str] = set()
        self._last_error = ""
        self._clear_status_when: str | None = None   # uid whose start ends the current status text
        self._quitting = False
        self.bnet_status = self.core.bnet.status()
        self.vulkan32 = proton.vulkan32_available(env=self.core.engine.vulkan_env())

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)
        self.setMinimumSize(980, 640)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        self.stack = QStackedWidget()
        self.library_page = LibraryPage(self)
        self.bnet_page = BattleNetPage(self)
        self.addons_page = AddonsPage(self)
        self.proton_page = ProtonPage(self)
        self.settings_page = SettingsPage(self)
        self.logs_page = LogsPage(self)
        for p in (self.library_page, self.bnet_page, self.addons_page, self.proton_page, self.settings_page,
                  self.logs_page):     # same order as the sidebar buttons
            self.stack.addWidget(p)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        root.addWidget(self._build_statusbar())
        self.setCentralWidget(central)

        self.tray: QSystemTrayIcon | None = None
        self.update_tray()

        self.poll_timer = QTimer(self, interval=1500, timeout=self.poll)
        self.poll_timer.start()
        self.update_timer = QTimer(self, interval=30 * 60 * 1000, timeout=self._periodic_checks)
        self.update_timer.start()
        QTimer.singleShot(0, self.startup)

    # ------------------------------------------------------------------ layout
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        pm = pixmap_from_file(paths.ASSETS_DIR / "glacier-64.png", 40)
        if pm:
            logo.setPixmap(pm)
        brand.addWidget(logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label(APP_NAME, "Brand"))
        col.addWidget(label("Battle.net on Proton", "BrandSub"))
        brand.addLayout(col, 1)
        lay.addLayout(brand)
        lay.addSpacing(22)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for i, (text, ic) in enumerate((("Library", "applications-games"), ("Battle.net", "internet-services"),
                                        ("Addons", "preferences-plugin"), ("Proton", "wine"),
                                        ("Settings", "configure"), ("Logs", "text-x-log"))):
            b = QPushButton(f"  {text}")
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setIcon(icon(ic, "applications-other"))
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav.addButton(b, i)
            lay.addWidget(b)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self.show_page)
        lay.addStretch(1)

        self.bnet_pill = QLabel("Battle.net")
        self.bnet_pill.setObjectName("Pill")
        lay.addWidget(self.bnet_pill, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addSpacing(6)
        self.bnet_btn = button("Open Battle.net", "primary", "media-playback-start")
        self.bnet_btn.clicked.connect(lambda: self.start_battlenet())
        lay.addWidget(self.bnet_btn)
        lay.addSpacing(8)
        lay.addWidget(label(f"v{__version__}", "BrandSub"))
        return side

    def _build_statusbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(38)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 4, 16, 4)
        self.status_msg = QLabel("")
        self.status_prog = QProgressBar()
        self.status_prog.setFixedWidth(220)
        self.status_prog.setRange(0, 1000)
        self.status_prog.setVisible(False)
        self.status_cancel = button("Cancel", "flat")
        self.status_cancel.setVisible(False)
        self.status_cancel.clicked.connect(self._cancel_task)
        self.status_runner = QLabel("")
        lay.addWidget(self.status_msg, 1)
        lay.addWidget(self.status_prog)
        lay.addWidget(self.status_cancel)
        lay.addSpacing(12)
        lay.addWidget(self.status_runner)
        self._refresh_runner_label()
        return bar

    def show_page(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.nav.button(idx).setChecked(True)
        if self.stack.currentWidget() is self.proton_page:
            self.proton_page.refresh()
            if not self.proton_page._releases:
                self.proton_page.load_releases()
        elif self.stack.currentWidget() is self.bnet_page:
            self.bnet_page.refresh(self.bnet_status)
        elif self.stack.currentWidget() is self.addons_page:
            self.addons_page.refresh()

    # ------------------------------------------------------------------ startup / polling
    def startup(self):
        if self.lock.acquire():
            self.tagger = WindowTagger()
        if not desktop.entry_exists("glacier.desktop"):
            try:
                desktop.install_app_entry()
                desktop.refresh()
            except OSError:
                pass
        self.rescan(check_updates=bool(self.core.cfg["check_updates"]))
        if not self.core.bnet.installed():
            if not self.core.cfg["setup_done"]:
                self.run_setup()        # first run: do everything unattended
        elif self.core.engine.runner() is None or self.core.engine.runtime_missing():
            self.ensure_runtime()
        if self.core.cfg["auto_update_ge"]:
            self.tasks.start("Checking GE-Proton", lambda p, c: self.core.update_ge(p, c),
                             lambda r: r and self.notify(f"Installed {r.name}"), key="ge-update", quiet=True)
        if self.core.cfg["check_glacier_updates"]:
            self.check_glacier_update()
        self.update_banner()

    def _periodic_checks(self):
        self.rescan(check_updates=True)
        if self.core.cfg["check_glacier_updates"]:
            self.check_glacier_update()     # asks GitHub at most every 6 hours

    # ------------------------------------------------------------------ Glacier's own updates
    def check_glacier_update(self, force: bool = False):
        """Look for a newer Glacier release; `force` = the user asked (say the result either way)."""
        def done(rel):
            self.glacier_update = rel
            self.update_banner()
            self.settings_page.refresh_updates()
            if rel and rel.version != self._announced_update:
                self._announced_update = rel.version
                self.notify(f"Glacier {rel.version} is available.")
            elif force and not rel:
                self.set_status(f"Glacier {__version__} is up to date.")

        def failed(msg):
            self.settings_page.refresh_updates(error=msg)
            if force:
                QMessageBox.warning(self, "Glacier updates", msg)

        self.tasks.start("Checking for Glacier updates", lambda p, c: selfupdate.check(force=force), done,
                         on_error=failed, key="glacier-update-check", quiet=True)
        self.settings_page.refresh_updates()

    def update_glacier(self):
        rel = self.glacier_update
        if rel is None:
            return
        ok, why = selfupdate.can_self_update()
        if not ok:
            QMessageBox.information(self, "Update Glacier", f"Glacier {rel.version} is available.\n\n{why}")
            return
        if QMessageBox.question(self, "Update Glacier",
                                f"Update Glacier {__version__} to {rel.version}?\n\n{rel.summary}\n\n"
                                "Battle.net and running games are not affected. Glacier restarts afterwards."
                                ) != QMessageBox.StandardButton.Yes:
            return

        def done(version):
            self.glacier_update = None
            self.update_banner()
            self.settings_page.refresh_updates()
            if QMessageBox.question(self, "Glacier updated",
                                    f"Glacier {version} is installed. Restart Glacier now?"
                                    ) == QMessageBox.StandardButton.Yes:
                self.restart()

        self.tasks.start("Updating Glacier", lambda p, c: selfupdate.install(rel, p, c), done,
                         on_error=lambda _m: self.settings_page.refresh_updates(), key="glacier-update")
        self.settings_page.refresh_updates()

    def restart(self):
        """Start a fresh Glacier once this one has exited, then quit."""
        import subprocess
        cmd = desktop.glacier_command()
        subprocess.Popen(["sh", "-c", 'sleep 2; exec "$@"', "sh", *cmd], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.quit()

    def poll(self):
        core = self.core
        try:
            procs = core.engine.processes()
        except OSError:
            return
        lib = core.library
        running = lib.refresh_running(procs)
        if self.tagger is None and not self.lock.held and self.lock.acquire():
            self.tagger = WindowTagger()
        if self.tagger:
            self.tagger.tick(lib.owners(procs))
            self.tracker.tick(running)
        bnet_running = BNET_UID in running
        if self._clear_status_when in running:   # "Battle.net is starting…" / "Launching X…" is over
            self.set_status("")
        if running != self._running:
            self._running = running
            self.library_page.refresh_cards(lib.states)
        if bnet_running != self.bnet_status.running:
            self.bnet_status.running = bnet_running
            self._refresh_bnet_widgets()
        if self.stack.currentWidget() is self.addons_page:
            self.addons_page.tick()
        try:
            mtime = core.bnet.product_db.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != self._db_mtime:
            self._db_mtime = mtime
            self.rescan()

    def rescan(self, check_updates: bool = False):
        core = self.core

        def work(progress, cancel):
            core.engine.runners(refresh=True)
            core.library.scan()
            if check_updates:
                try:
                    core.library.check_updates()
                except Exception:
                    pass
            status = core.bnet.status(check_latest=check_updates)
            for st in core.library.states.values():
                if st.installed:
                    try:
                        core.library.ensure_icon(st)
                    except Exception:
                        pass
            if core.bnet.installed():
                core.library.ensure_bnet_icon()
            if self.curseforge.installed():
                try:    # keep CurseForge's WoW folders in step with Battle.net (only while it is closed)
                    sync_games(core.bnet, core.engine)
                except Exception:
                    pass
            return status

        def done(status):
            if not check_updates and self.bnet_status.latest:
                status.latest = self.bnet_status.latest
            self.bnet_status = status
            self.library_page.update_states(core.library.states)
            self._refresh_bnet_widgets()
            self.update_banner()
            self.update_tray()
            self.sync_menus(quiet=True)
            updates = [s.game.name for s in core.library.states.values() if s.update_available]
            if check_updates and updates:
                self.set_status(f"Updates available: {', '.join(updates)} — Battle.net installs them when it runs.")

        self.tasks.start("Scanning library", work, done, key="scan" + ("-u" if check_updates else ""), quiet=True)

    # ------------------------------------------------------------------ status widgets
    def _refresh_bnet_widgets(self):
        st = self.bnet_status
        if not st.installed:
            text, color = "● Battle.net not installed", theme.MUTED
        elif st.running:
            text, color = "● Battle.net running", theme.OK
        else:
            text, color = "● Battle.net stopped", theme.MUTED
        self.bnet_pill.setText(text)
        self.bnet_pill.setStyleSheet(f"color: {color};")
        self.bnet_btn.setText("Show Battle.net" if st.running else "Open Battle.net" if st.installed
                              else "Set up Battle.net")
        if self.stack.currentWidget() is self.bnet_page:
            self.bnet_page.refresh(st)

    def _refresh_runner_label(self):
        r = self.core.engine.runner()
        rt = self.core.engine.runtime(r) if r else None
        self.status_runner.setText(f"{r.name if r else 'No Proton'}" + (f"  ·  {rt.name}" if rt else ""))

    def set_status(self, text: str, error: bool = False, clear_when: str | None = None):
        """Show text in the status bar; with clear_when, poll() clears it once that uid is running."""
        self._clear_status_when = clear_when
        self.status_msg.setText(text)
        self.status_msg.setStyleSheet(f"color: {theme.ERR if error else theme.MUTED};")

    def _tasks_changed(self):
        t = self.tasks.visible()
        if t:
            self.set_status(t.message)
            self.status_prog.setVisible(True)
            if t.fraction < 0:
                self.status_prog.setRange(0, 0)
            else:
                self.status_prog.setRange(0, 1000)
                self.status_prog.setValue(int(t.fraction * 1000))
            self.status_cancel.setVisible(True)
        else:
            self.status_prog.setVisible(False)
            self.status_cancel.setVisible(False)
        self.update_banner()

    def _cancel_task(self):
        t = self.tasks.visible()
        if t:
            t.cancel.set()
            self.set_status(f"Cancelling {t.name}…")

    def _task_error(self, name: str, msg: str, tb: str):
        self._last_error = msg
        self.set_status(f"{name} failed: {msg}", error=True)
        self.notify(f"{name} failed: {msg}", error=True)
        if tb:
            try:
                (paths.LOG_DIR / "glacier-errors.log").open("a").write(f"\n# {time.ctime()} {name}\n{tb}")
            except OSError:
                pass
        self.update_banner()

    def notify(self, text: str, error: bool = False):
        if self.tray and self.tray.isVisible() and not self.isActiveWindow():
            self.tray.showMessage(APP_NAME, text, QSystemTrayIcon.MessageIcon.Warning if error
                                  else QSystemTrayIcon.MessageIcon.Information, 5000)
        elif not error:
            self.set_status(text)

    def update_banner(self):
        core = self.core
        page = self.library_page
        setup = self.tasks.active.get("setup")
        bn_pm = pixmap_from_file(desktop.icon_png(desktop.icon_name(BNET_UID)), 40) or \
            pixmap_from_file(paths.ASSETS_DIR / "glacier-64.png", 40)
        if setup:
            page.set_banner("Setting up Battle.net", setup.message, "", None, setup.fraction, bn_pm)
        elif core.engine.runner() is None:
            page.set_banner("No Proton found", "Glacier needs Proton to run Battle.net. It can download "
                            "GE-Proton for you.", "Download GE-Proton", self.ensure_runtime, None, bn_pm)
        elif core.engine.runtime_missing() and not self.tasks.running("runtime"):
            page.set_banner("Steam Linux Runtime missing", "The selected Proton runs inside Valve's runtime "
                            "container, which is not installed.", "Download runtime", self.ensure_runtime,
                            None, bn_pm)
        elif self.tasks.running("runtime"):
            t = self.tasks.active["runtime"]
            page.set_banner("Preparing Proton", t.message, "", None, t.fraction, bn_pm)
        elif self.vulkan32 is False:
            page.set_banner("32-bit Vulkan driver missing", proton.VULKAN32_HINT, "Check again",
                            self.recheck_vulkan32, None, bn_pm)
        elif not self.bnet_status.installed:
            text = "Glacier downloads the official installer and installs it unattended."
            if self._last_error:
                text = f"Last attempt failed: {self._last_error}"
            page.set_banner("Battle.net is not installed", text, "Install Battle.net", self.run_setup, None, bn_pm)
        elif not any(s.installed for s in core.library.states.values()):
            page.set_banner("Log in to Battle.net to get your games",
                            "Installed games appear here automatically, with their own icons and menu entries.",
                            "Open Battle.net", self.start_battlenet, None, bn_pm)
        elif self.tasks.running("glacier-update"):
            t = self.tasks.active["glacier-update"]
            page.set_banner("Updating Glacier", t.message, "", None, t.fraction,
                            pixmap_from_file(paths.ASSETS_DIR / "glacier-64.png", 40))
        elif self.glacier_update:
            rel = self.glacier_update
            page.set_banner(f"Glacier {rel.version} is available",
                            rel.summary or f"You have {__version__}. Released {rel.published}.",
                            "Update Glacier", self.update_glacier, None,
                            pixmap_from_file(paths.ASSETS_DIR / "glacier-64.png", 40))
        else:
            page.set_banner(None)

    # ------------------------------------------------------------------ actions
    def run_setup(self, force: bool = False):
        core = self.core

        def work(progress, cancel):
            if force and core.bnet.installed():
                core.ensure_runner(progress, cancel)
                core.engine.ensure_prefix(progress)
                if core.bnet.running():
                    core.engine.kill_all()
                core.bnet.install(progress, cancel)
                core.library.scan()
                core.library.sync_menu_entries()
            else:
                core.setup(progress, cancel)

        def done(_):
            self._last_error = ""
            self.notify("Battle.net is ready — log in to start playing.")
            self.rescan()
            self._refresh_runner_label()

        self.tasks.start("Battle.net setup", work, done, key="setup")
        self.show_page(0)
        self.update_banner()

    def ensure_runtime(self):
        def done(_):
            self._refresh_runner_label()
            self.proton_page.refresh()
            self.update_banner()
        self.tasks.start("Preparing Proton", lambda p, c: self.core.ensure_runner(p, c), done, key="runtime")

    def recheck_vulkan32(self):
        self.vulkan32 = proton.vulkan32_available(env=self.core.engine.vulkan_env())
        checked = time.strftime("%H:%M:%S")
        if self.vulkan32 is True:
            self.set_status("32-bit Vulkan driver found")
        elif self.vulkan32 is False:
            self.set_status(f"Still no 32-bit Vulkan driver found (checked {checked})", error=True)
        else:
            self.set_status("Couldn't tell whether a 32-bit Vulkan driver is installed")
        self.update_banner()

    def start_battlenet(self, background: bool = False):
        if not self.core.bnet.installed():
            self.run_setup()
            return
        args = ["--runonstartup"] if background else []

        def work(p, c):
            self.core.bnet.launch(args)
            return self.core.bnet.running()   # True: handed to the open client, which just shows itself

        def done(handed_over):
            if handed_over:
                self.set_status("")
            else:
                self.set_status("Battle.net is starting…", clear_when=BNET_UID)

        self.tasks.start("Starting Battle.net", work, done, key="bnet-start", quiet=True)

    def stop_all(self):
        self.tasks.start("Stopping Battle.net and games", lambda p, c: self.core.engine.kill_all(),
                         lambda _: self.set_status("Stopped."), key="kill")

    def game_action(self, uid: str, action: str):
        lib = self.core.library
        st = lib.states.get(uid)
        if st is None:
            return
        name = st.game.name
        if action in ("play", "install", "open", "repair") and not self.core.bnet.installed():
            if action == "play" and self.core.cfg.game(uid)["launch_mode"] == "direct":
                pass
            else:
                self.run_setup()
                return
        if action == "play" and st.needs_locate and self.core.cfg.game(uid)["launch_mode"] != "direct":
            # Copied-in game Battle.net hasn't adopted: show its page and say how to add it.
            self.tasks.start("Battle.net", lambda p, c: lib.open_in_bnet(uid), key=f"open-{uid}", quiet=True)
            QMessageBox.information(self, f"Add {name} to Battle.net", lib.locate_hint(st))
            return
        if action == "play":
            def work(p, c):
                p(f"Launching {name}…", -1)
                return lib.launch(uid)
            self.tasks.start(f"Launching {name}", work,
                             lambda _: self.set_status(f"Launching {name}…", clear_when=uid),
                             key=f"launch-{uid}", quiet=True)
            self._flash_launch(uid)
        elif action == "stop":
            procs = self.core.engine.processes()
            owners = lib.owners(procs)
            for p in procs:
                if owners.get(p.pid) == uid:
                    try:
                        os.kill(p.pid, signal.SIGTERM)
                    except OSError:
                        pass
            self.set_status(f"Asked {name} to close.")
        elif action == "install":
            self.tasks.start(f"Install {name}", lambda p, c: lib.install(uid),
                             lambda _: self.set_status(f"Battle.net opens its installer for {name}."),
                             key=f"install-{uid}", quiet=True)
        elif action == "open":
            self.tasks.start("Battle.net", lambda p, c: lib.open_in_bnet(uid), key=f"open-{uid}", quiet=True)
        elif action == "repair":
            self.tasks.start(f"Repair {name}", lambda p, c: lib.repair(uid),
                             lambda _: self.set_status(f"Battle.net is repairing {name}."), key=f"repair-{uid}")
        elif action == "uninstall":
            if QMessageBox.question(self, "Uninstall", f"Uninstall {name}? This deletes its game files.") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.tasks.start(f"Uninstall {name}", lambda p, c: lib.uninstall(uid),
                             lambda _: self.set_status(f"Uninstalling {name}…"), key=f"uninstall-{uid}")
        elif action == "icon":
            def work(p, c):
                path = lib.ensure_icon(st, force=True)
                desktop.refresh()
                return path
            self.tasks.start("Extracting icon", work,
                             lambda _: self.library_page.update_states(lib.states), key=f"icon-{uid}", quiet=True)

    def _flash_launch(self, uid: str):
        card = self.library_page.cards.get(uid)
        if card:
            card.btn.setText("Starting…")
            card.btn.setEnabled(False)
            QTimer.singleShot(6000, lambda: (card.btn.setEnabled(True), card.set_state(card.st)))

    def run_tool(self, tool: str):
        def work(p, c):
            self.core.engine.ensure_prefix(p)
            spec = self.core.engine.build(tool, [], log_name=tool, wrap=False)
            return self.core.engine.spawn(spec)
        self.tasks.start(f"Opening {tool}", work, key=f"tool-{tool}", quiet=True)

    def run_exe(self, path: str):
        def work(p, c):
            spec = self.core.engine.build(path, [], log_name="run")
            return self.core.engine.spawn(spec)
        self.tasks.start("Running program", work, key="run-exe", quiet=True)

    def _watch_curseforge(self, proc, log, text: str):
        """Report a CurseForge that fails to start; the AppImage runtime stays up while it runs.

        A start that only hands over to an open CurseForge exits 0 at once. A failing runtime
        (no FUSE, a noexec or full $TMPDIR) exits non-zero, after extracting when it extracts,
        hence the long watch.
        """
        started = time.monotonic()
        timer = QTimer(self, interval=1000)

        def check():
            code = proc.poll()
            if code is None and time.monotonic() - started < 45:
                return
            timer.stop()
            timer.deleteLater()
            if code:
                self.set_status(f"CurseForge didn't start (exit code {code}). See {log.name} on the Logs page.",
                                error=True)
            elif self.status_msg.text() == text:
                self.set_status("")

        timer.timeout.connect(check)
        timer.start()

    def curseforge_action(self, action: str):
        """install / update / open / remove the CurseForge AppImage."""
        cf = self.curseforge
        page = self.addons_page
        if action == "open":
            try:
                synced = sync_games(self.core.bnet, self.core.engine)
                proc, log = cf.launch()
            except (EngineError, OSError) as e:
                QMessageBox.warning(self, "CurseForge", str(e))
            else:
                extra = f" ({len(synced.added)} WoW folder(s) added, {len(synced.removed)} leftover(s) removed)" \
                    if synced.changed else ""
                text = "Starting CurseForge…" + extra
                self.set_status(text)
                self._watch_curseforge(proc, log, text)
            return
        if action == "remove":
            if QMessageBox.question(self, "Remove CurseForge",
                                    "Remove the CurseForge app? Your addons and CurseForge's own "
                                    "settings are kept.") != QMessageBox.StandardButton.Yes:
                return
            cf.remove()
            self.notify("Removed CurseForge.")
            page.refresh()
            self.update_tray()
            return

        def work(progress, cancel):
            result = cf.install(progress, cancel)
            return result, sync_games(self.core.bnet, self.core.engine)

        def done(out):
            result, synced = out
            self.notify(" ".join([result.message] + synced.lines()[:3]))
            page.refresh()
            self.update_tray()

        self.tasks.start("CurseForge", work, done, key="curseforge",
                         on_error=lambda _msg: page.refresh())
        page.refresh()

    def install_ge(self, release: proton.Release):
        def done(r):
            self.notify(f"Installed {r.name}")
            self.on_runner_changed()
        self.tasks.start(f"Installing {release.tag}", lambda p, c: self.core.install_ge(release, True, p, c),
                         done, key="ge-install")

    def remove_runner(self, r: proton.Runner):
        if not r.removable:
            QMessageBox.information(self, "Proton", "Only Proton builds downloaded by Glacier can be removed here.")
            return
        if QMessageBox.question(self, "Remove Proton", f"Delete {r.name}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            proton.remove_runner(r)
        except (OSError, RuntimeError) as e:
            self.set_status(str(e), error=True)
        self.on_runner_changed()

    def on_runner_changed(self):
        self.core.engine.runners(refresh=True)
        self._refresh_runner_label()
        self.proton_page.refresh()
        if self.core.engine.runtime_missing():
            self.ensure_runtime()
        self.update_banner()

    def on_prefix_changed(self):
        self._db_mtime = -1
        self.bnet_status = self.core.bnet.status()
        self.rescan()

    def remap_library(self):
        try:
            if self.core.engine.prefix_exists():
                self.core.engine.map_library_drive()
                self.set_status("Game library drive updated.")
        except (OSError, EngineError) as e:
            self.set_status(str(e), error=True)

    def sync_menus(self, force_icons: bool = False, quiet: bool = False):
        lib = self.core.library

        def work(p, c):
            if force_icons:
                for st in lib.states.values():
                    if st.installed:
                        lib.ensure_icon(st, force=True)
            if not self.core.cfg["desktop_entries"]:
                for st in lib.states.values():
                    desktop.remove_entry(desktop.desktop_id(st.uid))
            changed = lib.sync_menu_entries()
            if force_icons:
                desktop.refresh()
            return changed

        def done(changed):
            if changed and not quiet:
                self.set_status("App menu updated.")
            elif not quiet:
                self.set_status("App menu entries are up to date.")
        self.tasks.start("Updating app menu", work, done, key="menus", quiet=quiet)

    def remove_menus(self):
        self.core.library.remove_menu_entries()
        self.set_status("Removed Glacier's game entries from the app menu.")

    # ------------------------------------------------------------------ tray
    def update_tray(self):
        want = bool(self.core.cfg["tray"]) and QSystemTrayIcon.isSystemTrayAvailable()
        if not want:
            if self.tray:
                self.tray.hide()
                self.tray = None
            return
        if self.tray is None:
            self.tray = QSystemTrayIcon(app_icon(), self)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        menu = QMenu()
        for st in sorted(self.core.library.states.values(), key=lambda s: s.game.name):
            if st.installed:
                pm = pixmap_from_file(desktop.icon_png(desktop.icon_name(st.uid)), 32)
                a = QAction(st.game.name, menu)
                if pm:
                    from PyQt6.QtGui import QIcon
                    a.setIcon(QIcon(pm))
                a.triggered.connect(lambda _=False, u=st.uid: self.game_action(u, "play"))
                menu.addAction(a)
        if menu.actions():
            menu.addSeparator()
        menu.addAction("Open Battle.net", self.start_battlenet)
        if self.curseforge.installed():
            menu.addAction("Open CurseForge", lambda: self.curseforge_action("open"))
        menu.addAction(f"Show {APP_NAME}", self.bring_to_front)
        menu.addSeparator()
        menu.addAction("Stop Battle.net and games", self.stop_all)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self.bring_to_front()

    def bring_to_front(self):
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ shutdown
    def closeEvent(self, e):
        if not self._quitting and self.tray and self.core.cfg["close_to_tray"]:
            self.hide()
            e.ignore()
            return
        self._shutdown()
        e.accept()
        QApplication.instance().quit()

    def quit(self):
        self._quitting = True
        self.close()

    def _shutdown(self):
        self.poll_timer.stop()
        for t in list(self.tasks.active.values()):
            t.cancel.set()
        self.tracker.flush()
        if self.tagger:
            self.tagger.close()
            self.tagger = None
        busy = False
        try:
            busy = not self.core.library.is_idle()
        except OSError:
            pass
        self.lock.release()
        if busy:
            spawn_detached()        # keep tagging windows / counting playtime after we exit
        if self.tray:
            self.tray.hide()


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv
    QGuiApplication.setDesktopFileName("glacier")
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_ID)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    # Single instance: ask an existing window to come forward.
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show\n")
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    app.setStyle("Fusion")
    app.setPalette(theme.palette())
    app.setStyleSheet(theme.QSS)

    win = MainWindow()

    def on_conn():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(lambda: (c.readAll(), win.bring_to_front()))
    server.newConnection.connect(on_conn)

    signal.signal(signal.SIGINT, lambda *_: win.quit())
    signal.signal(signal.SIGTERM, lambda *_: win.quit())
    keepalive = QTimer(interval=300, timeout=lambda: None)  # let Python handle signals
    keepalive.start()

    if "--hidden" not in argv:
        win.show()
    rc = app.exec()
    server.close()
    return rc
