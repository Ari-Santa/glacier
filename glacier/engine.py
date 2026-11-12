"""Proton execution engine: prefix, environment, commands and process tracking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from . import paths, proton
from .config import Config

ProgressFn = Callable[[str, float], None]


class EngineError(RuntimeError):
    pass


def split_args(text: str | None, what: str) -> list[str]:
    """Split a command line from the settings; unbalanced quotes become a readable error."""
    try:
        return shlex.split(text or "")
    except ValueError as e:
        raise EngineError(f"{what} can't be read ({e}). Check the quotes.") from None


@dataclass
class WineProc:
    pid: int
    exe: str            # lower-case basename of the Windows executable
    argv: list[str]

    @property
    def is_subprocess(self) -> bool:
        """CEF/Chromium helper processes (Battle.net spawns many)."""
        return any(a.startswith("--type=") for a in self.argv[1:])


@dataclass
class LaunchSpec:
    cmd: list[str]
    env: dict[str, str]
    cwd: str
    fingerprint: str
    log_name: str


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._runners: list[proton.Runner] | None = None

    # ------------------------------------------------------------------ runners
    def runners(self, refresh: bool = False) -> list[proton.Runner]:
        if self._runners is None or refresh:
            self._runners = proton.find_runners()
        return self._runners

    def runner(self) -> proton.Runner | None:
        runners = self.runners()
        wanted = self.cfg["runner"]
        if wanted:
            for r in runners:
                if r.id == wanted:
                    return r
            # Same directory name from another source (e.g. moved library)
            for r in runners:
                if r.id.split(":", 1)[-1] == wanted.split(":", 1)[-1]:
                    return r
        return proton.default_runner(runners)

    def runtime(self, runner: proton.Runner | None = None) -> proton.Runtime | None:
        runner = runner or self.runner()
        if runner is None or not self.cfg["use_runtime"]:
            return None
        return proton.find_runtime(runner.runtime_appid)

    def require_runner(self) -> proton.Runner:
        r = self.runner()
        if r is None:
            raise EngineError("No Proton build found. Install Proton through Steam or download "
                              "GE-Proton from the Proton page.")
        if not r.proton.is_file():
            raise EngineError(f"Proton build is broken: {r.proton} is missing")
        return r

    def runtime_missing(self) -> str | None:
        """App id of a runtime the selected runner needs but which is not installed."""
        r = self.runner()
        if r and self.cfg["use_runtime"] and r.runtime_appid and self.runtime(r) is None:
            return r.runtime_appid
        return None

    # ------------------------------------------------------------------ prefix
    @property
    def compat_dir(self) -> Path:
        return self.cfg.prefix

    @property
    def pfx(self) -> Path:
        return self.compat_dir / "pfx"

    @property
    def drive_c(self) -> Path:
        return self.pfx / "drive_c"

    def prefix_exists(self) -> bool:
        return (self.pfx / "system.reg").is_file()

    def prefix_version(self) -> str:
        try:
            return (self.compat_dir / "version").read_text().strip()
        except OSError:
            return ""

    def _drive_target(self, letter: str) -> Path | None:
        link = self.pfx / "dosdevices" / f"{letter.lower()}:"
        try:
            if link.is_symlink():
                return (link.parent / os.readlink(link)).resolve()
        except OSError:
            return None
        if letter.lower() == "c":
            return self.drive_c
        return None

    def win_to_unix(self, wpath: str) -> Path | None:
        p = wpath.strip().replace("\\", "/")
        m = re.match(r"^([A-Za-z]):/*(.*)$", p)
        if not m:
            return None
        base = self._drive_target(m.group(1))
        if base is None:
            return None
        rest = [part for part in m.group(2).split("/") if part]
        cur = base
        for part in rest:            # Windows paths are case-insensitive
            nxt = cur / part
            if not nxt.exists() and cur.is_dir():
                low = part.lower()
                try:
                    match = next((c for c in cur.iterdir() if c.name.lower() == low), None)
                except OSError:
                    match = None
                if match is not None:
                    nxt = match
            cur = nxt
        return cur

    def unix_to_win(self, path: Path) -> str:
        path = Path(path).resolve()
        try:
            rel = path.relative_to(self.drive_c.resolve())
            return "C:\\" + str(rel).replace("/", "\\") if str(rel) != "." else "C:\\"
        except ValueError:
            pass
        lib = self.library_dir()
        if lib:
            try:
                rel = path.relative_to(lib.resolve())
                letter = self.cfg["library_drive"].upper()
                return f"{letter}:\\" + ("" if str(rel) == "." else str(rel).replace("/", "\\"))
            except ValueError:
                pass
        return "Z:" + str(path).replace("/", "\\")

    def library_dir(self) -> Path | None:
        d = (self.cfg["library_dir"] or "").strip()
        return Path(os.path.expanduser(d)) if d else None

    def map_library_drive(self) -> None:
        lib = self.library_dir()
        letter = (self.cfg["library_drive"] or "d").lower()[:1]
        if letter in ("c", "z"):
            raise EngineError("Drive C: and Z: are reserved by Proton")
        dd = self.pfx / "dosdevices"
        if not dd.is_dir():
            return
        link = dd / f"{letter}:"
        if lib is None:
            return
        lib.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or not link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == lib:
                return
            if link.is_symlink():
                link.unlink()
            os.symlink(lib, link)

    # ------------------------------------------------------------------ environment
    def base_env(self, runner: proton.Runner, runtime: proton.Runtime | None,
                 install_dir: Path | None = None) -> dict[str, str]:
        paths.ensure_dirs()
        env = dict(os.environ)
        # Do not leak a user's own wine setup into Proton.
        for k in ("WINEPREFIX", "WINE", "WINELOADER", "WINESERVER", "WINEDLLPATH", "WINEARCH"):
            env.pop(k, None)
        compat = self.compat_dir
        compat.mkdir(parents=True, exist_ok=True)
        tool_paths = [str(runner.path)] + ([str(runtime.path)] if runtime else [])
        lib = self.library_dir()
        # Only the real game library goes into LIBRARY_PATHS: Proton maps the library that contains
        # STEAM_COMPAT_INSTALL_PATH to drive S:, which must never point back into the prefix.
        library_paths = [str(lib)] if lib else []
        shared = [str(compat)] + library_paths + ([str(install_dir)] if install_dir else [])
        env.update({
            "STEAM_COMPAT_DATA_PATH": str(compat),
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(paths.STEAM_STUB_DIR),
            "STEAM_COMPAT_TOOL_PATHS": ":".join(tool_paths),
            "STEAM_COMPAT_LIBRARY_PATHS": ":".join(library_paths),
            "STEAM_COMPAT_SHADER_PATH": str(compat / "shadercache"),
            "STEAM_COMPAT_APP_ID": "0",
            "SteamAppId": "0",
            "SteamGameId": "0",
            "PROTON_LOG_DIR": str(paths.LOG_DIR),
        })
        if install_dir:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(install_dir)
        else:
            env.pop("STEAM_COMPAT_INSTALL_PATH", None)
        outside_home = [p for p in shared + tool_paths if not p.startswith(str(paths.HOME) + "/")]
        if outside_home:
            existing = env.get("PRESSURE_VESSEL_FILESYSTEMS_RW", "")
            env["PRESSURE_VESSEL_FILESYSTEMS_RW"] = ":".join(
                dict.fromkeys([p for p in existing.split(":") if p] + outside_home))
        if runner.is_ge:
            env.setdefault("UMU_ID", "umu-battlenet")
            env.setdefault("STORE", "battlenet")
        return env

    def option_env(self, uid: str | None) -> dict[str, str]:
        """Environment variables from launch options (global + per-game)."""
        c = self.cfg
        env: dict[str, str] = {}
        if c.effective(uid, "mangohud"):
            env["MANGOHUD"] = "1"
        if c["dxvk_hud"]:
            env["DXVK_HUD"] = "devinfo,fps,frametimes,gpuload"
        if c["wayland"]:
            env["PROTON_ENABLE_WAYLAND"] = "1"
        if c["hdr"]:
            env["PROTON_ENABLE_HDR"] = "1"
            env["DXVK_HDR"] = "1"
        if c["disable_fsync"]:
            env["PROTON_NO_FSYNC"] = "1"
        if c["disable_esync"]:
            env["PROTON_NO_ESYNC"] = "1"
        if c["proton_log"]:
            env["PROTON_LOG"] = "1"
        for k, v in (c["env"] or {}).items():
            env[str(k)] = str(v)
        if uid:
            for k, v in (c.game(uid).get("env") or {}).items():
                env[str(k)] = str(v)
        return env

    def vulkan_env(self) -> dict[str, str]:
        """What Battle.net's Vulkan loader sees: Glacier's environment plus the user's settings."""
        return {**os.environ, **self.option_env(None)}

    def wrappers(self, uid: str | None) -> list[str]:
        c = self.cfg
        out: list[str] = []
        if c.effective(uid, "gamemode") and shutil.which("gamemoderun"):
            out.append("gamemoderun")
        if c.effective(uid, "gamescope") and shutil.which("gamescope"):
            out += ["gamescope", *split_args(c["gamescope_args"], "The Gamescope arguments"), "--"]
        if c["wrapper"]:
            out += split_args(c["wrapper"], "The wrapper command")
        return out

    def fingerprint(self, uid: str | None) -> str:
        r = self.runner()
        blob = json.dumps({"runner": r.id if r else None, "env": self.option_env(uid),
                           "wrap": self.wrappers(uid), "rt": self.cfg["use_runtime"]}, sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    def build(self, exe: Path | str, args: list[str] | None = None, *, uid: str | None = None,
              verb: str = "auto", install_dir: Path | None = None,
              log_name: str = "run", wrap: bool = True) -> LaunchSpec:
        """Build a Proton command line.

        verb "auto" picks `run` when the prefix is already active (so we join the running
        wineserver instead of waiting for it to exit) and `waitforexitandrun` otherwise.
        """
        if verb == "auto":
            verb = "run" if self.wineserver_running() else "waitforexitandrun"
        runner = self.require_runner()
        runtime = self.runtime(runner)
        if self.cfg["use_runtime"] and runner.runtime_appid and runtime is None:
            raise EngineError(f"{runner.name} needs the Steam Linux Runtime (app {runner.runtime_appid}). "
                              "Install it from the Proton page or through Steam.")
        env = self.base_env(runner, runtime, install_dir)
        env.update(self.option_env(uid))
        core = [str(runner.proton), verb, str(exe), *(args or [])]
        if runtime:
            core = [str(runtime.entry_point), f"--verb={verb}", "--", *core]
        cmd = (self.wrappers(uid) if wrap else []) + core
        exe_path = Path(exe)
        cwd = str(exe_path.parent) if exe_path.is_absolute() and exe_path.parent.is_dir() else str(self.compat_dir)
        return LaunchSpec(cmd, env, cwd, self.fingerprint(uid), log_name)

    # ------------------------------------------------------------------ running
    def _log_file(self, name: str):
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        logs = sorted(paths.LOG_DIR.glob(f"{safe}-*.log"))
        for old in logs[:-9]:
            old.unlink(missing_ok=True)
        path = paths.LOG_DIR / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        fh = open(path, "ab", buffering=0)
        latest = paths.LOG_DIR / "latest.log"
        try:
            latest.unlink(missing_ok=True)
            latest.symlink_to(path.name)
        except OSError:
            pass
        return fh, path

    def spawn(self, spec: LaunchSpec) -> tuple[subprocess.Popen, Path]:
        fh, path = self._log_file(spec.log_name)
        header = f"# {time.ctime()}\n# cwd: {spec.cwd}\n# cmd: {shlex.join(spec.cmd)}\n"
        fh.write(header.encode())
        proc = subprocess.Popen(spec.cmd, env=spec.env, cwd=spec.cwd, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        fh.close()
        return proc, path

    def run_wait(self, spec: LaunchSpec, timeout: float = 600) -> int:
        proc, _ = self.spawn(spec)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise EngineError(f"{spec.log_name} did not finish within {int(timeout)}s") from None

    # ------------------------------------------------------------------ processes
    def processes(self) -> list[WineProc]:
        """Wine processes that belong to this prefix."""
        want = str(self.pfx.resolve()) if self.pfx.exists() else str(self.pfx)
        found = []
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                raw = Path(f"/proc/{name}/cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            argv = raw.rstrip(b"\0").split(b"\0")
            a0 = argv[0].decode("utf-8", "replace")
            base = re.split(r"[\\/]", a0)[-1].lower()
            if not base.endswith(".exe"):
                # Wine may keep the exe in argv[1] behind the loader
                if len(argv) > 1 and base in ("wine", "wine64", "wine-preloader", "wine64-preloader"):
                    a0 = argv[1].decode("utf-8", "replace")
                    base = re.split(r"[\\/]", a0)[-1].lower()
                    argv = argv[1:]
                if not base.endswith(".exe"):
                    continue
            try:
                environ = Path(f"/proc/{name}/environ").read_bytes()
            except OSError:
                continue
            prefix = None
            for item in environ.split(b"\0"):
                if item.startswith(b"WINEPREFIX="):
                    prefix = item[11:].decode("utf-8", "replace").rstrip("/")
                    break
            if prefix is None:
                continue
            try:
                same = prefix == want or os.path.realpath(prefix) == want
            except OSError:
                same = False
            if same:
                found.append(WineProc(int(name), base, [a.decode("utf-8", "replace") for a in argv]))
        return found

    def wineserver_running(self) -> bool:
        want = str(self.pfx.resolve()) if self.pfx.exists() else str(self.pfx)
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                comm = Path(f"/proc/{name}/comm").read_text().strip()
                if comm != "wineserver":
                    continue
                environ = Path(f"/proc/{name}/environ").read_bytes()
            except OSError:
                continue
            for item in environ.split(b"\0"):
                if item.startswith(b"WINEPREFIX="):
                    if os.path.realpath(item[11:].decode("utf-8", "replace")) == want:
                        return True
        return False

    def kill_all(self, timeout: float = 10) -> None:
        """Stop everything running in the prefix (Battle.net, games, wineserver)."""
        runner = self.runner()
        if runner and runner.wineserver.is_file():
            env = dict(os.environ, WINEPREFIX=str(self.pfx))
            try:
                subprocess.run([str(runner.wineserver), "-k"], env=env, timeout=timeout,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass
        deadline = time.time() + timeout
        for sig in (signal.SIGTERM, signal.SIGKILL):
            procs = self.processes()
            if not procs and not self.wineserver_running():
                return
            for p in procs:
                try:
                    os.kill(p.pid, sig)
                except OSError:
                    pass
            while time.time() < deadline and self.processes():
                time.sleep(0.3)

    # ------------------------------------------------------------------ prefix setup
    def ensure_prefix(self, progress: ProgressFn | None = None) -> None:
        runner = self.require_runner()
        # Proton upgrades an existing prefix by itself on every launch, so only a missing
        # prefix needs an explicit wineboot here.
        if not self.prefix_exists():
            if progress:
                progress(f"Creating Wine prefix with {runner.name}", -1)
            spec = self.build("wineboot", ["-u"], verb="run", log_name="prefix-setup", wrap=False)
            rc = self.run_wait(spec, timeout=900)
            if not self.prefix_exists():
                raise EngineError(f"Prefix creation failed (exit {rc}); see logs in {paths.LOG_DIR}")
        self.map_library_drive()
        if self.cfg["dll_tweaks"]:
            self.apply_tweaks(progress)

    def reg_add(self, key: str, name: str, value: str, rtype: str = "REG_SZ") -> int:
        spec = self.build("reg", ["add", key, "/v", name, "/t", rtype, "/d", value, "/f"],
                          verb="runinprefix", log_name="reg", wrap=False)
        return self.run_wait(spec, timeout=120)

    def apply_tweaks(self, progress: ProgressFn | None = None) -> None:
        marker = self.compat_dir / ".glacier-tweaks"
        version = "1"
        try:
            if marker.read_text().strip() == version:
                return
        except OSError:
            pass
        if progress:
            progress("Applying Battle.net prefix tweaks", -1)
        # Battle.net probes the Windows Location API; Wine's stub can crash or hang it.
        rc = self.reg_add(r"HKCU\Software\Wine\DllOverrides", "locationapi", "")
        if rc == 0:         # otherwise try again on the next start
            marker.write_text(version)
