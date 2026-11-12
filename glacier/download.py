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


class Incomplete(OSError):
    """The server closed the connection before the whole file had arrived."""


def _validator(headers) -> str:
    """What If-Range can name the file by: a strong ETag, else its Last-Modified date."""
    etag = headers.get("ETag", "")
    if etag and not etag.startswith("W/"):
        return etag
    return headers.get("Last-Modified", "")


def download(url: str, dest: Path, progress: ProgressFn | None = None,
             cancel: threading.Event | None = None, chunk: int = 1 << 20) -> Path:
    """Download url to dest, resuming a previous .part file when the server still has that file.

    Next to the .part sits a .part.tag with the ETag (or date) of the file it came from. A resume
    sends it as If-Range, so a "latest" link that moved on to a newer build restarts from zero
    instead of gluing two builds together.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    tag = dest.with_name(dest.name + ".part.tag")
    have = part.stat().st_size if part.exists() else 0
    try:
        known = tag.read_text().strip() if have else ""
    except (OSError, ValueError):
        known = ""
    if not known:
        have = 0                           # nothing says which file the .part belongs to
    headers = {"Accept-Encoding": "identity"}      # sizes and offsets are those of the file itself
    if have:
        headers.update({"Range": f"bytes={have}-", "If-Range": known})

    with session() as s, s.get(url, stream=True, timeout=(15, 60), headers=headers,
                               allow_redirects=True) as r:
        if r.status_code == 416:           # nothing left to send: the .part is complete, or too long
            size = r.headers.get("Content-Range", "").rpartition("/")[2]
            if have and size.isdigit() and int(size) == have:
                os.replace(part, dest)
                tag.unlink(missing_ok=True)
                return dest
            part.unlink(missing_ok=True)
            tag.unlink(missing_ok=True)
            if not have:
                r.raise_for_status()
            return download(url, dest, progress, cancel, chunk)     # once more, from the start
        r.raise_for_status()
        if have and r.status_code != 206:  # no Range support, or the file changed: start over
            have = 0
        total = int(r.headers.get("Content-Length", 0))
        if total and r.status_code == 206:
            total += have
        if not have:
            known = _validator(r.headers)
            if known:
                tag.write_text(known)
            else:
                tag.unlink(missing_ok=True)
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
        if total and done != total and not r.headers.get("Content-Encoding"):
            raise Incomplete(f"The download of {dest.name} stopped early ({done} of {total} bytes). Try again.")
    os.replace(part, dest)
    tag.unlink(missing_ok=True)
    return dest


def file_hash(path: Path, algo: str = "sha512") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
