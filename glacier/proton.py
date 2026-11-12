"""Discover, download and manage Proton builds and Steam Linux Runtime containers."""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Mapping

from . import download, paths, vdf

ProgressFn = Callable[[str, float], None]   # (message, fraction 0..1 or -1 for busy)

GE_RELEASES_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"

# Steam Linux Runtime containers, keyed by Steam app id (as used in toolmanifest.vdf).
RUNTIMES = {
    "1628350": ("SteamLinuxRuntime_sniper", "Steam Linux Runtime 3.0 (sniper)",
                "https://repo.steampowered.com/steamrt3/images/latest-container-runtime-public-beta/"
                "SteamLinuxRuntime_sniper.tar.xz"),
    "4183110": ("SteamLinuxRuntime_4", "Steam Linux Runtime 4.0",
                "https://repo.steampowered.com/steamrt4/images/latest-container-runtime-public-beta/"
                "SteamLinuxRuntime_4.tar.xz"),
    "1391110": ("SteamLinuxRuntime_soldier", "Steam Linux Runtime 2.0 (soldier)",
                "https://repo.steampowered.com/steamrt-images-soldier/snapshots/"
                "latest-container-runtime-public-beta/SteamLinuxRuntime_soldier.tar.xz"),
}


@dataclass
class Runtime:
    appid: str
    name: str
    path: Path

    @property
    def entry_point(self) -> Path:
        return self.path / "_v2-entry-point"


@dataclass
class Runner:
    id: str
    name: str
    path: Path
    source: str                 # steam | compattools | glacier
    version: str = ""
    runtime_appid: str | None = None
    sort_key: tuple = field(default_factory=tuple)

    @property
    def proton(self) -> Path:
        return self.path / "proton"

    @property
    def files_dir(self) -> Path:
        for sub in ("files", "dist"):
            if (self.path / sub / "bin").is_dir():
                return self.path / sub
        return self.path / "files"

    @property
    def wine(self) -> Path:
        return self.files_dir / "bin" / "wine"

    @property
    def wineserver(self) -> Path:
        return self.files_dir / "bin" / "wineserver"

    @property
    def is_ge(self) -> bool:
        return "GE-Proton" in self.name or "proton-ge" in self.name.lower()

    @property
    def is_experimental(self) -> bool:
        return "experimental" in self.name.lower()

    @property
    def removable(self) -> bool:
        return self.source == "glacier"

    @property
    def label(self) -> str:
        where = {"steam": "Steam", "compattools": "Steam compat tool", "glacier": "Glacier"}[self.source]
        return f"{self.name}  ·  {where}"


# --------------------------------------------------------------------------- Steam discovery

def steam_roots() -> list[Path]:
    home = paths.HOME
    candidates = [
        home / ".steam" / "root",
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".steam" / "debian-installation",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
    ]
    seen: set[Path] = set()
    roots = []
    for c in candidates:
        try:
            real = c.resolve()
        except OSError:
            continue
        if real in seen or not (real / "steamapps").is_dir():
            continue
        seen.add(real)
        roots.append(real)
    return roots


def steam_libraries() -> list[Path]:
    libs: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        try:
            real = p.resolve()
        except OSError:
            return
        if real not in seen and (real / "steamapps").is_dir():
            seen.add(real)
            libs.append(real)

    for root in steam_roots():
        add(root)
        lf = root / "steamapps" / "libraryfolders.vdf"
        if not lf.is_file():
            continue
        try:
            data = vdf.load(lf).find("libraryfolders", {})
        except OSError:
            continue
        for entry in data.values():
            if isinstance(entry, dict) and entry.get("path"):
                add(Path(entry["path"]))
            elif isinstance(entry, str) and entry.startswith("/"):
                add(Path(entry))     # very old format: "1" "/path"
    return libs


def _version_key(name: str) -> tuple:
    nums = tuple(int(n) for n in re.findall(r"\d+", name))
    return nums or (0,)


def _read_runner(d: Path, source: str) -> Runner | None:
    if not (d / "proton").is_file():
        return None
    name = d.name
    compat_vdf = d / "compatibilitytool.vdf"
    if compat_vdf.is_file():
        try:
            tools = vdf.load(compat_vdf).find("compatibilitytools", {}).find("compat_tools", {})
            for tool in tools.values():
                if isinstance(tool, dict) and tool.get("display_name"):
                    name = tool["display_name"]
                    break
        except (OSError, AttributeError):
            pass
    version = ""
    try:
        version = (d / "version").read_text().split()[-1]
    except (OSError, IndexError):
        pass
    runtime_appid = None
    manifest = d / "toolmanifest.vdf"
    if manifest.is_file():
        try:
            runtime_appid = vdf.load(manifest).find("manifest", {}).find("require_tool_appid")
        except OSError:
            pass
    rid = f"{source}:{d.name}"
    lname = name.lower()
    if "experimental" in lname:
        rank = 1
    elif "ge-proton" in lname or "proton-ge" in lname:
        rank = 2
    elif lname.startswith("proton"):
        rank = 3
    else:
        rank = 0
    return Runner(rid, name, d, source, version, runtime_appid, (rank, _version_key(name)))


def find_runners() -> list[Runner]:
    runners: list[Runner] = []
    seen: set[Path] = set()

    def consider(d: Path, source: str) -> None:
        try:
            real = d.resolve()
        except OSError:
            return
        if real in seen:
            return
        r = _read_runner(d, source)
        if r:
            seen.add(real)
            runners.append(r)

    for lib in steam_libraries():
        common = lib / "steamapps" / "common"
        if common.is_dir():
            for d in sorted(common.iterdir()):
                if d.name.lower().startswith("proton"):
                    consider(d, "steam")
    for root in steam_roots():
        ct = root / "compatibilitytools.d"
        if ct.is_dir():
            for d in sorted(ct.iterdir()):
                consider(d, "compattools")
    if paths.RUNNERS_DIR.is_dir():
        for d in sorted(paths.RUNNERS_DIR.iterdir()):
            consider(d, "glacier")
    runners.sort(key=lambda r: r.sort_key, reverse=True)
    return runners


def default_runner(runners: list[Runner]) -> Runner | None:
    """Newest stable Valve Proton, then newest GE-Proton, then anything (Experimental last)."""
    for pred in (lambda r: r.sort_key[0] == 3, lambda r: r.is_ge, lambda r: True):
        matches = [r for r in runners if pred(r)]
        if matches:
            return max(matches, key=lambda r: r.sort_key)
    return None


def find_runtime(appid: str | None) -> Runtime | None:
    if not appid:
        return None
    info = RUNTIMES.get(appid)
    for lib in steam_libraries():
        acf = lib / "steamapps" / f"appmanifest_{appid}.acf"
        if acf.is_file():
            try:
                state = vdf.load(acf).find("AppState", {})
                installdir = state.find("installdir")
                name = state.find("name") or (info[1] if info else appid)
            except OSError:
                continue
            if installdir:
                path = lib / "steamapps" / "common" / installdir
                if (path / "_v2-entry-point").is_file():
                    return Runtime(appid, name, path)
    if info:
        path = paths.RUNTIMES_DIR / info[0]
        if (path / "_v2-entry-point").is_file():
            return Runtime(appid, info[1], path)
        # Steam may have it installed without a manifest in a library we know about.
        for lib in steam_libraries():
            path = lib / "steamapps" / "common" / info[0]
            if (path / "_v2-entry-point").is_file():
                return Runtime(appid, info[1], path)
    return None


# --------------------------------------------------------------------------- host graphics

VULKAN_ICD_DIRS = ("/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d", "/usr/share/vulkan/icd.d")
# Where 32-bit libraries live: Debian/Ubuntu multiarch, Arch's lib32, Fedora/openSUSE's /usr/lib.
LIB32_DIRS = ("/usr/lib/i386-linux-gnu", "/usr/lib32", "/usr/lib", "/lib/i386-linux-gnu", "/lib32")
VULKAN32_HINT = ("Battle.net is a 32-bit program, and Proton runs it with your graphics driver's 32-bit "
                 "Vulkan library. Without one, Battle.net closes right after it starts. On Debian or "
                 "Ubuntu: sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install "
                 "mesa-vulkan-drivers:i386 (NVIDIA: your driver's 32-bit libraries). Arch: "
                 "lib32-vulkan-radeon, lib32-vulkan-intel or lib32-nvidia-utils. Fedora: "
                 "mesa-vulkan-drivers.i686 (NVIDIA: xorg-x11-drv-nvidia-libs.i686). The README lists more.")


def _elf_class(path: Path) -> int:
    """1 for a 32-bit ELF file, 2 for 64-bit, 0 for anything else (or missing)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
    except OSError:
        return 0
    return head[4] if len(head) == 5 and head[:4] == b"\x7fELF" else 0


def vulkan32_available(icd_dirs: tuple[str, ...] = VULKAN_ICD_DIRS,
                       lib32_dirs: tuple[str, ...] = LIB32_DIRS, *,
                       env: Mapping[str, str] | None = None) -> bool | None:
    """Whether the host has a 32-bit Vulkan driver, which Battle.net needs.

    Battle.net.exe is 32-bit and its browser renders through Vulkan (DXVK or ANGLE). Proton's
    32-bit side loads the host's 32-bit driver, which pressure-vessel imports into the runtime
    container. Without one the client's GPU process fails and Battle.net aborts at start.
    Steam from the distribution's packages pulls these libraries in; Flatpak and Snap Steam
    bring their own inside the sandbox, which Battle.net doesn't use.

    Each Vulkan driver manifest names its library either by path or by bare name (Debian 13's
    Mesa uses one manifest for both architectures). Returns None when no manifest is found or
    the user points the loader elsewhere in `env` (Battle.net's environment, default ours), so
    callers only warn when sure.
    """
    env = os.environ if env is None else env
    if any(env.get(k) for k in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES", "VK_ADD_DRIVER_FILES")):
        return None
    found = False
    for d in icd_dirs:
        try:
            manifests = sorted(Path(d).glob("*.json"))
        except OSError:
            continue
        for manifest in manifests:
            try:
                lib = json.loads(manifest.read_text()).get("ICD", {}).get("library_path")
            except (OSError, ValueError, AttributeError):
                continue
            if not isinstance(lib, str) or not lib:
                continue
            found = True
            if "/" in lib:
                candidates = [manifest.parent / lib]      # absolute, or relative to the manifest
            else:
                candidates = [Path(ld) / lib for ld in lib32_dirs]
            if any(_elf_class(c) == 1 for c in candidates):
                return True
    return False if found else None


# --------------------------------------------------------------------------- GE-Proton

@dataclass
class Release:
    tag: str
    published: str
    tarball_url: str
    tarball_name: str
    size: int
    checksum_url: str | None
    body: str = ""


def list_ge_releases(limit: int = 10) -> list[Release]:
    data = download.get_json(f"{GE_RELEASES_API}?per_page={limit}")
    releases = []
    for rel in data:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tar = sha = None
        for asset in rel.get("assets", []):
            n = asset["name"]
            if "aarch64" in n or "arm64" in n:
                continue
            if n.endswith(".tar.gz") or n.endswith(".tar.xz"):
                tar = asset
            elif n.endswith(".sha512sum"):
                sha = asset
        if tar:
            releases.append(Release(rel["tag_name"], rel.get("published_at", "")[:10],
                                    tar["browser_download_url"], tar["name"], tar["size"],
                                    sha["browser_download_url"] if sha else None,
                                    rel.get("body") or ""))
    return releases


_ARCH_SUFFIX = re.compile(r"-(x86_64|amd64|aarch64|arm64)$")


def ge_tag(dirname: str) -> str:
    """Release tag of an installed GE-Proton directory.

    Since GE-Proton 11 a release ships one tarball per architecture, so tag GE-Proton11-12
    unpacks to GE-Proton11-12-x86_64.
    """
    return _ARCH_SUFFIX.sub("", dirname)


def installed_ge_tags() -> set[str]:
    return {ge_tag(r.path.name) for r in find_runners() if r.is_ge}


def match_runner(runners: list[Runner], name: str) -> Runner | None:
    """The runner a user means by id, display name, directory or GE-Proton release tag.

    Glacier's own copy wins when Steam has a build of the same name, so it can be removed.
    """
    matches = [r for r in runners
               if name in (r.id, r.name, r.path.name) or (r.is_ge and name == ge_tag(r.path.name))]
    return next((r for r in matches if r.removable), matches[0] if matches else None)


def _extract(archive: Path, dest_parent: Path, progress: ProgressFn | None,
             cancel: threading.Event | None, label: str) -> Path:
    """Extract a single-top-level-directory archive into dest_parent; returns that directory."""
    staging = dest_parent / f".extract-{archive.stem}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            n = max(len(members), 1)
            for i, m in enumerate(members):
                if cancel is not None and cancel.is_set():
                    raise download.Cancelled()
                tf.extract(m, staging, filter="tar")
                if progress and i % 200 == 0:
                    progress(f"Extracting {label}", i / n)
        tops = [p for p in staging.iterdir()]
        if len(tops) != 1 or not tops[0].is_dir():
            raise RuntimeError(f"Unexpected archive layout in {archive.name}")
        final = dest_parent / tops[0].name
        if final.exists():
            shutil.rmtree(final)
        tops[0].rename(final)
        return final
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_ge(release: Release, progress: ProgressFn | None = None,
               cancel: threading.Event | None = None) -> Runner:
    paths.ensure_dirs()
    archive = paths.DOWNLOADS_DIR / release.tarball_name

    def on_bytes(done: int, total: int) -> None:
        if progress:
            frac = done / total if total else -1
            progress(f"Downloading {release.tag} ({done >> 20} / {max(total, release.size) >> 20} MiB)", frac)

    download.download(release.tarball_url, archive, on_bytes, cancel)

    if release.checksum_url:
        if progress:
            progress(f"Verifying {release.tag}", -1)
        expected = download.get_text(release.checksum_url).split()[0].strip().lower()
        actual = download.file_hash(archive, "sha512")
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for {release.tarball_name}; download removed.")

    final = _extract(archive, paths.RUNNERS_DIR, progress, cancel, release.tag)
    archive.unlink(missing_ok=True)
    runner = _read_runner(final, "glacier")
    if runner is None:
        raise RuntimeError(f"{final} does not look like a Proton build")
    if progress:
        progress(f"Installed {release.tag}", 1.0)
    return runner


def remove_runner(runner: Runner) -> None:
    if not runner.removable:
        raise RuntimeError("Only Proton builds downloaded by Glacier can be removed here.")
    shutil.rmtree(runner.path)


_RUNTIME_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _runtime_source(url: str) -> tuple[str, str]:
    """Where to get a runtime tarball, and Valve's SHA-256 for it ("" when unknown).

    The "latest" folder is served file by file from a CDN whose copies go out of step: its
    SHA256SUMS can describe an older build than its tarball. So read the version it names and
    take both from that version's own folder, which never changes. Without a usable version
    or checksum list, fall back to the latest link, unverified.
    """
    folder, _, name = url.rpartition("/")
    try:
        version = download.get_text(f"{folder}/VERSION.txt").strip()
        if not _RUNTIME_VERSION.fullmatch(version):
            return url, ""
        pinned = f"{folder.rpartition('/')[0]}/{version}"
        sums = download.get_text(f"{pinned}/SHA256SUMS")
    except OSError:                 # requests' errors are OSErrors
        return url, ""
    for line in sums.splitlines():
        digest, _, listed = line.partition(" ")
        if listed.strip().lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return f"{pinned}/{name}", digest.lower()
    return url, ""


def install_runtime(appid: str, progress: ProgressFn | None = None,
                    cancel: threading.Event | None = None) -> Runtime:
    if appid not in RUNTIMES:
        raise RuntimeError(f"Unknown Steam Linux Runtime app id {appid}")
    dirname, name, url = RUNTIMES[appid]
    paths.ensure_dirs()
    archive = paths.DOWNLOADS_DIR / url.rsplit("/", 1)[1]

    def on_bytes(done: int, total: int) -> None:
        if progress:
            progress(f"Downloading {name} ({done >> 20} MiB)", done / total if total else -1)

    source, expected = _runtime_source(url)
    download.download(source, archive, on_bytes, cancel)
    if expected:
        if progress:
            progress(f"Verifying {name}", -1)
        if download.file_hash(archive, "sha256") != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeError(f"Checksum mismatch for {archive.name}; download removed.")
    final = _extract(archive, paths.RUNTIMES_DIR, progress, cancel, name)
    archive.unlink(missing_ok=True)
    if final.name != dirname:
        target = paths.RUNTIMES_DIR / dirname
        if target.exists():
            shutil.rmtree(target)
        final.rename(target)
        final = target
    return Runtime(appid, name, final)
