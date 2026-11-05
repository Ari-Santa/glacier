"""Battle.net client: unattended install, detection, launching, config tweaks."""

from __future__ import annotations

import json
import locale
import os
import re
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import blizzard, download, paths, pe
from .engine import Engine, EngineError, ProgressFn
from .games import BATTLENET_DIR, BATTLENET_EXE

INSTALLER_URL = ("https://downloader.battle.net/download/getInstallerForGame"
                 "?os=win&gameProgram=BATTLENET_APP&version=Live")
TRUSTED_HOSTS = ("battle.net", "blizzard.com", "blizzard.cn", "battlenet.com.cn")

LOCALES = {"enUS", "enGB", "esMX", "esES", "ptBR", "deDE", "frFR", "itIT", "plPL", "ruRU",
           "koKR", "zhTW", "zhCN", "jaJP", "thTH", "trTR"}
LANG_DEFAULT = {"en": "enUS", "es": "esES", "pt": "ptBR", "de": "deDE", "fr": "frFR", "it": "itIT",
                "pl": "plPL", "ru": "ruRU", "ko": "koKR", "zh": "zhCN", "ja": "jaJP", "th": "thTH",
                "tr": "trTR"}

# Wine/Proton system processes that do not count as "something is running".
SYSTEM_EXES = {"services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe", "explorer.exe",
               "rpcss.exe", "tabtip.exe", "xalia.exe", "conhost.exe", "steam.exe", "start.exe",
               "wineboot.exe", "winemenubuilder.exe", "rundll32.exe", "cmd.exe", "reg.exe"}
BNET_HELPERS = {"battle.net.exe", "agent.exe", "agenthelper.exe", "battle.net launcher.exe",
                "blizzarderror.exe", "battle.net-setup.exe", "blizzard uninstaller.exe",
                "battle.net helper.exe", "blizzardbrowser.exe"}


def detect_language() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v and v not in ("C", "POSIX") and not v.startswith("C."):
            break
    else:
        v = (locale.getlocale()[0] or "en_US")
    m = re.match(r"([a-z]{2})(?:_([A-Z]{2}))?", v)
    if not m:
        return "enUS"
    code = m.group(1) + (m.group(2) or "")
    if code in LOCALES:
        return code
    return LANG_DEFAULT.get(m.group(1), "enUS")


@dataclass
class BnetStatus:
    installed: bool
    version: str
    latest: str
    running: bool
    logged_in_hint: bool
    agent_version: str


class BattleNet:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.cfg = engine.cfg

    # ------------------------------------------------------------------ paths
    @property
    def install_dir(self) -> Path:
        return self.engine.drive_c / BATTLENET_DIR

    @property
    def exe(self) -> Path:
        return self.install_dir / BATTLENET_EXE

    @property
    def product_db(self) -> Path:
        return self.engine.drive_c / "ProgramData" / "Battle.net" / "Agent" / "product.db"

    @property
    def config_file(self) -> Path:
        return self.engine.drive_c / "users" / "steamuser" / "AppData" / "Roaming" / "Battle.net" / "Battle.net.config"

    def installed(self) -> bool:
        return self.exe.is_file()

    def products(self) -> list[blizzard.ProductInstall]:
        try:
            return blizzard.read_product_db(self.product_db)
        except (OSError, ValueError):
            return []

    def version(self) -> str:
        for p in self.products():
            if p.product_code == "bna" and p.version:
                return p.version
        return pe.version_info(self.exe).get("FileVersion", "") if self.installed() else ""

    def region(self) -> str:
        if self.cfg["region"]:
            return self.cfg["region"].lower()
        for p in self.products():
            if p.product_code in ("bna", "agent") and p.play_region:
                return p.play_region.lower()
        return "us"

    def language(self) -> str:
        lang = self.cfg["language"]
        return lang if lang in LOCALES else detect_language()

    # ------------------------------------------------------------------ processes
    def main_process(self):
        for p in self.engine.processes():
            if p.exe == "battle.net.exe" and not p.is_subprocess:
                return p
        return None

    def running(self) -> bool:
        return self.main_process() is not None

    def busy_processes(self) -> list:
        """Non-system processes other than Battle.net itself (i.e. games)."""
        return [p for p in self.engine.processes()
                if p.exe not in SYSTEM_EXES and p.exe not in BNET_HELPERS and not p.is_subprocess]

    def logged_in_hint(self) -> bool:
        """Best effort: the client remembers an account once someone has logged in."""
        try:
            data = json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            return False
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("Client"), dict):
                if v["Client"].get("SavedAccountNames") or v.get("Services", {}).get("LastCharacterInfo"):
                    return True
        return False

    def status(self, check_latest: bool = False) -> BnetStatus:
        latest = ""
        if check_latest:
            try:
                rv = blizzard.latest_for_region(blizzard.remote_versions("bna"), self.region())
                latest = rv.version if rv else ""
            except Exception:
                latest = ""
        agent = next((p.version for p in self.products() if p.product_code == "agent"), "")
        return BnetStatus(self.installed(), self.version(), latest, self.running(),
                          self.logged_in_hint(), agent)

    # ------------------------------------------------------------------ install
    def download_installer(self, progress: ProgressFn | None = None,
                           cancel: threading.Event | None = None) -> Path:
        paths.ensure_dirs()
        dest = paths.DOWNLOADS_DIR / "Battle.net-Setup.exe"
        dest.unlink(missing_ok=True)
        (dest.with_name(dest.name + ".part")).unlink(missing_ok=True)
        with download.session() as s:
            r = s.head(INSTALLER_URL, allow_redirects=True, timeout=20)
            r.raise_for_status()
            final = r.url
        host = urlparse(final).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS):
            raise EngineError(f"Installer redirect went to an unexpected host: {host}")

        def on_bytes(done: int, total: int) -> None:
            if progress:
                progress("Downloading Battle.net installer", done / total if total else -1)

        download.download(final, dest, on_bytes, cancel)
        info = pe.version_info(dest)
        if "Blizzard" not in info.get("CompanyName", ""):
            dest.unlink(missing_ok=True)
            raise EngineError("Downloaded installer is not a Blizzard executable")
        return dest

    def install(self, progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                timeout: float = 1800) -> None:
        """Download and run the official installer unattended (no clicks needed)."""
        self.engine.ensure_prefix(progress)
        setup = self.download_installer(progress, cancel)
        if progress:
            progress("Installing Battle.net", -1)
        args = [f"--lang={self.language()}", r"--installpath=C:\Program Files (x86)\Battle.net"]
        spec = self.engine.build(setup, args, verb="waitforexitandrun", log_name="battlenet-setup")
        proc, log = self.engine.spawn(spec)
        self._write_session(self.engine.fingerprint(None))
        start = time.time()
        seen_setup = False
        while time.time() - start < timeout:
            if cancel is not None and cancel.is_set():
                raise download.Cancelled()
            time.sleep(2)
            exes = {p.exe for p in self.engine.processes()}
            if "battle.net-setup.exe" in exes:
                seen_setup = True
            bna = next((p for p in self.products() if p.product_code == "bna"), None)
            if progress and bna and bna.progress:
                progress("Installing Battle.net", bna.progress)
            done = self.installed() and bna is not None and bna.installed and "battle.net-setup.exe" not in exes
            if done:
                break
            if proc.poll() is not None and not exes and (seen_setup or time.time() - start > 60):
                raise EngineError(f"Battle.net setup exited without installing; see {log}")
        else:
            raise EngineError("Timed out waiting for the Battle.net installer")
        self.apply_config()
        if progress:
            progress("Battle.net installed", 1.0)

    # ------------------------------------------------------------------ config
    def apply_config(self) -> None:
        """Push Glacier settings into Battle.net.config (only keys we own)."""
        f = self.config_file
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            if not f.parent.is_dir():
                return
            data = {}
        client = data.setdefault("Client", {})
        before = json.dumps(data, sort_keys=True)
        if self.cfg["bnet_hw_accel"]:
            client.pop("HardwareAcceleration", None)
        else:
            client["HardwareAcceleration"] = "false"
        # Point new installs at the game library drive unless the user picked a folder already.
        lib = self.engine.library_dir()
        letter = (self.cfg["library_drive"] or "d").upper()[:1]
        managed = {f"{c}:/" for c in "DEFGHIJKLMNOPQRSTUVWXY"}
        if lib and (not client.get("DefaultInstallPath") or client.get("DefaultInstallPath") in managed):
            client["DefaultInstallPath"] = f"{letter}:/"
        if json.dumps(data, sort_keys=True) == before:
            return
        tmp = f.with_suffix(".glacier-tmp")
        tmp.write_text(json.dumps(data, indent=4))
        os.replace(tmp, f)

    # ------------------------------------------------------------------ launching
    def _read_session(self) -> dict:
        try:
            return json.loads(paths.SESSION_FILE.read_text())
        except (OSError, ValueError):
            return {}

    def _write_session(self, fingerprint: str) -> None:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        paths.SESSION_FILE.write_text(json.dumps({"fingerprint": fingerprint, "started": time.time()}))

    def launch(self, args: list[str] | None = None, uid: str | None = None,
               log_name: str = "battlenet"):
        """Start Battle.net (or hand args to the running instance)."""
        if not self.installed():
            raise EngineError("Battle.net is not installed yet")
        args = list(args or [])
        fp = self.engine.fingerprint(uid)
        running = self.running()
        if running and self.cfg["restart_bnet_on_env_change"]:
            if self._read_session().get("fingerprint") != fp and not self.busy_processes():
                # Launch options changed: restart so the game inherits the new environment.
                self.engine.kill_all()
                running = False
        if running:
            # Hand the command to the running client. `runinprefix` skips Proton's steam.exe
            # shim, which would otherwise linger until Battle.net itself exits.
            spec = self.engine.build(self.exe, args, uid=uid, verb="runinprefix", log_name=log_name,
                                     wrap=False)
        else:
            self.apply_config()
            spec = self.engine.build(self.exe, args, uid=uid, verb="auto",
                                     log_name=log_name)
            self._write_session(fp)
        return self.engine.spawn(spec)

    def exec(self, command: str, uid: str | None = None, log_name: str = "battlenet"):
        """Battle.net --exec command, e.g. 'launch_uid wow' or 'install Pro'."""
        return self.launch([f"--exec={command}"], uid=uid, log_name=log_name)
