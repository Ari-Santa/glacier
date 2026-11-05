"""Main window pages."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QTextCursor
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QPlainTextEdit, QProgressBar, QScrollArea, QVBoxLayout,
                             QWidget)

from .. import desktop, paths, proton, stats
from ..battlenet import LOCALES
from ..library import GameState
from . import theme
from .widgets import Banner, FlowLayout, GameCard, button, game_pixmap, label, repolish

if TYPE_CHECKING:
    from .app import MainWindow


def scroll(widget: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setWidget(widget)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return sa


def page_header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(label(title, "PageTitle"))
    if subtitle:
        lay.addWidget(label(subtitle, muted=True, wrap=True))
    return w


def open_path(p: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


# =========================================================================== Library

class GameDetail(QFrame):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        self.setObjectName("Panel")
        self.setFixedWidth(360)
        self.uid: str | None = None
        self._loading = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(20, 16, 20, 20)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.addStretch(1)
        close = button("", "flat", "window-close", "Close")
        close.clicked.connect(lambda: self.win.library_page.select(None))
        top.addWidget(close)
        lay.addLayout(top)

        self.art = QLabel()
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setFixedHeight(140)
        lay.addWidget(self.art)
        self.title = label("", "SectionTitle", wrap=True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.title.font()
        f.setPointSizeF(14)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.status = QLabel()
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        lay.addWidget(self.progress)

        self.play = button("Play", "primary")
        self.play.setMinimumHeight(40)
        self.play.clicked.connect(self._primary)
        lay.addWidget(self.play)

        row = QHBoxLayout()
        self.b_open = button("Battle.net", icon_name="go-next", tip="Show this game in Battle.net")
        self.b_open.clicked.connect(lambda: self.win.game_action(self.uid, "open"))
        self.b_folder = button("Folder", icon_name="folder-open", tip="Open the install folder")
        self.b_folder.clicked.connect(self._open_folder)
        row.addWidget(self.b_open)
        row.addWidget(self.b_folder)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.b_repair = button("Repair", icon_name="tools-wizard", tip="Scan and repair via Battle.net")
        self.b_repair.clicked.connect(lambda: self.win.game_action(self.uid, "repair"))
        self.b_uninstall = button("Uninstall", "danger", "edit-delete")
        self.b_uninstall.clicked.connect(lambda: self.win.game_action(self.uid, "uninstall"))
        row.addWidget(self.b_repair)
        row.addWidget(self.b_uninstall)
        lay.addLayout(row)

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.info.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        lay.addWidget(self.info)
        self.note = label("", wrap=True)
        self.note.setStyleSheet(f"color: {theme.WARN}; background: transparent;")
        lay.addWidget(self.note)

        box = QGroupBox("Launch options")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.mode = QComboBox()
        self.mode.addItem("Through Battle.net", "bnet")
        self.mode.addItem("Run the game exe directly", "direct")
        self.mode.setToolTip("Direct mode starts the game executable itself. It skips the Battle.net "
                             "login handshake, so only games that allow it (e.g. WoW) work this way.")
        self.code = QLineEdit()
        self.code.setPlaceholderText("auto")
        self.code.setToolTip("Override the Battle.net launch code (advanced)")
        self.args = QLineEdit()
        self.args.setPlaceholderText("extra arguments (direct mode)")
        self.mangohud = self._tri("MangoHud")
        self.gamemode = self._tri("GameMode")
        self.gamescope = self._tri("Gamescope")
        self.env = QPlainTextEdit()
        self.env.setPlaceholderText("KEY=value per line")
        self.env.setFixedHeight(70)
        self.menu = QCheckBox("Show in the app menu")
        form.addRow("Mode", self.mode)
        form.addRow("Launch code", self.code)
        form.addRow("Arguments", self.args)
        form.addRow(self.mangohud)
        form.addRow(self.gamemode)
        form.addRow(self.gamescope)
        form.addRow("Environment", self.env)
        form.addRow(self.menu)
        lay.addWidget(box)
        hint = label("Checkbox states: ✓ on, empty off, grey = use global setting.", muted=True, wrap=True)
        hint.setStyleSheet(f"color: {theme.MUTED}; font-size: 8.5pt; background: transparent;")
        lay.addWidget(hint)
        b_icon = button("Re-extract icon", "flat", "view-refresh")
        b_icon.clicked.connect(lambda: self.win.game_action(self.uid, "icon"))
        lay.addWidget(b_icon)
        lay.addStretch(1)

        for w in (self.mode,):
            w.currentIndexChanged.connect(self._save)
        for w in (self.code, self.args):
            w.editingFinished.connect(self._save)
        for w in (self.mangohud, self.gamemode, self.gamescope):
            w.stateChanged.connect(self._save)
        self.menu.toggled.connect(self._save)
        self._env_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save)
        self.env.textChanged.connect(self._env_timer.start)
        outer.addWidget(scroll(body))

    def _tri(self, text: str) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setTristate(True)
        cb.setCheckState(Qt.CheckState.PartiallyChecked)
        return cb

    @staticmethod
    def _tri_value(cb: QCheckBox):
        s = cb.checkState()
        return None if s == Qt.CheckState.PartiallyChecked else s == Qt.CheckState.Checked

    @staticmethod
    def _set_tri(cb: QCheckBox, v):
        cb.setCheckState(Qt.CheckState.PartiallyChecked if v is None else
                         Qt.CheckState.Checked if v else Qt.CheckState.Unchecked)

    def show_game(self, st: GameState):
        first = self.uid != st.uid
        self.uid = st.uid
        self.art.setPixmap(game_pixmap(st, 128))
        self.title.setText(st.game.name)
        color = theme.STATUS_COLORS.get(st.status_text, theme.ACCENT)
        self.status.setText(st.status_text)
        self.status.setStyleSheet(f"color: {color}; background: transparent; font-weight: 600;")
        self.progress.setVisible(st.progress is not None)
        if st.progress is not None:
            self.progress.setValue(int(st.progress * 1000))
        if st.running:
            self.play.setText("Stop")
            self.play.setProperty("kind", "danger")
        elif st.installed:
            self.play.setText("Play")
            self.play.setProperty("kind", "primary")
        else:
            self.play.setText("Install with Battle.net")
            self.play.setProperty("kind", "primary")
        repolish(self.play)
        for b in (self.b_folder, self.b_repair, self.b_uninstall):
            b.setEnabled(st.installed)
        s = stats.load().get(st.uid, {})
        lines = []
        if st.installed:
            lines.append(f"Version: {st.version or 'unknown'}")
        if st.latest:
            lines.append(f"Latest: {st.latest}")
        if s.get("playtime"):
            lines.append(f"Played: {stats.fmt_playtime(s['playtime'])}")
        if s.get("last_played"):
            lines.append("Last launched: " + time.strftime("%d %b %Y %H:%M", time.localtime(s["last_played"])))
        if st.install_dir:
            lines.append(f"Location: {st.install_dir}")
        if st.exe:
            lines.append(f"Executable: {st.exe.name}")
        lines.append(f"Product code: {st.uid}" + (f" · uid {st.db_uid}" if st.db_uid and st.db_uid != st.uid else ""))
        self.info.setText("\n".join(lines))
        self.note.setText(st.game.note)
        self.note.setVisible(bool(st.game.note))
        if first:
            self._load_settings(st)

    def _load_settings(self, st: GameState):
        self._loading = True
        g = self.win.core.cfg.game(st.uid)
        self.mode.setCurrentIndex(max(0, self.mode.findData(g["launch_mode"])))
        self.code.setText(g.get("launch_code") or "")
        self.code.setPlaceholderText(f"auto ({'launch_uid ' + st.db_uid if st.db_uid else st.game.launch})")
        self.args.setText(g.get("args") or "")
        self._set_tri(self.mangohud, g.get("mangohud"))
        self._set_tri(self.gamemode, g.get("gamemode"))
        self._set_tri(self.gamescope, g.get("gamescope"))
        self.env.setPlainText("\n".join(f"{k}={v}" for k, v in (g.get("env") or {}).items()))
        self.menu.setChecked(not g.get("hidden"))
        self._loading = False

    def _save(self):
        if self._loading or not self.uid:
            return
        env = {}
        for line in self.env.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    env[k.strip()] = v.strip()
        hidden_before = self.win.core.cfg.game(self.uid).get("hidden")
        self.win.core.cfg.set_game(self.uid, launch_mode=self.mode.currentData(),
                                   launch_code=self.code.text().strip(), args=self.args.text().strip(),
                                   mangohud=self._tri_value(self.mangohud),
                                   gamemode=self._tri_value(self.gamemode),
                                   gamescope=self._tri_value(self.gamescope), env=env,
                                   hidden=not self.menu.isChecked())
        if hidden_before != (not self.menu.isChecked()):
            self.win.sync_menus()

    def _primary(self):
        st = self.win.core.library.states.get(self.uid)
        if not st:
            return
        self.win.game_action(st.uid, "stop" if st.running else "play" if st.installed else "install")

    def _open_folder(self):
        st = self.win.core.library.states.get(self.uid)
        if st and st.install_dir:
            open_path(st.install_dir)


class LibraryPage(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        self.cards: dict[str, GameCard] = {}
        self.selected: str | None = None
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        lay = QVBoxLayout(main)
        lay.setContentsMargins(28, 22, 28, 12)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.addWidget(page_header("Library"))
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search games…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self.apply_filter)
        self.filter = QComboBox()
        self.filter.addItem("Installed", "installed")
        self.filter.addItem("All games", "all")
        self.filter.setCurrentIndex(1 if win.core.cfg["show_all_games"] else 0)
        self.filter.currentIndexChanged.connect(self._filter_changed)
        refresh = button("", icon_name="view-refresh", tip="Rescan library and check for updates")
        refresh.clicked.connect(lambda: self.win.rescan(check_updates=True))
        head.addWidget(self.search)
        head.addWidget(self.filter)
        head.addWidget(refresh)
        lay.addLayout(head)

        self.banner = Banner()
        self.banner.setVisible(False)
        self.banner.btn.clicked.connect(self._banner_action)
        self._banner_action_fn = None
        lay.addWidget(self.banner)

        self.empty = label("", muted=True, wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(120)
        lay.addWidget(self.empty)

        grid_host = QWidget()
        self.flow = FlowLayout(grid_host, spacing=18)
        self.flow.setContentsMargins(0, 4, 0, 24)
        lay.addWidget(scroll(grid_host), 1)
        root.addWidget(main, 1)
        self.detail = GameDetail(win)
        self.detail.setVisible(False)
        root.addWidget(self.detail)

    # ---- banner -------------------------------------------------------------
    def set_banner(self, title: str | None, text: str = "", button_text: str = "", action=None,
                   fraction: float | None = None, pixmap=None):
        if title is None:
            self.banner.setVisible(False)
            return
        self._banner_action_fn = action
        self.banner.show_message(title, text, button_text, fraction, pixmap)

    def _banner_action(self):
        if self._banner_action_fn:
            self._banner_action_fn()

    # ---- cards ----------------------------------------------------------------
    def _filter_changed(self):
        self.win.core.cfg.update(show_all_games=self.filter.currentData() == "all")
        self.apply_filter()

    def update_states(self, states: dict[str, GameState]):
        order = sorted(states.values(), key=lambda s: (not s.installed, not s.game.featured, s.game.name))
        for st in order:
            card = self.cards.get(st.uid)
            if card is None:
                card = GameCard(st)
                card.clicked.connect(self.select)
                card.action.connect(self.win.game_action)
                self.cards[st.uid] = card
            else:
                card.set_state(st)
        # Re-insert in order (cheap: a few dozen widgets)
        while self.flow.count():
            self.flow.takeAt(0)
        for st in order:
            self.flow.addWidget(self.cards[st.uid])
        self.apply_filter()
        if self.selected and self.selected in states:
            self.detail.show_game(states[self.selected])

    def refresh_cards(self, states: dict[str, GameState]):
        for uid, card in self.cards.items():
            st = states.get(uid)
            if st:
                card.set_state(st)
        if self.selected and self.selected in states and self.detail.isVisible():
            self.detail.show_game(states[self.selected])

    def apply_filter(self):
        q = self.search.text().strip().lower()
        show_all = self.filter.currentData() == "all"
        visible = 0
        for uid, card in self.cards.items():
            st = card.st
            ok = st.installed or st.progress is not None or show_all
            if q:
                ok = (q in st.game.name.lower() or q in uid) and (st.installed or show_all)
            card.setVisible(bool(ok))
            visible += bool(ok)
        self.flow.invalidate()
        core = self.win.core
        if visible:
            self.empty.setVisible(False)
        else:
            self.empty.setVisible(True)
            if not core.bnet.installed():
                self.empty.setText("Battle.net is not set up yet.")
            elif q:
                self.empty.setText("No games match your search.")
            else:
                self.empty.setText("No games installed yet.\nLog in to Battle.net and install a game — it shows up "
                                   "here automatically. Or switch to “All games” to install from Glacier.")

    def select(self, uid: str | None):
        if self.selected and self.selected in self.cards:
            self.cards[self.selected].set_selected(False)
        self.selected = uid
        if uid is None:
            self.detail.setVisible(False)
            return
        self.cards[uid].set_selected(True)
        st = self.win.core.library.states.get(uid)
        if st:
            self.detail.show_game(st)
            self.detail.setVisible(True)


# =========================================================================== Battle.net

class BattleNetPage(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 28)
        lay.setSpacing(14)
        lay.addWidget(page_header("Battle.net", "The official client runs inside Glacier's Proton prefix. "
                                                "It installs and updates your games."))
        card = QFrame()
        card.setObjectName("Panel")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(18)
        self.logo = QLabel()
        self.logo.setFixedSize(72, 72)
        cl.addWidget(self.logo)
        col = QVBoxLayout()
        self.state = label("", "SectionTitle")
        self.details = label("", muted=True, wrap=True)
        col.addWidget(self.state)
        col.addWidget(self.details)
        cl.addLayout(col, 1)
        btns = QVBoxLayout()
        self.b_start = button("Open Battle.net", "primary", "media-playback-start")
        self.b_start.clicked.connect(lambda: self.win.start_battlenet())
        self.b_bg = button("Start in background", icon_name="go-down",
                           tip="Start minimised to the tray so it can update your games")
        self.b_bg.clicked.connect(lambda: self.win.start_battlenet(background=True))
        self.b_stop = button("Stop everything", "danger", "media-playback-stop",
                             "Close Battle.net and every game in the prefix")
        self.b_stop.clicked.connect(self.win.stop_all)
        for b in (self.b_start, self.b_bg, self.b_stop):
            btns.addWidget(b)
        cl.addLayout(btns)
        lay.addWidget(card)

        box = QGroupBox("Maintenance")
        g = QGridLayout(box)
        self.b_install = button("Install Battle.net", icon_name="download",
                                tip="Download the official installer and install it unattended")
        self.b_install.clicked.connect(self.win.run_setup)
        self.b_reinstall = button("Reinstall / repair client", icon_name="view-refresh")
        self.b_reinstall.clicked.connect(lambda: self.win.run_setup(force=True))
        self.hw = QCheckBox("Hardware acceleration in the Battle.net window")
        self.hw.setToolTip("Turn off if the Battle.net window is black or blank. Takes effect on the next start.")
        self.hw.setChecked(bool(win.core.cfg["bnet_hw_accel"]))
        self.hw.toggled.connect(self._hw)
        self.autostart = QCheckBox("Start Battle.net in the background when I log in (keeps games updated)")
        self.autostart.setChecked(desktop.autostart_enabled())
        self.autostart.toggled.connect(lambda v: desktop.set_autostart(v))
        g.addWidget(self.b_install, 0, 0)
        g.addWidget(self.b_reinstall, 0, 0)
        g.addWidget(self.hw, 1, 0, 1, 2)
        g.addWidget(self.autostart, 2, 0, 1, 2)
        lay.addWidget(box)

        box = QGroupBox("Prefix tools")
        g = QGridLayout(box)
        tools = [("Wine configuration", "winecfg", "preferences-system"),
                 ("Registry editor", "regedit", "document-edit"),
                 ("Task manager", "taskmgr", "utilities-system-monitor"),
                 ("File explorer", "explorer", "system-file-manager")]
        for i, (text, tool, ic) in enumerate(tools):
            b = button(text, icon_name=ic)
            b.clicked.connect(lambda _=False, t=tool: self.win.run_tool(t))
            g.addWidget(b, i // 2, i % 2)
        b = button("Run a Windows program…", icon_name="system-run")
        b.clicked.connect(self._run_exe)
        g.addWidget(b, 2, 0)
        b = button("Open prefix folder", icon_name="folder-open")
        b.clicked.connect(lambda: open_path(self.win.core.engine.drive_c if self.win.core.engine.drive_c.exists()
                                            else self.win.core.engine.compat_dir))
        g.addWidget(b, 2, 1)
        lay.addWidget(box)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll(body))

    def _hw(self, v: bool):
        self.win.core.cfg.update(bnet_hw_accel=v)
        try:
            self.win.core.bnet.apply_config()
        except OSError:
            pass

    def _run_exe(self):
        f, _ = QFileDialog.getOpenFileName(self, "Run Windows program", str(self.win.core.engine.drive_c),
                                           "Windows programs (*.exe *.msi *.bat);;All files (*)")
        if f:
            self.win.run_exe(f)

    def refresh(self, status):
        from .widgets import pixmap_from_file
        pm = pixmap_from_file(desktop.icon_png(desktop.icon_name("battlenet")), 72) or \
            pixmap_from_file(paths.ASSETS_DIR / "glacier-128.png", 72)
        if pm:
            self.logo.setPixmap(pm)
        core = self.win.core
        if not status.installed:
            self.state.setText("Not installed")
            self.details.setText("Glacier downloads the official installer from battle.net and installs it "
                                 "for you — no clicks needed.")
        else:
            self.state.setText("Running" if status.running else "Installed")
            parts = [f"Client {status.version or '?'}"]
            if status.latest and status.latest != status.version:
                parts.append(f"update {status.latest} available (installs automatically on start)")
            if status.agent_version:
                parts.append(f"Agent {status.agent_version}")
            r = core.engine.runner()
            parts.append(f"Proton: {r.name if r else 'none'}")
            parts.append(f"Prefix: {core.engine.pfx}")
            self.details.setText("\n".join(parts))
        self.state.setStyleSheet(f"color: {theme.OK if status.running else theme.TEXT}; background: transparent;")
        self.b_start.setEnabled(status.installed or True)
        self.b_start.setText("Show Battle.net" if status.running else
                             "Open Battle.net" if status.installed else "Set up Battle.net")
        self.b_bg.setEnabled(status.installed and not status.running)
        self.b_stop.setEnabled(status.running or bool(core.engine.processes()))
        self.b_install.setVisible(not status.installed)
        self.b_reinstall.setVisible(status.installed)


# =========================================================================== Proton

class ProtonPage(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 28)
        lay.setSpacing(14)
        lay.addWidget(page_header("Proton", "Choose the compatibility layer Battle.net and your games run on."))

        box = QGroupBox("Active build")
        v = QVBoxLayout(box)
        self.runner = QComboBox()
        self.runner.currentIndexChanged.connect(self._runner_changed)
        v.addWidget(self.runner)
        self.runtime = label("", muted=True, wrap=True)
        v.addWidget(self.runtime)
        self.use_rt = QCheckBox("Run inside the Steam Linux Runtime container (recommended)")
        self.use_rt.setChecked(bool(win.core.cfg["use_runtime"]))
        self.use_rt.toggled.connect(lambda val: (win.core.cfg.update(use_runtime=val), self.refresh()))
        v.addWidget(self.use_rt)
        self.b_runtime = button("Download runtime", icon_name="download")
        self.b_runtime.clicked.connect(self.win.ensure_runtime)
        v.addWidget(self.b_runtime, 0, Qt.AlignmentFlag.AlignLeft)
        self.auto_ge = QCheckBox("Keep GE-Proton up to date automatically")
        self.auto_ge.setChecked(bool(win.core.cfg["auto_update_ge"]))
        self.auto_ge.toggled.connect(lambda val: win.core.cfg.update(auto_update_ge=val))
        v.addWidget(self.auto_ge)
        lay.addWidget(box)

        box = QGroupBox("GE-Proton releases")
        v = QVBoxLayout(box)
        top = QHBoxLayout()
        top.addWidget(label("Community builds with extra fixes, verified against their sha512 checksums.",
                            muted=True, wrap=True), 1)
        b = button("Refresh", icon_name="view-refresh")
        b.clicked.connect(self.load_releases)
        top.addWidget(b)
        v.addLayout(top)
        self.releases = QListWidget()
        self.releases.setMinimumHeight(260)
        v.addWidget(self.releases)
        row = QHBoxLayout()
        self.b_install = button("Install selected", "primary", "download")
        self.b_install.clicked.connect(self._install_selected)
        self.b_remove = button("Remove selected", "danger", "edit-delete")
        self.b_remove.clicked.connect(self._remove_selected)
        row.addWidget(self.b_install)
        row.addWidget(self.b_remove)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(box)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll(body))
        self._releases: list[proton.Release] = []
        self._loading = False

    def refresh(self):
        eng = self.win.core.engine
        self._loading = True
        self.runner.clear()
        runners = eng.runners(refresh=True)
        current = eng.runner()
        for r in runners:
            self.runner.addItem(r.label, r.id)
        if current:
            self.runner.setCurrentIndex(max(0, self.runner.findData(current.id)))
        self._loading = False
        if current is None:
            self.runtime.setText("No Proton found. Install GE-Proton below or Proton through Steam.")
            self.b_runtime.setVisible(False)
        else:
            rt = eng.runtime(current)
            if not self.win.core.cfg["use_runtime"]:
                txt = "Runs directly on the host (no container)."
            elif rt:
                txt = f"Container: {rt.name}  ·  {rt.path}"
            elif current.runtime_appid:
                txt = f"Needs the Steam Linux Runtime (app {current.runtime_appid}), which is not installed."
            else:
                txt = "This build does not declare a runtime; runs on the host."
            self.runtime.setText(f"{current.path}\n{txt}")
            self.b_runtime.setVisible(bool(self.win.core.cfg["use_runtime"] and current.runtime_appid and not rt))
        self._fill_releases()

    def _runner_changed(self):
        if self._loading:
            return
        rid = self.runner.currentData()
        if rid and rid != self.win.core.cfg["runner"]:
            self.win.core.cfg.update(runner=rid)
            self.win.on_runner_changed()

    def load_releases(self):
        def work(progress, cancel):
            return proton.list_ge_releases(12)

        def done(rels):
            self._releases = rels
            self._fill_releases()
        self.win.tasks.start("Fetching GE-Proton releases", work, done, key="ge-list", quiet=True)

    def _fill_releases(self):
        self.releases.clear()
        installed = {r.path.name: r for r in self.win.core.engine.runners() if r.is_ge}
        for rel in self._releases:
            tag = "  ✓ installed" if rel.tag in installed else ""
            it = QListWidgetItem(f"{rel.tag}    {rel.published}    {rel.size >> 20} MiB{tag}")
            it.setData(Qt.ItemDataRole.UserRole, rel.tag)
            self.releases.addItem(it)
        for name, r in installed.items():
            if not any(rel.tag == name for rel in self._releases):
                it = QListWidgetItem(f"{name}    (installed{', ' + r.source if r.source != 'glacier' else ''})")
                it.setData(Qt.ItemDataRole.UserRole, name)
                self.releases.addItem(it)

    def _selected_tag(self) -> str | None:
        it = self.releases.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _install_selected(self):
        tag = self._selected_tag()
        rel = next((r for r in self._releases if r.tag == tag), None)
        if rel:
            self.win.install_ge(rel)

    def _remove_selected(self):
        tag = self._selected_tag()
        r = next((r for r in self.win.core.engine.runners() if r.path.name == tag), None)
        if r:
            self.win.remove_runner(r)


# =========================================================================== Settings

class SettingsPage(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        cfg = win.core.cfg
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 22, 28, 28)
        lay.setSpacing(14)
        lay.addWidget(page_header("Settings"))

        box = QGroupBox("Locations")
        form = QFormLayout(box)
        self.prefix = self._path_row(cfg["prefix"], "prefix", directory=True,
                                     tip="Wine prefix used for Battle.net and all games. Changing it starts "
                                         "with a fresh prefix.")
        form.addRow("Wine prefix", self.prefix)
        self.library = self._path_row(cfg["library_dir"], "library_dir", directory=True,
                                      tip="Optional: a big disk folder that appears as a drive in Battle.net, so "
                                          "you can install games there.")
        form.addRow("Game library folder", self.library)
        self.drive = QComboBox()
        for letter in "defghijklmnopqrstuvwxy":
            self.drive.addItem(f"{letter.upper()}:", letter)
        self.drive.setCurrentIndex(max(0, self.drive.findData(cfg["library_drive"])))
        self.drive.currentIndexChanged.connect(lambda: self._set("library_drive", self.drive.currentData(),
                                                                 remap=True))
        form.addRow("Library drive letter", self.drive)
        lay.addWidget(box)

        box = QGroupBox("Battle.net")
        form = QFormLayout(box)
        self.lang = QComboBox()
        self.lang.addItem("Automatic (from system)", "")
        for loc in sorted(LOCALES):
            self.lang.addItem(loc, loc)
        self.lang.setCurrentIndex(max(0, self.lang.findData(cfg["language"])))
        self.lang.currentIndexChanged.connect(lambda: self._set("language", self.lang.currentData()))
        form.addRow("Install language", self.lang)
        self.region = QComboBox()
        for text, val in (("Automatic", ""), ("Americas", "us"), ("Europe", "eu"), ("Korea", "kr"),
                          ("Taiwan", "tw"), ("China", "cn")):
            self.region.addItem(text, val)
        self.region.setCurrentIndex(max(0, self.region.findData(cfg["region"])))
        self.region.currentIndexChanged.connect(lambda: self._set("region", self.region.currentData()))
        form.addRow("Region for update checks", self.region)
        lay.addWidget(box)

        box = QGroupBox("Game launch options (global)")
        v = QVBoxLayout(box)
        self._check(v, "MangoHud performance overlay", "mangohud")
        self._check(v, "Feral GameMode", "gamemode")
        row = QHBoxLayout()
        self._check(row, "Gamescope", "gamescope")
        self.gs_args = QLineEdit(cfg["gamescope_args"])
        self.gs_args.setPlaceholderText("-f -W 2560 -H 1440")
        self.gs_args.editingFinished.connect(lambda: self._set("gamescope_args", self.gs_args.text().strip()))
        row.addWidget(self.gs_args, 1)
        v.addLayout(row)
        self._check(v, "DXVK HUD (fps, GPU load)", "dxvk_hud")
        self._check(v, "Wine Wayland driver (experimental; per-game taskbar icons need X11)", "wayland")
        self._check(v, "HDR (needs the Wayland driver)", "hdr")
        self._check(v, "Disable fsync", "disable_fsync")
        self._check(v, "Disable esync", "disable_esync")
        self._check(v, "Proton debug logging", "proton_log")
        self._check(v, "Restart Battle.net when launch options change (so games pick them up)",
                    "restart_bnet_on_env_change")
        form = QFormLayout()
        self.wrapper = QLineEdit(cfg["wrapper"])
        self.wrapper.setPlaceholderText("e.g. obs-gamecapture")
        self.wrapper.editingFinished.connect(lambda: self._set("wrapper", self.wrapper.text().strip()))
        form.addRow("Wrapper command", self.wrapper)
        self.env = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in (cfg["env"] or {}).items()))
        self.env.setPlaceholderText("KEY=value per line")
        self.env.setFixedHeight(80)
        self._env_timer = QTimer(self, singleShot=True, interval=600, timeout=self._save_env)
        self.env.textChanged.connect(self._env_timer.start)
        form.addRow("Environment", self.env)
        v.addLayout(form)
        note = label("Options apply when Battle.net starts; Glacier restarts it automatically when they "
                     "change and no game is running.", muted=True, wrap=True)
        v.addWidget(note)
        lay.addWidget(box)

        box = QGroupBox("Desktop integration")
        v = QVBoxLayout(box)
        self._check(v, "Add installed games to the app menu automatically", "desktop_entries",
                    after=self.win.sync_menus)
        self._check(v, "Show a tray icon", "tray", after=self.win.update_tray)
        self._check(v, "Closing the window keeps Glacier in the tray", "close_to_tray")
        self._check(v, "Check Blizzard for game updates on start", "check_updates")
        row = QHBoxLayout()
        b = button("Rebuild menu entries & icons", icon_name="view-refresh")
        b.clicked.connect(lambda: self.win.sync_menus(force_icons=True))
        row.addWidget(b)
        b = button("Remove all menu entries", "danger", "edit-delete")
        b.clicked.connect(self.win.remove_menus)
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        lay.addWidget(box)
        lay.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll(body))

    def _check(self, layout, text: str, key: str, after=None) -> QCheckBox:
        cb = QCheckBox(text)
        cb.setChecked(bool(self.win.core.cfg[key]))

        def changed(v):
            self.win.core.cfg.update(**{key: v})
            if after:
                after()
        cb.toggled.connect(changed)
        layout.addWidget(cb)
        return cb

    def _set(self, key: str, value, remap: bool = False):
        self.win.core.cfg.update(**{key: value})
        if remap:
            self.win.remap_library()

    def _save_env(self):
        env = {}
        for line in self.env.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    env[k.strip()] = v.strip()
        self.win.core.cfg.update(env=env)

    def _path_row(self, value: str, key: str, directory: bool, tip: str) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        edit.setToolTip(tip)
        edit.setPlaceholderText("not set")

        def commit():
            val = edit.text().strip()
            if val != (self.win.core.cfg[key] or ""):
                self._set(key, val, remap=(key == "library_dir"))
                if key == "prefix":
                    self.win.on_prefix_changed()
        edit.editingFinished.connect(commit)
        b = button("Browse…")

        def browse():
            d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or str(paths.HOME))
            if d:
                edit.setText(d)
                commit()
        b.clicked.connect(browse)
        h.addWidget(edit, 1)
        h.addWidget(b)
        return w


# =========================================================================== Logs

class LogsPage(QWidget):
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(12)
        head = QHBoxLayout()
        head.addWidget(page_header("Logs", "Output from Proton, Battle.net and games."), 1)
        b = button("Open folder", icon_name="folder-open")
        b.clicked.connect(lambda: open_path(paths.LOG_DIR))
        head.addWidget(b)
        lay.addLayout(head)
        row = QHBoxLayout()
        self.files = QListWidget()
        self.files.setFixedWidth(280)
        self.files.currentItemChanged.connect(lambda *_: self._load(force=True))
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSizeF(9)
        self.view.setFont(mono)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        row.addWidget(self.files)
        row.addWidget(self.view, 1)
        lay.addLayout(row, 1)
        self._size = -1
        self.timer = QTimer(self, interval=1500, timeout=self._tick)

    def showEvent(self, e):
        self.refresh_list()
        self.timer.start()
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def refresh_list(self):
        current = self.files.currentItem().data(Qt.ItemDataRole.UserRole) if self.files.currentItem() else None
        logs = sorted(paths.LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0,
                      reverse=True)
        logs = [p for p in logs if not p.is_symlink()][:60]
        self.files.blockSignals(True)
        self.files.clear()
        for p in logs:
            it = QListWidgetItem(p.name)
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            self.files.addItem(it)
        self.files.blockSignals(False)
        if logs:
            idx = next((i for i, p in enumerate(logs) if str(p) == current), 0)
            self.files.setCurrentRow(idx)

    def _tick(self):
        if self.files.count() and self.files.currentRow() == 0:
            newest = max(paths.LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, default=None)
            if newest and str(newest) != self.files.item(0).data(Qt.ItemDataRole.UserRole) and not newest.is_symlink():
                self.refresh_list()
                return
        self._load()

    def _load(self, force: bool = False):
        it = self.files.currentItem()
        if not it:
            self.view.setPlainText("No logs yet.")
            return
        p = Path(it.data(Qt.ItemDataRole.UserRole))
        try:
            size = p.stat().st_size
        except OSError:
            return
        if size == self._size and not force:
            return
        self._size = size
        with open(p, "rb") as fh:
            if size > 400_000:
                fh.seek(size - 400_000)
            text = fh.read().decode("utf-8", "replace")
        at_bottom = self.view.verticalScrollBar().value() >= self.view.verticalScrollBar().maximum() - 4
        self.view.setPlainText(text)
        if at_bottom or force:
            self.view.moveCursor(QTextCursor.MoveOperation.End)
