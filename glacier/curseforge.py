"""CurseForge, the official World of Warcraft addon manager, as a native Linux AppImage.

Glacier downloads the AppImage that https://www.curseforge.com/download/app links to, checks it
(host, size, MD5 from the CDN's ETag, AppImage header), and gives it an app-menu entry and icon.
The app runs on the host, not in the Proton prefix: point it at the World of Warcraft folder
Battle.net installed into. CurseForge updates the AppImage in place by itself afterwards.
"""

from __future__ import annotations

import copy
import ctypes.util
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import blizzard, desktop, download, paths
from .engine import EngineError, ProgressFn

if TYPE_CHECKING:
    from .battlenet import BattleNet
    from .engine import Engine
    from .library import Library

DOWNLOAD_URL = "https://curseforge.overwolf.com/downloads/curseforge-latest-linux.AppImage"
PAGE_URL = "https://www.curseforge.com/download/app"
TRUSTED_HOSTS = ("overwolf.com", "curseforge.com", "forgecdn.net")
# CurseForge registers its link handlers (cfauth:// finishes signing in) through Electron, which
# names the menu entry "<package name>.desktop". Using the same name keeps those links working.
DESKTOP_ID = "CurseForge.desktop"
ICON = "glacier-curseforge"
# The AppImage's own menu entry passes this too: Ubuntu's AppArmor blocks the user-namespace
# sandbox Electron falls back to, and the setuid helper can't work from a FUSE mount.
SANDBOX_ARGS = ["--no-sandbox"]
DEFAULT_MIME = "x-scheme-handler/curseforge;x-scheme-handler/cfauth;x-scheme-handler/curseforge-checkout;"
PLACEHOLDER_COLOR = "#f16436"


# --------------------------------------------------------------------------- AppImage format

@dataclass(frozen=True)
class AppImageInfo:
    offset: int             # where the squashfs payload starts
    static_runtime: bool    # no ELF interpreter: needs fusermount, not libfuse2


def inspect_appimage(path: Path) -> AppImageInfo:
    """Check that path is a type 2 AppImage and find its squashfs payload."""
    with open(path, "rb") as fh:
        head = fh.read(64)
        if len(head) < 64 or head[:4] != b"\x7fELF":
            raise ValueError("not an ELF executable")
        if head[8:11] != b"AI\x02":
            raise ValueError("not a type 2 AppImage")
        bits, endian = head[4], head[5]
        if endian != 1 or bits not in (1, 2):
            raise ValueError("unsupported ELF layout")
        if bits == 2:
            phoff, shoff = struct.unpack_from("<QQ", head, 0x20)
            phentsize, phnum, shentsize, shnum = struct.unpack_from("<HHHH", head, 0x36)
        else:
            phoff, shoff = struct.unpack_from("<II", head, 0x1C)
            phentsize, phnum, shentsize, shnum = struct.unpack_from("<HHHH", head, 0x2A)
        # The runtime's section headers come last in the ELF part; the image follows them.
        offset = shoff + shentsize * shnum
        fh.seek(offset)
        if fh.read(4) != b"hsqs":
            raise ValueError("no squashfs image after the AppImage runtime")
        has_interp = False
        for i in range(phnum):
            fh.seek(phoff + i * phentsize)
            raw = fh.read(4)
            if len(raw) == 4 and struct.unpack("<I", raw)[0] == 3:   # PT_INTERP
                has_interp = True
                break
    return AppImageInfo(offset, not has_interp)


def can_mount(info: AppImageInfo) -> bool:
    """Whether the AppImage can mount itself; otherwise it has to extract itself on every start."""
    if not os.path.exists("/dev/fuse"):
        return False
    if info.static_runtime:
        return bool(shutil.which("fusermount3") or shutil.which("fusermount"))
    return ctypes.util.find_library("fuse") is not None     # libfuse.so.2


def no_new_privs() -> bool:
    """Whether this process runs with no_new_privs (set by sandboxes such as bwrap).

    The AppImage mounts itself through the setuid fusermount, which can't gain root then and
    fails with "fusermount: mount failed: Operation not permitted". The flag is inherited by
    everything Glacier starts, so it only matters for launches, not for the menu entry.
    """
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("NoNewPrivs:"):
                    return line.split()[1:2] == ["1"]
    except OSError:
        pass
    return False


LIB_DIRS = ("/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu", "/usr/lib64", "/lib64", "/usr/lib", "/lib")


def _have_lib(soname: str) -> bool:
    return any(os.path.exists(os.path.join(d, soname)) for d in LIB_DIRS)


def openssl_override() -> str:
    """libssl version for CurseForge's addon agent, or "" when it finds one by itself.

    The agent (Curse.Agent.Host, .NET 10) only looks for libssl 3, 1.1 and 1.0 and aborts
    when none is there, which is the case on distributions that moved to OpenSSL 4 (Ubuntu
    26.10). .NET's DOTNET_OPENSSL_VERSION_OVERRIDE makes it load a newer one, which offers
    every function the OpenSSL 3 code path needs.
    """
    if any(os.environ.get(v) for v in ("DOTNET_OPENSSL_VERSION_OVERRIDE", "CLR_OPENSSL_VERSION_OVERRIDE")):
        return ""
    if any(_have_lib(f"libssl.so.{v}") for v in ("3", "1.1", "1.0.2", "1.0.0", "10")):
        return ""
    return next((str(v) for v in range(9, 3, -1) if _have_lib(f"libssl.so.{v}")), "")


def launch_env(info: AppImageInfo) -> dict[str, str]:
    """Extra environment CurseForge needs on this system."""
    env = {}
    if not can_mount(info):
        env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        # The runtime extracts to $TMPDIR/appimage_extracted_<md5> and deletes that folder when
        # it exits. A second start (a sign-in link, the menu while CurseForge is open) only hands
        # over to the open window, and its cleanup would pull the files from under it.
        env["NO_CLEANUP"] = "1"
    ssl = openssl_override()
    if ssl:
        env["DOTNET_OPENSSL_VERSION_OVERRIDE"] = ssl
    return env


def parse_desktop(text: str) -> dict[str, str]:
    """Keys of the [Desktop Entry] group (first value wins)."""
    out: dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Desktop Entry]" and "=" in line:
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), value.strip())
    return out


def short_version(raw: str) -> str:
    """'1.325.0-41594.41594' (X-AppImage-Version) -> '1.325.0-41594'."""
    m = re.match(r"\d+(?:\.\d+)*(?:-\d+)?", raw or "")
    return m.group(0) if m else raw


@dataclass
class Metadata:
    entry: dict[str, str]           # the AppImage's own [Desktop Entry]
    icon: bytes | None              # largest PNG icon

    @property
    def version(self) -> str:
        return short_version(self.entry.get("X-AppImage-Version", ""))


def _inside(root: Path, p: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _extract(appimage: Path, info: AppImageInfo, tmp: Path, timeout: float) -> Path:
    """Unpack the desktop entry and icons into tmp without starting the app."""
    unsquashfs = shutil.which("unsquashfs")
    if unsquashfs:
        root = tmp / "root"
        subprocess.run([unsquashfs, "-o", str(info.offset), "-n", "-f", "-d", str(root), str(appimage),
                        "*.desktop", "*.png", ".DirIcon", "usr/share/icons"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        if any(root.glob("*.desktop")):
            return root
    # The AppImage runtime can unpack itself too (it doesn't start the app for this).
    env = {k: v for k, v in os.environ.items() if not k.startswith("APPIMAGE")}
    for pattern in ("*.desktop", ".DirIcon", "*.png", "usr/share/icons/*"):
        subprocess.run([str(appimage), "--appimage-extract", pattern], cwd=tmp, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    return tmp / "squashfs-root"


def read_metadata(appimage: Path, info: AppImageInfo | None = None, timeout: float = 120) -> Metadata:
    info = info or inspect_appimage(appimage)
    with tempfile.TemporaryDirectory(prefix="glacier-appimage-") as d:
        root = _extract(appimage, info, Path(d), timeout)
        entry: dict[str, str] = {}
        for f in sorted(root.glob("*.desktop")):
            if _inside(root, f):
                entry = parse_desktop(f.read_text(errors="replace"))
                break
        name = entry.get("Icon", "")
        candidates: list[tuple[int, Path]] = []
        for f in root.glob(f"usr/share/icons/hicolor/*/apps/{name or '*'}.png"):
            m = re.match(r"(\d+)x\d+", f.parent.parent.name)
            candidates.append((int(m.group(1)) if m else 0, f))
        ordered = [f for _, f in sorted(candidates, reverse=True)]
        ordered += [root / ".DirIcon", root / f"{name}.png"]
        icon = None
        for f in ordered:
            if f.is_file() and _inside(root, f) and f.stat().st_size < 16 << 20:
                data = f.read_bytes()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    icon = data
                    break
    return Metadata(entry, icon)


def check_host(url: str) -> None:
    u = urlparse(url)
    host = u.hostname or ""
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in TRUSTED_HOSTS):
        raise EngineError(f"The CurseForge download went to an unexpected place: {url}")


def wow_folders(library: Library) -> list[Path]:
    """World of Warcraft install folders (the ones CurseForge asks for), without duplicates."""
    out: list[Path] = []
    for st in library.states.values():
        if st.installed and st.install_dir and st.game.folder == "World of Warcraft" and st.install_dir not in out:
            out.append(st.install_dir)
    return out


# --------------------------------------------------------------------------- WoW folders in CurseForge
#
# CurseForge's addon agent keeps one "game instance" per WoW flavour folder in
# ~/.config/CurseForge/agent/GameInstances/AddonGameInstance.json and reads it once at start.
# On Linux it has no registry to ask, so with an empty list it scans every mounted drive and
# adds each flavour folder it finds, including old _ptr_/_beta_ leftovers. Glacier knows from
# Battle.net's product.db which flavours are really installed, so it writes the list itself
# while CurseForge is closed: adds installed flavours, drops leftovers that hold no addons.

CF_CONFIG_DIR = paths.XDG_CONFIG / "CurseForge"     # Electron userData ("<package name>")
WOW_GAME_ID = 1
# CurseForge's game-version types for WoW, as its API lists them (games/1, "gmvers:1" in the
# app's query cache): one per flavour family, Classic ones per expansion.
RETAIL, FOREVER, CLASSIC_ERA, TITAN = 517, 88568, 67408, 81212
CLASSIC_BY_MAJOR = {1: 67408, 2: 73246, 3: 73713, 4: 77522, 5: 79434}
FLAVOUR_NAMES = {   # the names CurseForge itself gives these flavours
    "wow": "Retail", "wowt": "Retail PTR", "wowxptr": "Retail PTR X", "wow_beta": "Retail Beta",
    "wowf": "Forever", "wowf_beta": "Forever Beta", "wow_classic": "Classic",
    "wow_classic_ptr": "Classic PTR", "wow_classic_beta": "Classic Beta", "wow_classic_era": "Classic Era",
    "wow_classic_era_ptr": "Classic Era PTR", "wow_anniversary": "Anniversary",
    "wow_classic_titan": "Titan Reforged Classic",
}
_NULL_DATE = "0001-01-01T00:00:00"


@dataclass(frozen=True)
class Flavour:
    code: str           # Battle.net product code from the folder's .flavor.info (wow, wowf, ...)
    path: Path          # the flavour folder, e.g. .../World of Warcraft/_retail_
    version: str        # from the install's .build.info ("" if unknown)
    installed: bool     # Battle.net lists this product as installed in that folder


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)    # installed, but no CurseForge type known
    skipped: str = ""                                   # why nothing was checked

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def lines(self) -> list[str]:
        if self.skipped:
            return [self.skipped]
        out = [f"Added to CurseForge: {a}" for a in self.added]
        out += [f"Removed leftover from CurseForge: {r}" for r in self.removed]
        out += [f"Not added (unknown WoW flavour, add it in CurseForge): {u}" for u in self.unknown]
        return out


def version_type(code: str, version: str) -> int | None:
    """CurseForge's game-version type for a WoW flavour, or None when it isn't known."""
    c = code.lower()
    if c.startswith("wowf"):
        return FOREVER
    if c.startswith("wow_classic_titan"):
        return TITAN
    if c.startswith("wow_classic_era"):
        return CLASSIC_ERA
    if c.startswith(("wow_classic", "wow_anniversary")):
        # Progression Classic moves through expansions; the client version says which one.
        m = re.match(r"(\d+)\.", version or "")
        return CLASSIC_BY_MAJOR.get(int(m.group(1))) if m else None
    if c.startswith("wow"):
        return RETAIL
    return None


def flavour_name(code: str, folder: Path) -> str:
    return FLAVOUR_NAMES.get(code.lower()) or folder.name.strip("_").replace("_", " ").title()


def read_flavour_code(folder: Path) -> str:
    """Product code from a flavour folder's .flavor.info (a one-column BPSV table)."""
    try:
        rows = blizzard.parse_bpsv((folder / ".flavor.info").read_text(errors="replace"))
    except OSError:
        return ""
    return rows[0].get("Product Flavor", "").strip().lower() if rows else ""


def scan_wow_flavours(installs: dict[Path, set[str]]) -> list[Flavour]:
    """Flavour folders in each WoW install folder. `installs` maps an install folder to the
    product codes Battle.net has installed there."""
    out: list[Flavour] = []
    for root, codes in installs.items():
        try:
            versions = {b.product.lower(): b.version for b in blizzard.read_build_info(root)}
        except (OSError, ValueError):
            versions = {}
        try:
            subdirs = sorted(p for p in root.iterdir()
                             if p.is_dir() and p.name.startswith("_") and p.name.endswith("_"))
        except OSError:
            continue
        for d in subdirs:
            code = read_flavour_code(d)
            if code:
                out.append(Flavour(code, d, versions.get(code, ""), code in codes))
    return out


def wow_flavours(bnet: BattleNet, engine: Engine) -> list[Flavour]:
    """WoW flavour folders in every install Battle.net knows about."""
    installs: dict[Path, set[str]] = {}
    for p in bnet.products():
        if not p.product_code.lower().startswith("wow") or not p.install_path:
            continue
        root = engine.win_to_unix(p.install_path)
        if root is None or not root.is_dir():
            continue
        installs.setdefault(root, set())
        if p.installed:
            installs[root].add(p.product_code.lower())
    return scan_wow_flavours(installs)


def running() -> bool:
    """CurseForge (from any install) or its addon agent is running."""
    try:
        procs = [d for d in Path("/proc").iterdir() if d.name.isdigit()]
    except OSError:
        return False
    for d in procs:
        try:
            argv0 = (d / "cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            continue
        name = os.path.basename(argv0.decode(errors="replace")).lower()
        if name in ("curseforge", "curse.agent.host") or (name.endswith(".appimage") and "curseforge" in name):
            return True
    return False


def _norm(p: str) -> str:
    return os.path.normpath(p) if p else ""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_instance(template: dict | None, fl: Flavour, vtype: int) -> dict:
    """A game instance like the ones CurseForge writes, reusing an existing one's layout."""
    guid = str(uuid.uuid4())
    e = copy.deepcopy(template) if template else {
        "preferenceAlternateFile": False, "preferenceAutoInstallUpdates": False,
        "preferenceDeleteOrphanedDependencies": False, "preferenceDeleteSavedVariables": False,
        "preferenceReleaseType": 1,
    }
    e.update({
        "guid": guid, "gameTypeID": WOW_GAME_ID, "installPath": str(fl.path).rstrip("/") + "/",
        "name": flavour_name(fl.code, fl.path), "cachedScans": [], "isValid": True,
        "lastPreviousMatchUpdate": _NULL_DATE, "lastRefreshAttempt": _NULL_DATE, "isEnabled": True,
        "gameVersion": None, "gameVersionFlavor": None, "gameVersionTypeId": vtype,
        "preferenceModdingFolderPath": None, "installDate": _now(), "installedAddons": [],
        "installedGamePrerequisites": [], "wasNameManuallyChanged": False,
        "wasGameVersionTypeIdManuallyChanged": False,
        "syncProfile": {"PreferenceEnabled": False, "PreferenceAutoSync": True, "PreferenceAutoDelete": False,
                        "PreferenceBackupSavedVariables": False, "GameInstanceGuid": guid, "SyncProfileID": 0,
                        "SavedVariablesProfile": None, "LastSyncDate": _NULL_DATE},
    })
    return e


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".glacier-tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


def _repoint_last_instance(config_dir: Path, removed: set[str], entries: list[dict],
                           retail_path: str = "") -> None:
    """If CurseForge's last-used WoW instance was removed, point it at Battle.net's Retail
    (else any Retail, else the first WoW instance)."""
    f = config_dir / "storage.json"
    try:
        data = json.loads(f.read_text())
        last = json.loads(data["internal-last-game-instances"])
        current = last["lastGameInstances"].get(str(WOW_GAME_ID))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    if current not in removed:
        return
    wow = [e for e in entries if e.get("gameTypeID") == WOW_GAME_ID]
    pick = (next((e for e in wow if retail_path and _norm(e["installPath"]) == retail_path), None)
            or next((e for e in wow if os.path.basename(_norm(e["installPath"])) == "_retail_"), None)
            or (wow[0] if wow else None))
    if pick:
        last["lastGameInstances"][str(WOW_GAME_ID)] = pick.get("guid")
    else:
        last["lastGameInstances"].pop(str(WOW_GAME_ID), None)
    data["internal-last-game-instances"] = json.dumps(last, separators=(",", ":"))
    _write_json(f, data)


def sync_instances(flavours: list[Flavour], config_dir: Path | None = None) -> SyncResult:
    """Make CurseForge's WoW list match Battle.net. Call only while CurseForge is closed."""
    config_dir = config_dir or CF_CONFIG_DIR
    res = SyncResult()
    f = config_dir / "agent" / "GameInstances" / "AddonGameInstance.json"
    try:
        entries = json.loads(f.read_text())
    except FileNotFoundError:
        entries = []
    except (OSError, ValueError):
        res.skipped = f"Left CurseForge's game list alone: {f} is unreadable."
        return res
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("installPath"), str)
                                                for e in entries):
        res.skipped = f"Left CurseForge's game list alone: {f} has an unexpected format."
        return res

    by_folder = {_norm(str(fl.path)): fl for fl in flavours}
    roots = {_norm(str(fl.path.parent)) for fl in flavours}
    keep: list[dict] = []
    removed_guids: set[str] = set()
    for e in entries:
        p = _norm(e["installPath"])
        fl = by_folder.get(p)
        leftover = ((fl is not None and not fl.installed)
                    or (fl is None and os.path.dirname(p) in roots and not os.path.isdir(p)))
        # Only WoW instances in a WoW folder Battle.net manages, and never one with addons.
        if e.get("gameTypeID") == WOW_GAME_ID and leftover and not e.get("installedAddons"):
            res.removed.append(f"{e.get('name') or os.path.basename(p)} ({p})")
            removed_guids.add(e.get("guid"))
        else:
            keep.append(e)

    have = {_norm(e["installPath"]) for e in keep if e.get("gameTypeID") == WOW_GAME_ID}
    template = next((e for e in entries if e.get("gameTypeID") == WOW_GAME_ID), None)
    for fl in flavours:
        p = _norm(str(fl.path))
        if not fl.installed or p in have:
            continue
        vtype = version_type(fl.code, fl.version)
        if vtype is None:
            res.unknown.append(f"{fl.code} ({p})")
            continue
        keep.append(_new_instance(template, fl, vtype))
        have.add(p)
        res.added.append(f"{flavour_name(fl.code, fl.path)} ({p})")

    if res.changed:
        backup = f.with_name(f.name + ".glacier-backup")
        if f.exists() and not backup.exists():
            shutil.copy2(f, backup)
        _write_json(f, keep)
        retail = next((p for p, fl in by_folder.items() if fl.code == "wow" and fl.installed), "")
        _repoint_last_instance(config_dir, removed_guids, keep, retail)
    return res


def sync_games(bnet: BattleNet, engine: Engine) -> SyncResult:
    """Update CurseForge's WoW list from Battle.net, unless CurseForge is running (it would
    overwrite the file, and only reads it at start)."""
    if running():
        return SyncResult(skipped="CurseForge is running; Glacier updates its WoW list the next time "
                                  "it starts CurseForge.")
    return sync_instances(wow_flavours(bnet, engine))


# --------------------------------------------------------------------------- installed addons
#
# The same file lists each instance's addons: the file CurseForge installed and the newest one
# it found for the instance's release-type preference. It only looks for updates while it runs,
# and saves the list as it goes, so this is what CurseForge last saw. Read-only: CurseForge
# rewrites the file while running, and its own API is private (a WebSocket keyed per start).

RELEASE_TYPES = {2: "beta", 3: "alpha"}     # CurseForge file releaseType; 1 = release


def instances_file(config_dir: Path | None = None) -> Path:
    return (config_dir or CF_CONFIG_DIR) / "agent" / "GameInstances" / "AddonGameInstance.json"


def file_version(f: dict) -> str:
    """A short version for a CurseForge file: "1.8.7" from "Narcissus-1.8.7.zip", else its date."""
    stem = re.sub(r"\.zip$", "", str(f.get("fileName") or ""), flags=re.IGNORECASE)
    # Packages are named "<addon folder>-<version>.zip", and folder names can hold digits
    # themselves (LibSharedMedia-3.0, 3DPortraits), so skip a leading folder name first.
    mods = f.get("modules") if isinstance(f.get("modules"), list) else []
    folders = sorted((m["foldername"] for m in mods if isinstance(m, dict) and isinstance(m.get("foldername"), str)
                      and m["foldername"]), key=len, reverse=True)
    rest = next((stem[len(n) + 1:] for n in folders
                 if stem.lower().startswith(n.lower()) and stem[len(n):len(n) + 1] in ("-", "_", " ", ".")), stem)
    m = re.search(r"(?:^|[-_ .])([vVrR]?\d[\w.+-]*)$", rest)
    out = m.group(1) if m else (str(f.get("fileDate") or "")[:10] or rest or stem)
    rt = f.get("releaseType")
    kind = RELEASE_TYPES.get(rt) if isinstance(rt, int) else None
    return f"{out} ({kind})" if kind and kind not in out.lower() else out


@dataclass(frozen=True)
class Addon:
    name: str
    author: str
    url: str            # its CurseForge page ("" if unknown)
    version: str        # the installed file
    latest: str         # the newer file CurseForge would install ("" when there is none)

    @property
    def update(self) -> bool:
        return bool(self.latest)


@dataclass
class AddonList:
    folders: dict[Path, list[Addon]]    # by WoW flavour folder, sorted by name
    checked: datetime | None            # CurseForge's last look for updates


def _addon(a: dict) -> Addon | None:
    inst = a.get("installedFile")
    if not isinstance(inst, dict):
        return None
    new = a.get("latestFile")
    # The same test CurseForge's own Update button uses (checkCanUpdateProject in its app),
    # plus "not older": a beta installed on purpose isn't outdated by an older release.
    newer = (isinstance(new, dict) and new.get("id") not in (None, inst.get("id"))
             and str(new.get("fileDate") or "") >= str(inst.get("fileDate") or "")
             and not new.get("isEarlyAccessContent") and not a.get("preferenceIsIgnored")
             and not a.get("isWorkingCopy") and not a.get("isFuzzyMatch"))
    authors = a.get("authors") if isinstance(a.get("authors"), list) else []
    author = a.get("primaryAuthor") or next((x.get("name") for x in authors if isinstance(x, dict)), "")
    url = a.get("webSiteURL")
    return Addon(str(a.get("name") or a.get("fileNameOnDisk") or "?"), str(author or ""),
                 url if isinstance(url, str) and url.startswith("https://") else "",
                 file_version(inst), file_version(new) if newer else "")


def _parse_time(value) -> datetime | None:
    """A .NET timestamp from CurseForge's file ("...T18:43:08.4825421+01:00", "...Z"), or None."""
    if not isinstance(value, str) or value.startswith("0001-"):      # DateTime.MinValue: never
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def installed_addons(config_dir: Path | None = None) -> AddonList | None:
    """CurseForge's WoW addons. Empty when CurseForge has no list yet; None when the file is
    there but can't be read (half written, or a layout Glacier doesn't know)."""
    try:
        entries = json.loads(instances_file(config_dir).read_text())
    except FileNotFoundError:
        return AddonList({}, None)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(entries, list):
        return None
    out = AddonList({}, None)
    for e in entries:
        if not isinstance(e, dict) or e.get("gameTypeID") != WOW_GAME_ID or not isinstance(e.get("installPath"), str):
            continue
        listed = e.get("installedAddons") if isinstance(e.get("installedAddons"), list) else []
        addons = [x for x in (_addon(a) for a in listed if isinstance(a, dict)) if x]
        out.folders.setdefault(Path(_norm(e["installPath"])), []).extend(addons)
        checked = _parse_time(e.get("lastRefreshAttempt"))
        if checked and (out.checked is None or checked > out.checked):
            out.checked = checked
    for addons in out.folders.values():
        addons.sort(key=lambda x: x.name.casefold())
    return out


# --------------------------------------------------------------------------- install / update

@dataclass
class Remote:
    url: str
    size: int
    md5: str            # the ETag when it is a plain MD5 (single-part upload), else ""
    modified: str


@dataclass
class Result:
    version: str
    changed: bool       # a new AppImage was installed
    message: str


class CurseForge:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.APPS_DIR

    @property
    def appimage(self) -> Path:
        return self.root / "CurseForge.AppImage"

    @property
    def state_file(self) -> Path:
        return self.root / "curseforge.json"

    @property
    def _new_file(self) -> Path:
        return self.root / "CurseForge.AppImage.new"

    def installed(self) -> bool:
        return self.appimage.is_file()

    def _state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **values) -> None:
        data = self._state()
        data.update(values)
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self.state_file)

    def version(self) -> str:
        """Version Glacier last saw (CurseForge may have updated itself since)."""
        return self._state().get("version", "") if self.installed() else ""

    def local_md5(self) -> str:
        st = self.appimage.stat()
        s = self._state()
        if s.get("md5") and s.get("size") == st.st_size and s.get("mtime_ns") == st.st_mtime_ns:
            return s["md5"]
        # Changed since Glacier installed it: CurseForge updated itself.
        md5 = download.file_hash(self.appimage, "md5")
        values = {"md5": md5, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        try:
            values["version"] = read_metadata(self.appimage).version or s.get("version", "")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self._save_state(**values)
        return md5

    def remote(self) -> Remote:
        with download.session() as s:
            r = s.head(DOWNLOAD_URL, allow_redirects=True, timeout=20)
            r.raise_for_status()
        check_host(r.url)
        etag = r.headers.get("ETag", "").removeprefix("W/").strip('"').lower()
        return Remote(r.url, int(r.headers.get("Content-Length") or 0),
                      etag if re.fullmatch(r"[0-9a-f]{32}", etag) else "", r.headers.get("Last-Modified", ""))

    def _differs(self, rem: Remote) -> bool:
        if rem.md5:
            return rem.md5 != self.local_md5()
        return bool(rem.size) and rem.size != self.appimage.stat().st_size

    def update_available(self) -> bool:
        return self.installed() and self._differs(self.remote())

    def _check_entry_free(self) -> None:
        if desktop.entry_exists(DESKTOP_ID) and not desktop.entry_managed(DESKTOP_ID):
            raise EngineError(f"{paths.APPLICATIONS_DIR / DESKTOP_ID} belongs to another CurseForge install. "
                              "Remove that one first, or keep using it.")

    def _download(self, rem: Remote, progress: ProgressFn | None,
                  cancel: threading.Event | None) -> Path:
        def on_bytes(done: int, total: int) -> None:
            if progress:
                progress("Downloading CurseForge", done / total if total else -1)

        dest = self._new_file
        for _ in range(2):
            download.download(rem.url, dest, on_bytes, cancel)
            size_ok = not rem.size or dest.stat().st_size == rem.size
            if size_ok and (not rem.md5 or download.file_hash(dest, "md5") == rem.md5):
                return dest
            # A .part left from an older build, or a broken transfer: start over once.
            dest.unlink(missing_ok=True)
        raise EngineError("The CurseForge download is damaged (size or checksum mismatch); try again")

    def install(self, progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                force: bool = False) -> Result:
        """Install CurseForge, or update it when the download changed. Idempotent."""
        self._check_entry_free()
        if progress:
            progress("Checking the CurseForge download", -1)
        rem = self.remote()
        if self.installed() and not force and not self._differs(rem):
            self.integrate()
            v = self.version()
            return Result(v, False, f"CurseForge {v} is up to date." if v else "CurseForge is up to date.")
        self.root.mkdir(parents=True, exist_ok=True)
        new = self._download(rem, progress, cancel)
        try:
            info = inspect_appimage(new)
        except ValueError as e:
            new.unlink(missing_ok=True)
            raise EngineError(f"The CurseForge download is not an AppImage ({e})") from None
        if progress:
            progress("Installing CurseForge", -1)
        meta = read_metadata(new, info)
        os.chmod(new, 0o755)
        # Replacing is safe while CurseForge runs: the mounted old file stays until it exits.
        os.replace(new, self.appimage)
        st = self.appimage.stat()
        self._save_state(version=meta.version, md5=rem.md5 or download.file_hash(self.appimage, "md5"),
                         size=st.st_size, mtime_ns=st.st_mtime_ns, modified=rem.modified,
                         installed_at=int(time.time()))
        self.integrate(meta, info)
        if progress:
            progress("CurseForge installed", 1.0)
        v = meta.version
        return Result(v, True, f"Installed CurseForge {v}." if v else "Installed CurseForge.")

    def integrate(self, meta: Metadata | None = None, info: AppImageInfo | None = None) -> Path:
        """(Re)write the menu entry and icon for the installed AppImage."""
        self._check_entry_free()
        try:
            info = info or inspect_appimage(self.appimage)
        except ValueError as e:
            raise EngineError(f"{self.appimage} is not an AppImage ({e})") from None
        meta = meta or read_metadata(self.appimage, info)
        if meta.icon:
            from PIL import Image, UnidentifiedImageError
            try:
                big = Image.open(io.BytesIO(meta.icon)).convert("RGBA")
                desktop.install_icon_images(ICON, {s: big.resize((s, s), Image.LANCZOS)
                                                   for s in desktop.ICON_SIZES})
            except (OSError, UnidentifiedImageError):
                desktop.install_placeholder_icon(ICON, "CF", PLACEHOLDER_COLOR)
        else:
            desktop.install_placeholder_icon(ICON, "CF", PLACEHOLDER_COLOR)
        entry = meta.entry
        env = launch_env(info) or None
        path = desktop.write_entry(
            DESKTOP_ID, entry.get("Name") or "CurseForge", SANDBOX_ARGS, ICON,
            comment="World of Warcraft addon manager", wm_class=entry.get("StartupWMClass") or "CurseForge",
            categories="Game;", keywords="addons;wow;warcraft;mods;curse;",
            command=[str(self.appimage)], field_code="%U",
            mime_types=entry.get("MimeType") or DEFAULT_MIME, extra_env=env)
        desktop.refresh()
        return path

    def remove(self) -> None:
        """Remove the AppImage, menu entry and icon. CurseForge's settings and addons stay."""
        if desktop.entry_managed(DESKTOP_ID):
            desktop.remove_entry(DESKTOP_ID)
        desktop.remove_icon(ICON)
        for p in (self.appimage, self.state_file, self._new_file,
                  self._new_file.with_name(self._new_file.name + ".part")):
            p.unlink(missing_ok=True)
        desktop.refresh()

    def _remove_old_extractions(self, keep_current: bool) -> None:
        """Drop CurseForge folders extract-and-run left in $TMPDIR (NO_CLEANUP keeps them), except
        the current build's when this start reuses it. Only call while CurseForge is closed."""
        tmp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        try:
            keep = f"appimage_extracted_{self.local_md5()}" if keep_current else ""
            found = list(tmp.glob("appimage_extracted_*"))
        except OSError:
            return
        for d in found:
            try:
                ours = d.is_dir() and not d.is_symlink() and d.stat().st_uid == os.getuid()
            except OSError:
                continue
            if ours and d.name != keep and (d / "resources" / "app.asar").is_file() and (d / "curseforge").is_file():
                shutil.rmtree(d, ignore_errors=True)

    def launch(self) -> tuple[subprocess.Popen, Path]:
        """Start CurseForge detached; returns the AppImage process and its log file."""
        if not self.installed():
            raise EngineError("CurseForge is not installed (glacier curseforge install)")
        try:
            info = inspect_appimage(self.appimage)
        except ValueError as e:
            raise EngineError(f"{self.appimage} is not an AppImage ({e})") from None
        already = running()
        env = dict(os.environ)
        env.update(launch_env(info))
        if no_new_privs():
            env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            if already:     # see launch_env: this start only hands over to the open window
                env["NO_CLEANUP"] = "1"
        if not already:
            self._remove_old_extractions(keep_current=bool(env.get("APPIMAGE_EXTRACT_AND_RUN")))
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = paths.LOG_DIR / "curseforge.log"
        # A fresh log per CurseForge session. While one runs it keeps writing here, so a start
        # that only hands over appends instead (truncating under a writer leaves a hole of NUL
        # bytes), and O_APPEND keeps writers from overwriting each other's lines.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | (0 if already else os.O_TRUNC)
        with os.fdopen(os.open(log, flags, 0o644), "wb") as fh:
            fh.write(f"--- Glacier started CurseForge at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
            fh.flush()
            proc = subprocess.Popen([str(self.appimage), *SANDBOX_ARGS], stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, env=env, start_new_session=True)
        return proc, log
