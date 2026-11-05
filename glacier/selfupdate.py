"""Glacier's own updates, from the GitHub releases of its repository.

Each release carries `glacier-<version>.tar.gz` (a `git archive` of the tag) and `SHA256SUMS`.
Updating downloads both, checks the checksum, unpacks the tarball and runs its `install.sh`
into the same place the running copy was installed to.

The repository may be private. GitHub only shows a private repository's releases to signed-in
users, so Glacier asks through the GitHub CLI (`gh`) when it is installed and signed in, and
falls back to plain HTTPS (which is enough for a public repository).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

from . import __version__, download, paths
from .engine import EngineError, ProgressFn

REPO = "Ari-Santa/glacier"
RELEASES_URL = f"https://github.com/{REPO}/releases"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
SUMS_NAME = "SHA256SUMS"
CHECK_INTERVAL = 6 * 3600           # seconds between automatic checks
STATE_FILE = paths.STATE_DIR / "selfupdate.json"


class UpdateError(EngineError):
    pass


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v0.2.0' / '0.2.0' -> (0, 2, 0)."""
    m = re.match(r"v?(\d+(?:\.\d+)*)$", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


@dataclass
class Release:
    tag: str
    version: str
    url: str                # release page
    published: str          # YYYY-MM-DD
    notes: str
    asset_name: str
    asset_url: str
    sums_url: str

    @property
    def summary(self) -> str:
        """First meaningful line of the release notes."""
        for line in self.notes.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line
        return ""


def release_from_json(data: dict) -> Release | None:
    """A Release from GitHub's API answer, or None when it lacks a version or tarball."""
    tag = data.get("tag_name") or ""
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None:
        return None
    version = tag.lstrip("v")
    assets = {a.get("name"): a.get("browser_download_url", "") for a in data.get("assets") or []}
    name = f"glacier-{version}.tar.gz"
    if name not in assets or SUMS_NAME not in assets:
        return None
    return Release(tag, version, data.get("html_url") or f"{RELEASES_URL}/tag/{tag}",
                   (data.get("published_at") or "")[:10], data.get("body") or "", name, assets[name],
                   assets[SUMS_NAME])


def _gh() -> str | None:
    return shutil.which("gh")


def fetch_latest() -> Release | None:
    """The newest published release (network)."""
    gh = _gh()
    if gh:
        try:
            r = subprocess.run([gh, "api", f"repos/{REPO}/releases/latest"], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                return release_from_json(json.loads(r.stdout))
            if "Not Found" in r.stdout + r.stderr and "HTTP 404" in r.stderr:
                # Signed in and allowed to see the repository, but nothing is released yet.
                return None
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        return release_from_json(download.get_json(API_LATEST))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            raise UpdateError(f"Glacier's releases aren't visible from here ({RELEASES_URL}). If the "
                              "repository is private, sign in with `gh auth login` to check for updates.") from None
        raise


# --------------------------------------------------------------------------- checking

def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE_FILE)


def cached_latest() -> Release | None:
    """Release seen by the last check (no network)."""
    latest = _load_state().get("latest")
    try:
        return Release(**latest) if isinstance(latest, dict) else None
    except TypeError:
        return None


def available() -> Release | None:
    """A newer release seen by the last check (no network)."""
    rel = cached_latest()
    return rel if rel and is_newer(rel.version) else None


def check(force: bool = False) -> Release | None:
    """Ask GitHub for the newest release (at most every CHECK_INTERVAL unless forced).
    Returns it when it is newer than the running Glacier."""
    state = _load_state()
    checked = state.get("checked", 0)
    if not force and isinstance(checked, (int, float)) and 0 <= time.time() - checked < CHECK_INTERVAL:
        return available()
    rel = fetch_latest()
    _save_state({"checked": int(time.time()), "latest": asdict(rel) if rel else None})
    return rel if rel and is_newer(rel.version) else None


# --------------------------------------------------------------------------- installing

def install_prefix() -> Path | None:
    """The PREFIX install.sh installed the running copy into (…/lib/glacier/glacier), or None
    when Glacier runs from somewhere else, such as a source checkout (`install.sh --dev`)."""
    pkg = paths.PACKAGE_DIR
    lib = pkg.parent
    if (lib / ".git").exists() or lib.name != "glacier" or lib.parent.name != "lib":
        return None
    return lib.parent.parent


def can_self_update() -> tuple[bool, str]:
    if install_prefix() is None:
        where = paths.PACKAGE_DIR.parent
        hint = "update it with `git pull`" if (where / ".git").exists() else "reinstall it with install.sh"
        return False, f"Glacier runs from {where}, not from an install.sh copy; {hint}."
    return True, ""


def _fetch_asset(rel: Release, name: str, url: str, dest: Path, progress: ProgressFn | None,
                 cancel: threading.Event | None) -> Path:
    out = dest / name
    gh = _gh()
    if gh:
        r = subprocess.run([gh, "release", "download", rel.tag, "-R", REPO, "-p", name, "-D", str(dest),
                            "--clobber"], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and out.is_file():
            return out

    def on_bytes(done: int, total: int) -> None:
        if progress:
            progress(f"Downloading Glacier {rel.version}", done / total if total else -1)

    try:
        return download.download(url, out, on_bytes, cancel)
    except requests.HTTPError as e:
        raise UpdateError(f"Couldn't download {name} from {rel.url}: {e}") from None


def _expected_sha256(sums: Path, name: str) -> str:
    for line in sums.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    raise UpdateError(f"{SUMS_NAME} has no checksum for {name}")


def _unpack(tarball: Path, dest: Path, version: str) -> Path:
    with tarfile.open(tarball, "r:gz") as tf:
        tops = {m.name.split("/", 1)[0] for m in tf.getmembers()}
        if tops != {f"glacier-{version}"}:
            raise UpdateError(f"Unexpected layout in {tarball.name}: {', '.join(sorted(tops))}")
        tf.extractall(dest, filter="data")      # refuses absolute paths, .. and device files
    root = dest / f"glacier-{version}"
    init = root / "glacier" / "__init__.py"
    m = re.search(r'__version__ = "([^"]+)"', init.read_text() if init.is_file() else "")
    if not (root / "install.sh").is_file() or not m or m.group(1) != version:
        raise UpdateError(f"{tarball.name} doesn't contain Glacier {version}")
    return root


def install(rel: Release, progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> str:
    """Download, verify and install a release over the running copy. Returns the new version."""
    ok, why = can_self_update()
    if not ok:
        raise UpdateError(why)
    prefix = install_prefix()
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="selfupdate-", dir=paths.CACHE_DIR) as d:
        work = Path(d)
        if progress:
            progress(f"Downloading Glacier {rel.version}", -1)
        tarball = _fetch_asset(rel, rel.asset_name, rel.asset_url, work, progress, cancel)
        sums = _fetch_asset(rel, SUMS_NAME, rel.sums_url, work, None, cancel)
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != _expected_sha256(sums, rel.asset_name):
            raise UpdateError(f"Checksum mismatch for {rel.asset_name}; not installing it")
        if cancel is not None and cancel.is_set():
            raise download.Cancelled()
        if progress:
            progress(f"Installing Glacier {rel.version}", -1)
        root = _unpack(tarball, work / "src", rel.version)
        env = dict(os.environ, PREFIX=str(prefix))
        r = subprocess.run(["bash", str(root / "install.sh")], cwd=root, env=env, capture_output=True,
                           text=True, timeout=600, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
            raise UpdateError(f"install.sh for Glacier {rel.version} failed:\n{tail}")
    state = _load_state()
    state["installed"] = rel.version
    _save_state(state)
    if progress:
        progress(f"Glacier {rel.version} installed", 1.0)
    return rel.version
