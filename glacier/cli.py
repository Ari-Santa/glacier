"""Command line interface."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import APP_NAME, __version__, desktop, paths, proton, stats
from .core import Core
from .download import Cancelled, describe_error
from .engine import EngineError
from .library import NotRegisteredError


def _progress():
    state = {"key": None, "t": 0.0}
    tty = sys.stderr.isatty()

    def report(msg: str, frac: float) -> None:
        now = time.monotonic()
        if tty:
            bar = ""
            if frac >= 0:
                n = int(frac * 30)
                bar = f" [{'#' * n}{'.' * (30 - n)}] {frac * 100:5.1f}%"
            sys.stderr.write(f"\r\033[K{msg}{bar}")
            sys.stderr.flush()
            if frac >= 1.0:
                sys.stderr.write("\n")
            return
        # Without a terminal (a log file, a service), print each step once and its progress at
        # most every 10 s. Download messages carry a running MiB count, so they differ by digits.
        key = re.sub(r"\d+", "#", msg)
        if key != state["key"] or frac >= 1.0 or now - state["t"] > 10:
            pct = f" ({frac * 100:.0f}%)" if frac >= 0 else ""
            print(f"{msg}{pct}", file=sys.stderr, flush=True)
            state["t"] = now
        state["key"] = key

    return report


def _warn_vulkan32(core: Core) -> None:
    if proton.vulkan32_available(env=core.engine.vulkan_env()) is False:
        print(f"Warning: no 32-bit Vulkan driver found. {proton.VULKAN32_HINT}", file=sys.stderr)


def _done_line():
    if sys.stderr.isatty():
        sys.stderr.write("\n")


def cmd_setup(core: Core, a) -> int:
    if a.prefix:
        core.cfg.update(prefix=a.prefix)
    if a.library:
        core.cfg.update(library_dir=a.library)
    core.setup(_progress())
    _done_line()
    print(f"Battle.net installed in {core.engine.pfx}. Log in in the Battle.net window to finish.")
    _warn_vulkan32(core)
    _after_launch(a)
    return 0


def cmd_status(core: Core, a) -> int:
    lib = core.library
    lib.scan()
    if a.check_updates:
        lib.check_updates()
    r = core.engine.runner()
    rt = core.engine.runtime(r) if r else None
    st = core.bnet.status(check_latest=a.check_updates)
    play = stats.load()
    vk32 = proton.vulkan32_available(env=core.engine.vulkan_env())
    if a.json:
        out = {
            "runner": r.name if r else None, "runtime": rt.name if rt else None,
            "vulkan32": vk32,
            "prefix": str(core.engine.pfx), "battlenet": st.__dict__,
            "games": {u: {"name": s.game.name, "installed": s.installed, "version": s.version,
                          "latest": s.latest, "running": s.running, "progress": s.progress,
                          "path": str(s.install_dir) if s.install_dir else None,
                          "exe": str(s.exe) if s.exe else None,
                          "playtime": play.get(u, {}).get("playtime", 0)}
                      for u, s in lib.states.items() if s.installed or a.all},
        }
        print(json.dumps(out, indent=2))
        return 0
    from . import selfupdate
    newer = selfupdate.available()
    print(f"{APP_NAME} {__version__}" + (f"  (Glacier {newer.version} is available: glacier self-update)"
                                         if newer else ""))
    print(f"  Proton   : {r.label if r else 'none found'}")
    print(f"  Runtime  : {rt.name if rt else ('disabled' if not core.cfg['use_runtime'] else 'n/a')}")
    print(f"  Vulkan   : 32-bit driver {({True: 'found', False: 'MISSING', None: 'unknown'})[vk32]}")
    print(f"  Prefix   : {core.engine.pfx} ({'ok' if core.engine.prefix_exists() else 'not created'})")
    bn = "not installed"
    if st.installed:
        bn = f"{st.version or '?'}" + (f" (latest {st.latest})" if st.latest and st.latest != st.version else "")
        bn += ", running" if st.running else ""
    print(f"  Battle.net: {bn}")
    if vk32 is False:
        print(f"  {proton.VULKAN32_HINT}")
    rows = [s for s in lib.states.values() if s.installed or a.all]
    if not rows:
        print("  No games installed yet.")
    for s in sorted(rows, key=lambda s: s.game.name):
        pt = play.get(s.uid, {}).get("playtime", 0)
        extra = f"  [{stats.fmt_playtime(pt)}]" if pt else ""
        ver = s.version + (f" → {s.latest}" if s.update_available else "")
        print(f"  {s.uid:<16} {s.game.name:<34} {s.status_text:<18} {ver}{extra}")
    return 0


def cmd_list(core: Core, a) -> int:
    core.library.scan()
    for s in sorted(core.library.states.values(), key=lambda s: (not s.installed, s.game.name)):
        mark = "*" if s.installed else " "
        print(f"{mark} {s.uid:<16} {s.game.launch:<6} {s.game.name}")
    return 0


def _after_launch(a) -> None:
    if not getattr(a, "no_watch", False):
        from .watcher import spawn_detached
        spawn_detached()


def cmd_launch(core: Core, a) -> int:
    if not core.bnet.installed():
        print("Battle.net is not installed; running setup first…", file=sys.stderr)
        core.setup(_progress())
    try:
        proc, log = core.library.launch(a.game)
    except NotRegisteredError as e:
        # Usually started from the app menu with no terminal: show the game page and say why.
        print(e, file=sys.stderr)
        core.library.open_in_bnet(a.game)
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Glacier", "-i", desktop.icon_name(core.library.get(a.game).uid),
                            "Add the game to Battle.net first", str(e)], check=False)
        return 1
    print(f"Launching {core.library.get(a.game).game.name} (log: {log})")
    _after_launch(a)
    return 0


def cmd_bnet(core: Core, a) -> int:
    if not core.bnet.installed():
        core.setup(_progress())
        _warn_vulkan32(core)
        _after_launch(a)
        return 0
    args = ["--runonstartup"] if a.background else []
    proc, log = core.bnet.launch(args)
    print(f"Starting Battle.net (log: {log})")
    _warn_vulkan32(core)
    _after_launch(a)
    return 0


def cmd_open(core: Core, a) -> int:
    core.library.open_in_bnet(a.game)
    _after_launch(a)
    return 0


def cmd_install(core: Core, a) -> int:
    core.library.install(a.game)
    print("Battle.net will show its install dialog; progress appears in `glacier status`.")
    _after_launch(a)
    return 0


def cmd_repair(core: Core, a) -> int:
    core.library.repair(a.game)
    _after_launch(a)
    return 0


def cmd_uninstall(core: Core, a) -> int:
    core.library.uninstall(a.game)
    return 0


def cmd_kill(core: Core, a) -> int:
    core.engine.kill_all()
    print("Stopped everything in the Battle.net prefix.")
    return 0


def cmd_watch(core: Core, a) -> int:
    from .watcher import run_daemon
    return run_daemon(core, idle_exit=a.idle)


def cmd_menu(core: Core, a) -> int:
    core.library.scan()
    if a.remove:
        core.library.remove_menu_entries()
        print("Removed Glacier app-menu entries.")
        return 0
    changed = core.library.sync_menu_entries()
    print("Menu entries up to date." + (f" Changed: {', '.join(changed)}" if changed else ""))
    return 0


def cmd_icons(core: Core, a) -> int:
    core.library.scan()
    for s in core.library.states.values():
        if s.installed:
            p = core.library.ensure_icon(s, force=a.force)
            print(f"{s.uid:<16} {p}")
    desktop.refresh()
    return 0


def cmd_run(core: Core, a) -> int:
    exe = a.exe
    spec = core.engine.build(Path(exe).resolve() if Path(exe).exists() else exe, a.args,
                             verb="run", log_name="run")
    proc, log = core.engine.spawn(spec)
    print(f"Started (log: {log})")
    if a.wait:
        return proc.wait()
    return 0


def cmd_tool(core: Core, a) -> int:
    spec = core.engine.build(a.tool, [], verb="run", log_name=a.tool, wrap=False)
    core.engine.spawn(spec)
    return 0


def cmd_proton(core: Core, a) -> int:
    eng = core.engine
    if a.action == "list":
        current = eng.runner()
        for r in eng.runners(refresh=True):
            rt = proton.find_runtime(r.runtime_appid)
            mark = "*" if current and r.id == current.id else " "
            need = f" needs runtime {r.runtime_appid}" if r.runtime_appid and not rt else ""
            print(f"{mark} {r.id:<40} {r.version:<36}{need}")
        return 0
    if a.action == "available":
        have = proton.installed_ge_tags()
        for rel in proton.list_ge_releases(10):
            mark = "installed" if rel.tag in have else ""
            print(f"{mark:<9} {rel.tag:<18} {rel.published}  {rel.size >> 20} MiB")
        return 0
    if a.action == "install":
        rels = proton.list_ge_releases(15)
        tag = a.target or "latest"
        rel = rels[0] if tag == "latest" else next((r for r in rels if r.tag == tag), None)
        if rel is None:
            print(f"No GE-Proton release named {tag}", file=sys.stderr)
            return 1
        r = core.install_ge(rel, select=not a.no_select, progress=_progress())
        _done_line()
        print(f"Installed {r.name}" + ("" if a.no_select else " and selected it"))
        return 0
    if a.action == "update":
        r = core.update_ge(_progress())
        _done_line()
        print(f"Installed {r.name}" if r else "GE-Proton is up to date.")
        return 0
    if a.action == "use":
        match = proton.match_runner(eng.runners(refresh=True), a.target or "")
        if not match:
            print(f"No Proton build matches {a.target}", file=sys.stderr)
            return 1
        core.cfg.update(runner=match.id)
        core.ensure_runner(_progress())
        print(f"Using {match.name}")
        return 0
    if a.action == "remove":
        match = proton.match_runner(eng.runners(refresh=True), a.target or "")
        if not match:
            print(f"No Proton build matches {a.target}", file=sys.stderr)
            return 1
        proton.remove_runner(match)
        print(f"Removed {match.name}")
        return 0
    return 1


def _print_sync(res) -> None:
    for line in res.lines():
        print(line)


def cmd_curseforge(core: Core, a) -> int:
    from . import curseforge
    cf = curseforge.CurseForge()
    if a.action in ("install", "update"):
        if a.action == "update" and not cf.installed():
            print("CurseForge is not installed; use `glacier curseforge install`.", file=sys.stderr)
            return 1
        result = cf.install(_progress(), force=a.force)
        _done_line()
        print(result.message)
        if result.changed:
            print(f"  AppImage : {cf.appimage}")
            print(f"  Menu     : {paths.APPLICATIONS_DIR / curseforge.DESKTOP_ID}")
        _print_sync(curseforge.sync_games(core.bnet, core.engine))
        return 0
    if a.action == "sync":
        res = curseforge.sync_games(core.bnet, core.engine)
        _print_sync(res)
        if not res.lines():
            print("CurseForge's WoW list already matches Battle.net.")
        return 0
    if a.action == "launch":
        _print_sync(curseforge.sync_games(core.bnet, core.engine))
        proc, log = cf.launch()
        print(f"Starting CurseForge (log: {log})", flush=True)
        try:
            code = proc.wait(timeout=5)     # one that can't start (a missing library) exits at once
        except subprocess.TimeoutExpired:
            return 0
        if code:
            print(f"error: {curseforge.start_failure(code, log)}", file=sys.stderr)
            return 1
        return 0
    if a.action == "remove":
        if not cf.installed():
            print("CurseForge is not installed.")
            return 0
        cf.remove()
        print("Removed CurseForge. Your addons and CurseForge's own settings are kept.")
        return 0
    # status
    if not cf.installed():
        print(f"CurseForge is not installed (`glacier curseforge install` gets it from {curseforge.PAGE_URL}).")
        return 0
    print(f"CurseForge {cf.version() or '?'}: {cf.appimage}")
    if a.check_updates:
        print("  An update is available (`glacier curseforge update`)." if cf.update_available()
              else "  Up to date.")
    return 0


def cmd_self_update(core: Core, a) -> int:
    from . import selfupdate
    rel = selfupdate.check(force=True)
    if rel is None:
        latest = selfupdate.cached_latest()
        print(f"{APP_NAME} {__version__} is up to date." + ("" if latest else " (No release published yet.)"))
        return 0
    print(f"{APP_NAME} {rel.version} is available (you have {__version__}), released {rel.published}: {rel.url}")
    if rel.summary:
        print(f"  {rel.summary}")
    if a.check:
        return 0
    ok, why = selfupdate.can_self_update()
    if not ok:
        print(why, file=sys.stderr)
        return 1
    version = selfupdate.install(rel, _progress())
    _done_line()
    print(f"Installed {APP_NAME} {version}. Restart Glacier (the window and tray) to use it.")
    return 0


def cmd_config(core: Core, a) -> int:
    if a.key is None:
        print(json.dumps(core.cfg.data, indent=2, sort_keys=True))
        return 0
    if a.value is None:
        print(json.dumps(core.cfg[a.key]))
        return 0
    try:
        value = json.loads(a.value)
    except ValueError:
        value = a.value
    core.cfg.update(**{a.key: value})
    print(f"{a.key} = {json.dumps(value)}")
    return 0


def cmd_logs(core: Core, a) -> int:
    latest = paths.LOG_DIR / "latest.log"
    if a.path:
        print(latest.resolve() if latest.exists() else paths.LOG_DIR)
        return 0
    try:
        lines = latest.read_text(errors="replace").splitlines()
    except OSError:
        print("No logs yet.")
        return 0
    print("\n".join(lines[-a.lines:]))
    return 0


def cmd_gui(core: Core | None, a) -> int:
    from .gui.app import main as gui_main
    return gui_main(sys.argv[:1] + (["--hidden"] if getattr(a, "hidden", False) else []))


def cmd_integrate(core: Core, a) -> int:
    """Install Glacier's own menu entry/icon and refresh game entries."""
    if a.remove:
        core.library.scan()
        core.library.remove_menu_entries()
        desktop.remove_entry("glacier.desktop")
        desktop.set_autostart(False)
        for s in desktop.ICON_SIZES + (512,):
            (paths.HICOLOR_DIR / f"{s}x{s}" / "apps" / "glacier.png").unlink(missing_ok=True)
        desktop.refresh()
        print("Removed Glacier from the app menu.")
        return 0
    desktop.install_app_entry()
    core.library.scan()
    core.library.sync_menu_entries()
    desktop.refresh()
    print("Glacier is in your app menu.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="glacier", description="Battle.net launcher for Linux (Proton).")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("gui", help="open the launcher window (default)")
    s.add_argument("--hidden", action="store_true", help="start in the tray")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("integrate", help="add Glacier and installed games to the app menu")
    s.add_argument("--remove", action="store_true", help="remove every menu entry Glacier created")
    s.set_defaults(func=cmd_integrate)

    s = sub.add_parser("setup", help="install Proton runtime, prefix and Battle.net, unattended")
    s.add_argument("--prefix", help="where to create the Wine prefix")
    s.add_argument("--library", help="host folder to expose as the game drive (D:)")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("status", help="show Proton, Battle.net and game status")
    s.add_argument("-u", "--check-updates", action="store_true", help="query Blizzard for new versions")
    s.add_argument("-a", "--all", action="store_true", help="include games that are not installed")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list known games and their ids")
    s.set_defaults(func=cmd_list)

    for name, fn, hlp in (("launch", cmd_launch, "play a game"),
                          ("open", cmd_open, "show a game in Battle.net"),
                          ("install", cmd_install, "install a game through Battle.net"),
                          ("repair", cmd_repair, "repair a game through Battle.net"),
                          ("uninstall", cmd_uninstall, "run a game's uninstaller")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("game", help="game id, launch code or name (see `glacier list`)")
        s.add_argument("--no-watch", action="store_true", help="do not start the window/playtime watcher")
        s.set_defaults(func=fn)

    s = sub.add_parser("bnet", help="start Battle.net")
    s.add_argument("--background", action="store_true", help="start minimised to the tray")
    s.add_argument("--no-watch", action="store_true")
    s.set_defaults(func=cmd_bnet)

    s = sub.add_parser("kill", help="stop Battle.net and all games")
    s.set_defaults(func=cmd_kill)

    s = sub.add_parser("watch", help="(internal) tag game windows and track playtime until idle")
    s.add_argument("--idle", type=float, default=30.0)
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("menu", help="create/refresh app-menu entries for installed games")
    s.add_argument("--remove", action="store_true")
    s.set_defaults(func=cmd_menu)

    s = sub.add_parser("icons", help="extract game icons from their executables")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_icons)

    s = sub.add_parser("run", help="run any Windows program in the Battle.net prefix")
    s.add_argument("exe")
    s.add_argument("args", nargs=argparse.REMAINDER)
    s.add_argument("--wait", action="store_true")
    s.set_defaults(func=cmd_run)

    for tool in ("winecfg", "regedit", "taskmgr", "explorer"):
        s = sub.add_parser(tool, help=f"open {tool} in the prefix")
        s.set_defaults(func=cmd_tool, tool=tool)

    s = sub.add_parser("proton", help="manage Proton builds")
    s.add_argument("action", choices=["list", "available", "install", "update", "use", "remove"])
    s.add_argument("target", nargs="?", help="GE-Proton tag (install), runner id/name (use/remove)")
    s.add_argument("--no-select", action="store_true", help="install without switching to it")
    s.set_defaults(func=cmd_proton)

    s = sub.add_parser("curseforge", help="install, update or open CurseForge (WoW addon manager)")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "install", "update", "launch", "sync", "remove"],
                   help="sync: make CurseForge's WoW folders match what Battle.net has installed")
    s.add_argument("--force", action="store_true", help="(install) download again even if up to date")
    s.add_argument("-u", "--check-updates", action="store_true", help="(status) check for a newer download")
    s.set_defaults(func=cmd_curseforge)

    s = sub.add_parser("self-update", help="update Glacier itself from its GitHub releases")
    s.add_argument("--check", action="store_true", help="only report whether an update is available")
    s.set_defaults(func=cmd_self_update)

    s = sub.add_parser("config", help="show or change settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?", help="JSON value (true/false/numbers/strings)")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("logs", help="show the most recent log")
    s.add_argument("-n", "--lines", type=int, default=60)
    s.add_argument("--path", action="store_true")
    s.set_defaults(func=cmd_logs)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.cmd is None or a.cmd == "gui":
        return cmd_gui(None, a)
    core = Core()
    try:
        return a.func(core, a)
    except (EngineError, RuntimeError, OSError) as e:
        _done_line()
        print(f"error: {describe_error(e)}", file=sys.stderr)
        return 1
    except Cancelled:
        return 130
    except KeyboardInterrupt:
        _done_line()
        return 130
