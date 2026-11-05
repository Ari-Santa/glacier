"""Readers for Battle.net data: product.db (protobuf), .build.info (BPSV) and version servers."""

from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from . import download, paths

# --------------------------------------------------------------------------- protobuf wire format

Fields = dict[int, list]


def decode_message(buf: bytes) -> Fields:
    """Decode one protobuf message into {field_number: [raw values]} (schema-less)."""
    out: Fields = {}
    i, n = 0, len(buf)

    def varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            if i >= n:
                raise ValueError("truncated varint")
            b = buf[i]
            i += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 70:
                raise ValueError("varint too long")

    while i < n:
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            val: object = varint()
        elif wire == 1:
            if i + 8 > n:
                raise ValueError("truncated fixed64")
            val = buf[i:i + 8]
            i += 8
        elif wire == 2:
            length = varint()
            if i + length > n:
                raise ValueError("truncated bytes")
            val = buf[i:i + length]
            i += length
        elif wire == 5:
            if i + 4 > n:
                raise ValueError("truncated fixed32")
            val = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(num, []).append(val)
    return out


def _str(f: Fields, num: int) -> str:
    v = f.get(num)
    if not v or not isinstance(v[-1], (bytes, bytearray)):
        return ""
    return bytes(v[-1]).decode("utf-8", "replace")


def _bool(f: Fields, num: int) -> bool:
    v = f.get(num)
    return bool(v and isinstance(v[-1], int) and v[-1])


def _int(f: Fields, num: int) -> int:
    v = f.get(num)
    return v[-1] if v and isinstance(v[-1], int) else 0


def _double(f: Fields, num: int) -> float | None:
    v = f.get(num)
    if v and isinstance(v[-1], (bytes, bytearray)) and len(v[-1]) == 8:
        return struct.unpack("<d", v[-1])[0]
    return None


def _msg(f: Fields, num: int) -> Fields:
    v = f.get(num)
    if not v or not isinstance(v[-1], (bytes, bytearray)):
        return {}
    try:
        return decode_message(bytes(v[-1]))
    except ValueError:
        return {}


# --------------------------------------------------------------------------- product.db

@dataclass
class ProductInstall:
    uid: str
    product_code: str
    install_path: str           # Windows-style path as written by the Agent
    play_region: str
    installed: bool
    playable: bool
    update_complete: bool
    version: str
    progress: float | None      # 0..1 while downloading/updating, else None
    total_bytes: int = 0
    remaining_bytes: int = 0


def read_product_db(path: Path) -> list[ProductInstall]:
    """Parse the Battle.net Agent's product.db.

    Layout (reverse engineered, matches Lutris' reader):
      Database { repeated ProductInstall product_install = 1; }
      ProductInstall { uid=1; product_code=2; UserSettings settings=3; CachedProductState state=4; }
      UserSettings { install_path=1; play_region=2; }
      CachedProductState { BaseProductState base=1; ...; UpdateProgress update_progress=4; }
      BaseProductState { installed=1; playable=2; update_complete=3; current_version_str=7; }
      UpdateProgress { last_disc_set_used=1; double progress=2; total_to_download=4; download_remaining=5 }
    """
    data = Path(path).read_bytes()
    db = decode_message(data)
    result = []
    for raw in db.get(1, []):
        if not isinstance(raw, (bytes, bytearray)):
            continue
        try:
            p = decode_message(bytes(raw))
        except ValueError:
            continue
        settings = _msg(p, 3)
        state = _msg(p, 4)
        base = _msg(state, 1)
        upd = _msg(state, 4)
        progress = _double(upd, 2)
        update_complete = _bool(base, 3)
        if progress is not None and (update_complete or progress >= 1.0 or progress < 0):
            progress = None
        result.append(ProductInstall(
            uid=_str(p, 1),
            product_code=_str(p, 2),
            install_path=_str(settings, 1),
            play_region=_str(settings, 2),
            installed=_bool(base, 1),
            playable=_bool(base, 2),
            update_complete=update_complete,
            version=_str(base, 7),
            progress=progress,
            total_bytes=_int(upd, 4),
            remaining_bytes=_int(upd, 5),
        ))
    return result


# --------------------------------------------------------------------------- BPSV (.build.info, versions)

def parse_bpsv(text: str) -> list[dict[str, str]]:
    """Parse Blizzard's pipe-separated tables: header `Name!TYPE:n|...`, `##` comments."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        line = line.strip("\r\n")
        if not line.strip() or line.startswith("##"):
            continue
        cols = line.split("|")
        if header is None:
            header = [c.split("!", 1)[0].strip() for c in cols]
            continue
        rows.append({h: (cols[i] if i < len(cols) else "") for i, h in enumerate(header)})
    return rows


@dataclass
class BuildInfo:
    product: str
    branch: str
    version: str
    build_key: str
    active: bool


def read_build_info(install_dir: Path) -> list[BuildInfo]:
    f = Path(install_dir) / ".build.info"
    try:
        rows = parse_bpsv(f.read_text(errors="replace"))
    except OSError:
        return []
    out = []
    for r in rows:
        out.append(BuildInfo(
            product=r.get("Product", ""),
            branch=r.get("Branch", ""),
            version=r.get("Version", ""),
            build_key=r.get("Build Key", "").lower(),
            active=r.get("Active", "1") != "0",
        ))
    return out


@dataclass
class RemoteVersion:
    region: str
    build_config: str
    build_id: str
    version: str


_VERSION_CACHE = paths.CACHE_DIR / "versions.json"
_VERSION_TTL = 15 * 60


def remote_versions(product: str, use_cache: bool = True) -> list[RemoteVersion]:
    """Current live builds for a product from Blizzard's public version service."""
    cache: dict = {}
    if use_cache:
        try:
            cache = json.loads(_VERSION_CACHE.read_text())
            entry = cache.get(product)
            if entry and time.time() - entry["t"] < _VERSION_TTL:
                return [RemoteVersion(**v) for v in entry["v"]]
        except (OSError, ValueError, KeyError, TypeError):
            cache = {}
    text = None
    for url in (f"https://us.version.battle.net/v2/products/{product}/versions",
                f"http://us.patch.battle.net:1119/{product}/versions"):
        try:
            text = download.get_text(url, timeout=10)
            break
        except Exception:
            continue
    if text is None:
        raise RuntimeError(f"Could not reach Blizzard version servers for {product}")
    versions = [RemoteVersion(r.get("Region", ""), r.get("BuildConfig", "").lower(),
                              r.get("BuildId", ""), r.get("VersionsName", ""))
                for r in parse_bpsv(text)]
    try:
        cache[product] = {"t": time.time(), "v": [v.__dict__ for v in versions]}
        _VERSION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _VERSION_CACHE.write_text(json.dumps(cache))
    except OSError:
        pass
    return versions


def latest_for_region(versions: list[RemoteVersion], region: str) -> RemoteVersion | None:
    region = (region or "us").lower()
    for v in versions:
        if v.region == region:
            return v
    for v in versions:
        if v.region == "us":
            return v
    return versions[0] if versions else None
