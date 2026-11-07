"""HTTP helpers: resumable downloads with progress and cancellation."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from collections.abc import Callable
from urllib.parse import urlsplit

import requests

from . import USER_AGENT

ProgressFn = Callable[[int, int], None]   # (bytes_done, bytes_total or 0)


class Cancelled(Exception):
    pass


def describe_error(e: BaseException) -> str:
    """Error text for the UI: a short sentence for network failures instead of urllib3's
    'HTTPSConnectionPool(...): Max retries exceeded ...', anything else as it is."""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        req = getattr(e, "request", None)
        host = urlsplit(getattr(req, "url", None) or "").hostname or "the server"
        if isinstance(e, requests.Timeout):     # ConnectTimeout is a ConnectionError too
            return f"{host} didn't answer in time. Check your internet connection and try again."
        text = str(e)
        if any(s in text for s in ("NameResolutionError", "Name or service not known",
                                   "Temporary failure in name resolution", "No address associated")):
            return f"Couldn't look up {host}. Check your internet connection and try again."
        return f"Couldn't connect to {host}. Check your internet connection and try again."
    return str(e) or e.__class__.__name__


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get_text(url: str, timeout: float = 15) -> str:
    with session() as s:
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text


def get_json(url: str, timeout: float = 15):
    with session() as s:
        r = s.get(url, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return r.json()


def download(url: str, dest: Path, progress: ProgressFn | None = None,
             cancel: threading.Event | None = None, chunk: int = 1 << 20) -> Path:
    """Download url to dest, resuming a previous .part file when the server allows it."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}

    with session() as s, s.get(url, stream=True, timeout=(15, 60), headers=headers,
                               allow_redirects=True) as r:
        if r.status_code == 416:          # .part already complete
            os.replace(part, dest)
            return dest
        r.raise_for_status()
        if have and r.status_code != 206:  # server ignored Range: start over
            have = 0
        total = int(r.headers.get("Content-Length", 0))
        if total and r.status_code == 206:
            total += have
        mode = "ab" if have else "wb"
        done = have
        with open(part, mode) as fh:
            for block in r.iter_content(chunk):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                fh.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
    os.replace(part, dest)
    return dest


def file_hash(path: Path, algo: str = "sha512") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
