"""Window tagging over Xlib (ctypes, no extra dependencies).

Proton names every window `steam_app_<SteamGameId>` (or `steam_proton`), and games
started by Battle.net inherit Battle.net's id, so desktops cannot tell the games apart
and show the wrong icon or group them all together. We fix that by tagging each game
window with the desktop file it belongs to:

  * `_KDE_NET_WM_DESKTOP_FILE` / `_GTK_APPLICATION_ID`: read by KWin (and others)
  * `WM_CLASS`: rewritten to `glacier-<uid>` to match `StartupWMClass` in our .desktop files
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading

_lib = None
_lock = threading.Lock()


def _load():
    global _lib
    if _lib is not None:
        return _lib or None
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        lib = ctypes.cdll.LoadLibrary(name)
    except OSError:
        _lib = False
        return None
    c = ctypes
    lib.XOpenDisplay.restype = c.c_void_p
    lib.XOpenDisplay.argtypes = [c.c_char_p]
    lib.XCloseDisplay.argtypes = [c.c_void_p]
    lib.XInternAtom.restype = c.c_ulong
    lib.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]
    lib.XDefaultRootWindow.restype = c.c_ulong
    lib.XDefaultRootWindow.argtypes = [c.c_void_p]
    lib.XGetWindowProperty.restype = c.c_int
    lib.XGetWindowProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_long, c.c_long, c.c_int,
                                       c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_int),
                                       c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
                                       c.POINTER(c.POINTER(c.c_ubyte))]
    lib.XChangeProperty.argtypes = [c.c_void_p, c.c_ulong, c.c_ulong, c.c_ulong, c.c_int, c.c_int,
                                    c.c_char_p, c.c_int]
    lib.XFree.argtypes = [c.c_void_p]
    lib.XFlush.argtypes = [c.c_void_p]
    lib.XSync.argtypes = [c.c_void_p, c.c_int]
    _lib = lib
    return lib


# Windows vanish all the time; never let Xlib's default handler exit the process.
_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)


class X11:
    def __init__(self, display: str | None = None):
        lib = _load()
        if lib is None:
            raise OSError("libX11 not available")
        disp = display or os.environ.get("DISPLAY")
        if not disp:
            raise OSError("DISPLAY is not set")
        with _lock:
            lib.XSetErrorHandler(_ERROR_HANDLER)
        self.lib = lib
        self.dpy = lib.XOpenDisplay(disp.encode())
        if not self.dpy:
            raise OSError(f"cannot open X display {disp}")
        self.root = lib.XDefaultRootWindow(self.dpy)
        self._atoms: dict[str, int] = {}

    def close(self) -> None:
        if self.dpy:
            self.lib.XCloseDisplay(self.dpy)
            self.dpy = None

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.lib.XInternAtom(self.dpy, name.encode(), 0)
        return self._atoms[name]

    def _get(self, win: int, prop: str, max_len: int = 4096) -> tuple[int, int, bytes, int]:
        c = ctypes
        atype, afmt = c.c_ulong(), c.c_int()
        nitems, after = c.c_ulong(), c.c_ulong()
        data = c.POINTER(c.c_ubyte)()
        rc = self.lib.XGetWindowProperty(self.dpy, win, self.atom(prop), 0, max_len, 0, 0,  # AnyPropertyType
                                         c.byref(atype), c.byref(afmt), c.byref(nitems), c.byref(after),
                                         c.byref(data))
        if rc != 0 or not data:
            return 0, 0, b"", 0
        try:
            n = nitems.value
            fmt = afmt.value
            unit = {8: 1, 16: c.sizeof(c.c_short), 32: c.sizeof(c.c_long)}.get(fmt, 1)
            raw = c.string_at(data, n * unit)
        finally:
            self.lib.XFree(data)
        return atype.value, fmt, raw, n

    def _get_longs(self, win: int, prop: str) -> list[int]:
        _, fmt, raw, n = self._get(win, prop, 1 << 16)
        if fmt != 32 or not n:
            return []
        arr = (ctypes.c_ulong * n).from_buffer_copy(raw)
        return list(arr)

    def client_windows(self) -> list[int]:
        return self._get_longs(self.root, "_NET_CLIENT_LIST")

    def pid(self, win: int) -> int | None:
        v = self._get_longs(win, "_NET_WM_PID")
        return v[0] if v else None

    def string(self, win: int, prop: str) -> str:
        _, fmt, raw, _ = self._get(win, prop)
        return raw.decode("utf-8", "replace") if fmt == 8 else ""

    def wm_class(self, win: int) -> tuple[str, str]:
        parts = self.string(win, "WM_CLASS").split("\0")
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    def set_string(self, win: int, prop: str, value: str, type_name: str = "UTF8_STRING") -> None:
        data = value.encode()
        self.lib.XChangeProperty(self.dpy, win, self.atom(prop), self.atom(type_name), 8, 0, data, len(data))

    def tag(self, win: int, desktop_name: str, wm_class: str | None = None) -> None:
        """Associate a window with `<desktop_name>.desktop`."""
        self.set_string(win, "_KDE_NET_WM_DESKTOP_FILE", desktop_name)
        self.set_string(win, "_GTK_APPLICATION_ID", desktop_name)
        if wm_class:
            raw = f"{wm_class}\0{wm_class}\0"
            self.set_string(win, "WM_CLASS", raw, "STRING")
        self.lib.XFlush(self.dpy)

    def sync(self) -> None:
        self.lib.XSync(self.dpy, 0)
