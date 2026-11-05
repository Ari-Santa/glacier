"""Game library: what is installed, where, which version, and how to show it."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import blizzard, desktop, games, paths, pe, stats
from .battlenet import BNET_HELPERS, SYSTEM_EXES, BattleNet
from .engine import Engine, EngineError
from .games import GameDef

BNET_UID = "battlenet"
IGNORED_PRODUCTS = {"agent", "bna", "battle.net", "bts", "catalogs"}


@dataclass
class GameState:
    game: GameDef
    installed: bool = False
    install_dir: Path | None = None
    exe: Path | None = None
    version: str = ""
    db_uid: str | None = None          # product.db uid (for --exec="launch_uid ...")
    playable: bool = False
    progress: float | None = None      # 0..1 while Battle.net downloads/updates it
    remaining_bytes: int = 0
    latest: str = ""                   # newest live version (after check_updates)
    running: bool = False
    extra_exes: set[str] = field(default_factory=set)

    @property
    def uid(self) -> str:
        return self.game.uid

    @property
    def update_available(self) -> bool:
        return bool(self.installed and self.latest and self.version and self.latest != self.version)

    @property
    def status_text(self) -> str:
        if self.running:
            return "Running"
        if self.progress is not None:
            verb = "Updating" if self.installed else "Installing"
            return f"{verb} {self.progress * 100:.0f}%"
        if not self.installed:
            return "Not installed"
        if self.update_available:
            return "Update available"
        return "Ready"

    def exe_names(self) -> set[str]:
        names = {Path(p).name.lower() for p in self.game.exes if "*" not in Path(p).name}
        if self.exe:
            names.add(self.exe.name.lower())
        return names | self.extra_exes


def _numeric_sort_key(p: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(p))]


def find_main_exe(install_dir: Path, game: GameDef) -> Path | None:
    if not install_dir or not install_dir.is_dir():
        return None
    for pattern in game.exes:
        if any(ch in pattern for ch in "*?["):
            matches = sorted(install_dir.glob(pattern), key=_numeric_sort_key)
            matches = [m for m in matches if m.is_file()]
            if matches:
                return matches[-1]
        else:
            p = install_dir / pattern
            if p.is_file():
                return p
            # case-insensitive fallback
            parent = install_dir / os.path.dirname(pattern)
            if parent.is_dir():
                low = os.path.basename(pattern).lower()
                for c in parent.iterdir():
                    if c.name.lower() == low and c.is_file():
                        return c
    # Heuristic: biggest plausible .exe within a few levels.
    root = install_dir / game.subdir if game.subdir and (install_dir / game.subdir).is_dir() else install_dir
    best: tuple[int, Path] | None = None
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - base_depth
        if depth >= 3:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(("_CommonRedist", "Redist", "Data", "Cache",
                                                               "Logs", "Errors", "WTF", "Interface"))]
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(".exe") or any(bad in low for bad in games.NOT_GAME_EXE):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if best is None or size > best[0]:
                best = (size, p)
    return best[1] if best else None


def _dynamic_game(code: str) -> GameDef:
    name = code.replace("_", " ").title()
    return GameDef(code, name, code.upper(), name, (), "#4a5568", code[:3].upper(), featured=False)


class Library:
    def __init__(self, engine: Engine, bnet: BattleNet):
        self.engine = engine
        self.bnet = bnet
        self.cfg = engine.cfg
        self.states: dict[str, GameState] = {}
        self._latest: dict[str, str] = {}
        self._exe_cache: dict[tuple[str, str], Path | None] = {}

    # ------------------------------------------------------------------ scanning
    def scan(self) -> dict[str, GameState]:
        states: dict[str, GameState] = {g.uid: GameState(g) for g in games.GAMES}
        for p in self.bnet.products():
            code = (p.product_code or p.uid).lower()
            if code in IGNORED_PRODUCTS or p.uid.lower() in IGNORED_PRODUCTS:
                continue
            g = games.lookup(code) or games.lookup(p.uid) or _dynamic_game(code)
            st = states.setdefault(g.uid, GameState(g))
            install_dir = self.engine.win_to_unix(p.install_path) if p.install_path else None
            st.db_uid = p.uid or None
            st.install_dir = install_dir
            st.version = p.version
            st.playable = p.playable
            st.progress = p.progress
            st.remaining_bytes = p.remaining_bytes
            st.installed = bool(p.installed or p.playable)
        for st in states.values():
            g = st.game
            if st.install_dir is None:
                # Not known to Battle.net: look in the usual places.
                for base in self._search_roots():
                    cand = base / g.folder
                    if cand.is_dir() and (not g.subdir or (cand / g.subdir).is_dir()):
                        exe = self._exe(cand, g)
                        if exe:
                            st.install_dir, st.exe, st.installed = cand, exe, True
                            break
            elif st.installed:
                st.exe = self._exe(st.install_dir, g)
            if st.installed and st.install_dir and not st.version:
                for b in blizzard.read_build_info(st.install_dir):
                    if b.active and (not b.product or b.product == g.uid):
                        st.version = b.version
                        break
            st.latest = self._latest.get(g.uid, "")
        self.states = states
        self.refresh_running()
        return states

    def _search_roots(self) -> list[Path]:
        roots = [self.engine.drive_c / "Program Files (x86)", self.engine.drive_c / "Program Files"]
        lib = self.engine.library_dir()
        if lib:
            roots.insert(0, lib)
        return [r for r in roots if r.is_dir()]

    def _exe(self, install_dir: Path, g: GameDef) -> Path | None:
        key = (str(install_dir), g.uid)
        cached = self._exe_cache.get(key)
        if cached is not None and cached.is_file():
            return cached
        exe = find_main_exe(install_dir, g)
        self._exe_cache[key] = exe
        return exe

    # ------------------------------------------------------------------ running state
    def exe_map(self) -> dict[str, str]:
        """lower-case exe basename -> game uid (plus Battle.net)."""
        m = {"battle.net.exe": BNET_UID}
        for st in self.states.values():
            if st.installed:
                for n in st.exe_names():
                    m.setdefault(n, st.uid)
        return m

    def refresh_running(self, procs=None) -> set[str]:
        procs = self.engine.processes() if procs is None else procs
        exes = {p.exe for p in procs if not p.is_subprocess}
        emap = self.exe_map()
        running = {emap[e] for e in exes if e in emap}
        for st in self.states.values():
            st.running = st.uid in running
        return running

    # ------------------------------------------------------------------ updates
    def check_updates(self) -> dict[str, str]:
        region = self.bnet.region()
        for st in self.states.values():
            if not st.installed:
                continue
            try:
                rv = blizzard.latest_for_region(blizzard.remote_versions(st.game.uid), region)
            except Exception:
                continue
            if rv:
                self._latest[st.uid] = rv.version
                st.latest = rv.version
        return dict(self._latest)

    # ------------------------------------------------------------------ icons & menus
    def ensure_icon(self, st: GameState, force: bool = False) -> Path:
        name = desktop.icon_name(st.uid)
        png = paths.ICONS_DIR / f"{name}.png"
        marker = paths.ICONS_DIR / f"{name}.src"
        src = str(st.exe) if st.exe else "placeholder"
        try:
            up_to_date = png.exists() and marker.read_text() == src
        except OSError:
            up_to_date = False
        if up_to_date and not force:
            return png
        ico = pe.extract_icon(st.exe) if st.exe else None
        if ico is None and st.install_dir:
            # Fall back to any launcher exe in the install root.
            for c in sorted(st.install_dir.glob("*.exe")):
                if "launcher" in c.name.lower():
                    ico = pe.extract_icon(c)
                    if ico:
                        break
        if ico:
            try:
                png = desktop.install_icon_from_ico(name, ico)
            except Exception:
                ico = None
        if not ico:
            png = desktop.install_placeholder_icon(name, st.game.initials, st.game.color)
        paths.ICONS_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(src if ico else "placeholder")
        return png

    def ensure_bnet_icon(self) -> Path:
        name = desktop.icon_name(BNET_UID)
        png = paths.ICONS_DIR / f"{name}.png"
        if png.exists():
            return png
        ico = pe.extract_icon(self.bnet.exe) if self.bnet.installed() else None
        if ico:
            return desktop.install_icon_from_ico(name, ico)
        return desktop.install_placeholder_icon(name, "B", "#148eff")

    def write_menu_entry(self, st: GameState) -> Path:
        self.ensure_icon(st)
        actions = {
            "battlenet": ("Open in Battle.net", ["open", st.uid]),
        }
        comment = f"Play {st.game.name} through Battle.net (Proton)"
        return desktop.write_entry(desktop.desktop_id(st.uid), st.game.name, ["launch", st.uid],
                                   desktop.icon_name(st.uid), comment=comment,
                                   wm_class=desktop.icon_name(st.uid), categories="Game;",
                                   keywords="blizzard;battle.net;" + st.uid, actions=actions)

    def write_bnet_entry(self) -> Path:
        self.ensure_bnet_icon()
        return desktop.write_entry(desktop.desktop_id(BNET_UID), "Battle.net", ["bnet"],
                                   desktop.icon_name(BNET_UID), comment="Blizzard Battle.net (Proton)",
                                   wm_class=desktop.icon_name(BNET_UID), categories="Game;",
                                   keywords="blizzard;battle.net;launcher")

    def sync_menu_entries(self) -> list[str]:
        """Create entries for installed games, remove ones for games that are gone."""
        changed = []
        if self.bnet.installed():
            if not desktop.entry_exists(desktop.desktop_id(BNET_UID)):
                changed.append(BNET_UID)
            self.write_bnet_entry()
        if not self.cfg["desktop_entries"]:
            return changed
        for st in self.states.values():
            fid = desktop.desktop_id(st.uid)
            hidden = self.cfg.game(st.uid).get("hidden")
            if st.installed and not hidden:
                if not desktop.entry_exists(fid):
                    changed.append(st.uid)
                self.write_menu_entry(st)
            elif desktop.entry_exists(fid):
                desktop.remove_entry(fid)
                changed.append(st.uid)
        if changed:
            desktop.refresh()
        return changed

    def remove_menu_entries(self) -> None:
        for st in self.states.values():
            desktop.remove_entry(desktop.desktop_id(st.uid))
        desktop.remove_entry(desktop.desktop_id(BNET_UID))
        desktop.refresh()

    # ------------------------------------------------------------------ actions
    def get(self, key: str) -> GameState:
        if not self.states:
            self.scan()
        g = games.lookup(key)
        uid = g.uid if g else key.lower()
        st = self.states.get(uid)
        if st is None:
            raise EngineError(f"Unknown game '{key}'")
        return st

    def launch(self, key: str):
        st = self.get(key)
        if not st.installed:
            raise EngineError(f"{st.game.name} is not installed. Use 'install' to get it via Battle.net.")
        gcfg = self.cfg.game(st.uid)
        stats.touch(st.uid)
        if gcfg["launch_mode"] == "direct" and st.exe:
            args = shlex.split(gcfg.get("args") or "")
            spec = self.engine.build(st.exe, args, uid=st.uid, verb="auto",
                                     install_dir=st.install_dir, log_name=st.uid)
            return self.engine.spawn(spec)
        if gcfg.get("launch_code"):
            command = f"launch {gcfg['launch_code']}"
        elif st.db_uid:
            command = f"launch_uid {st.db_uid}"
        else:
            command = f"launch {st.game.launch}"
        return self.bnet.exec(command, uid=st.uid, log_name=st.uid)

    def install(self, key: str):
        """Ask Battle.net to install a game (it shows its own install dialog)."""
        st = self.get(key)
        return self.bnet.exec(f"install {st.game.launch}", log_name=f"install-{st.uid}")

    def repair(self, key: str):
        st = self.get(key)
        return self.bnet.exec(f"repair {st.game.launch}", log_name=f"repair-{st.uid}")

    def open_in_bnet(self, key: str):
        st = self.get(key)
        return self.bnet.exec(f"focus {st.game.launch}", log_name="battlenet")

    def uninstall_command(self, key: str) -> tuple[str, list[str]] | None:
        """Uninstaller registered in the prefix registry for this game, if any."""
        st = self.get(key)
        reg = self.engine.pfx / "system.reg"
        try:
            text = reg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        target = st.install_dir.resolve() if st.install_dir else None
        for block in text.split("\n\n"):
            if "\\\\Uninstall\\\\" not in block.split("\n", 1)[0]:
                continue
            vals = dict(re.findall(r'^"([^"]+)"="((?:\\.|[^"\\])*)"', block, re.M))
            vals = {k: v.replace("\\\\", "\\").replace('\\"', '"') for k, v in vals.items()}
            loc = vals.get("InstallLocation", "")
            if not loc or not vals.get("UninstallString"):
                continue
            loc_unix = self.engine.win_to_unix(loc)
            match = (target and loc_unix and loc_unix.resolve() == target) or \
                    (fnmatch.fnmatch(vals.get("DisplayName", "").lower(), f"*{st.game.name.lower()}*"))
            if match:
                parts = _split_windows_cmdline(vals["UninstallString"])
                if parts:
                    exe = self.engine.win_to_unix(parts[0])
                    if exe and exe.is_file():
                        return str(exe), parts[1:]
        return None

    def uninstall(self, key: str):
        cmd = self.uninstall_command(key)
        if not cmd:
            raise EngineError("No uninstaller registered; uninstall it from Battle.net instead.")
        exe, args = cmd
        spec = self.engine.build(exe, args, verb="run", log_name=f"uninstall-{key}", wrap=False)
        return self.engine.spawn(spec)

    def is_idle(self, procs=None) -> bool:
        procs = self.engine.processes() if procs is None else procs
        return not any(p.exe not in SYSTEM_EXES for p in procs)


def _split_windows_cmdline(s: str) -> list[str]:
    """Split a Windows command line (quotes, no escapes beyond \")."""
    out, cur, quoted, i = [], [], False, 0
    while i < len(s):
        c = s[i]
        if c == '"':
            quoted = not quoted
        elif c.isspace() and not quoted:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(c)
        i += 1
    if cur:
        out.append("".join(cur))
    return out


__all__ = ["Library", "GameState", "BNET_UID", "BNET_HELPERS"]
