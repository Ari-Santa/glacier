"""Minimal PE reader: pull icons and version info out of Windows executables.

Reads only headers and the resource section (seek-based), so it is cheap even on
multi-gigabyte game binaries.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

RT_ICON = 3
RT_GROUP_ICON = 14
RT_VERSION = 16


class PEError(Exception):
    pass


@dataclass
class _Section:
    va: int
    vsize: int
    raw_ptr: int
    raw_size: int


@dataclass
class _ResEntry:
    key: int | str
    data_rva: int | None = None
    size: int = 0
    children: list[_ResEntry] | None = None


class PEFile:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fh: BinaryIO = open(self.path, "rb")
        try:
            self._parse_headers()
            self._resources = self._parse_resources()
        except Exception:
            self._fh.close()
            raise

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read(self, off: int, n: int) -> bytes:
        self._fh.seek(off)
        data = self._fh.read(n)
        if len(data) != n:
            raise PEError("unexpected end of file")
        return data

    def _parse_headers(self) -> None:
        dos = self._read(0, 64)
        if dos[:2] != b"MZ":
            raise PEError("not an MZ executable")
        pe_off = struct.unpack_from("<I", dos, 0x3C)[0]
        if self._read(pe_off, 4) != b"PE\0\0":
            raise PEError("missing PE signature")
        coff = self._read(pe_off + 4, 20)
        nsect, opt_size = struct.unpack_from("<H", coff, 2)[0], struct.unpack_from("<H", coff, 16)[0]
        opt_off = pe_off + 24
        opt = self._read(opt_off, opt_size)
        magic = struct.unpack_from("<H", opt, 0)[0]
        if magic == 0x10B:
            dd_off, ndd_off = 96, 92
        elif magic == 0x20B:
            dd_off, ndd_off = 112, 108
        else:
            raise PEError(f"unknown optional header magic {magic:#x}")
        ndd = struct.unpack_from("<I", opt, ndd_off)[0]
        self.resource_rva = self.resource_size = 0
        if ndd > 2 and dd_off + 24 <= len(opt):
            self.resource_rva, self.resource_size = struct.unpack_from("<II", opt, dd_off + 16)
        self.sections: list[_Section] = []
        sec_off = opt_off + opt_size
        for i in range(nsect):
            s = self._read(sec_off + 40 * i, 40)
            vsize, va, raw_size, raw_ptr = struct.unpack_from("<IIII", s, 8)
            self.sections.append(_Section(va, vsize, raw_ptr, raw_size))

    def rva_to_offset(self, rva: int) -> int:
        for s in self.sections:
            if s.va <= rva < s.va + max(s.vsize, s.raw_size):
                return rva - s.va + s.raw_ptr
        raise PEError(f"RVA {rva:#x} outside all sections")

    def _parse_resources(self) -> list[_ResEntry]:
        if not self.resource_rva:
            return []
        base = self.rva_to_offset(self.resource_rva)
        seen: set[int] = set()

        def read_dir(off: int, depth: int) -> list[_ResEntry]:
            if depth > 3 or off in seen:
                return []
            seen.add(off)
            hdr = self._read(base + off, 16)
            n_named, n_id = struct.unpack_from("<HH", hdr, 12)
            entries = []
            raw = self._read(base + off + 16, 8 * (n_named + n_id))
            for i in range(n_named + n_id):
                name, target = struct.unpack_from("<II", raw, 8 * i)
                if name & 0x80000000:
                    name_off = base + (name & 0x7FFFFFFF)
                    length = struct.unpack("<H", self._read(name_off, 2))[0]
                    key: int | str = self._read(name_off + 2, length * 2).decode("utf-16-le", "replace")
                else:
                    key = name & 0xFFFF
                if target & 0x80000000:
                    entries.append(_ResEntry(key, children=read_dir(target & 0x7FFFFFFF, depth + 1)))
                else:
                    rva, size = struct.unpack("<II", self._read(base + target, 8))
                    entries.append(_ResEntry(key, data_rva=rva, size=size))
            return entries

        return read_dir(0, 0)

    def _type(self, rtype: int) -> list[_ResEntry]:
        for e in self._resources:
            if e.key == rtype and e.children:
                return e.children
        return []

    def _first_data(self, entry: _ResEntry) -> bytes | None:
        """Data of the first language variant below a name/id entry."""
        node = entry
        while node.children:
            node = node.children[0]
        if node.data_rva is None or node.size <= 0 or node.size > 64 << 20:
            return None
        return self._read(self.rva_to_offset(node.data_rva), node.size)

    # ---- icons -------------------------------------------------------------
    def icon_groups(self) -> list[int | str]:
        return [e.key for e in self._type(RT_GROUP_ICON)]

    def group_icon(self, group: int | str | None = None) -> bytes | None:
        """Rebuild a .ico file from an RT_GROUP_ICON (first group when group is None)."""
        groups = self._type(RT_GROUP_ICON)
        if not groups:
            return None
        entry = groups[0] if group is None else next((g for g in groups if g.key == group), None)
        if entry is None:
            return None
        grp = self._first_data(entry)
        if not grp or len(grp) < 6:
            return None
        _, gtype, count = struct.unpack_from("<HHH", grp, 0)
        icons = {e.key: e for e in self._type(RT_ICON)}
        images: list[tuple[bytes, bytes]] = []
        for i in range(count):
            off = 6 + 14 * i
            if off + 14 > len(grp):
                break
            head = grp[off:off + 12]          # w, h, colors, reserved, planes, bitcount, bytes
            icon_id = struct.unpack_from("<H", grp, off + 12)[0]
            if icon_id not in icons:
                continue
            data = self._first_data(icons[icon_id])
            if data:
                images.append((head[:8], data))
        if not images:
            return None
        out = io.BytesIO()
        out.write(struct.pack("<HHH", 0, 1, len(images)))
        offset = 6 + 16 * len(images)
        for head, data in images:
            out.write(head + struct.pack("<II", len(data), offset))
            offset += len(data)
        for _, data in images:
            out.write(data)
        return out.getvalue()

    # ---- version info --------------------------------------------------------
    def version_info(self) -> dict[str, str]:
        entries = self._type(RT_VERSION)
        if not entries:
            return {}
        data = self._first_data(entries[0])
        if not data:
            return {}
        info: dict[str, str] = {}
        # VS_FIXEDFILEINFO starts with signature 0xFEEF04BD
        sig = data.find(struct.pack("<I", 0xFEEF04BD))
        if sig >= 0 and sig + 16 <= len(data):
            ms, ls = struct.unpack_from("<II", data, sig + 8)
            info["FileVersion"] = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        # Pull common StringFileInfo values by scanning UTF-16 keys.
        for key in ("ProductName", "FileDescription", "ProductVersion", "CompanyName"):
            k = key.encode("utf-16-le") + b"\0\0"
            pos = data.find(k)
            if pos < 0:
                continue
            pos += len(k)
            pos += (-pos) % 4                  # value is 32-bit aligned
            end = pos
            while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            value = data[pos:end].decode("utf-16-le", "replace").strip()
            if value:
                info[key] = value
        return info


def extract_icon(exe: Path | str) -> bytes | None:
    try:
        with PEFile(exe) as pe:
            return pe.group_icon()
    except (OSError, PEError, struct.error):
        return None


def version_info(exe: Path | str) -> dict[str, str]:
    try:
        with PEFile(exe) as pe:
            return pe.version_info()
    except (OSError, PEError, struct.error):
        return {}
