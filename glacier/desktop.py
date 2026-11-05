"""Icons and freedesktop .desktop entries."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import APP_NAME, paths

ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256)


def icon_name(uid: str) -> str:
    return f"glacier-{uid}"


def desktop_id(uid: str) -> str:
    return f"glacier-{uid}.desktop"


# --------------------------------------------------------------------------- icon rendering

def _best_frame(ico: Image.Image, size: int) -> Image.Image:
    sizes = sorted(ico.info.get("sizes", {ico.size}), key=lambda s: s[0])
    pick = next((s for s in sizes if s[0] >= size), sizes[-1])
    ico.size = pick
    ico.load()
    return ico.convert("RGBA")


def ico_to_images(ico_bytes: bytes) -> dict[int, Image.Image]:
    """Return RGBA images for each standard size from .ico data."""
    src = Image.open(io.BytesIO(ico_bytes))
    out = {}
    for s in ICON_SIZES:
        frame = _best_frame(src, s)
        if frame.size != (s, s):
            frame = frame.resize((s, s), Image.LANCZOS)
        out[s] = frame
    return out


def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _font(size: int) -> ImageFont.ImageFont:
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
              "/usr/share/fonts/noto/NotoSans-Bold.ttf",
              "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def placeholder(initials: str, color: str, size: int = 256) -> Image.Image:
    """A rounded gradient tile with initials, for games without an extracted icon."""
    r, g, b = _hex(color)
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    grad = Image.new("RGBA", (big, big))
    px = grad.load()
    for y in range(big):
        t = y / big
        top = (min(255, int(r * 1.25 + 20)), min(255, int(g * 1.25 + 20)), min(255, int(b * 1.25 + 20)))
        bot = (int(r * 0.45), int(g * 0.45), int(b * 0.45))
        row = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,)
        for x in range(big):
            px[x, y] = row
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 5, fill=255)
    img.paste(grad, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    text = initials[:4]
    font = _font(int(big * (0.42 if len(text) <= 2 else 0.30 if len(text) == 3 else 0.24)))
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    pos = ((big - w) / 2 - box[0], (big - h) / 2 - box[1])
    draw.text((pos[0], pos[1] + big * 0.012), text, font=font, fill=(0, 0, 0, 90))
    draw.text(pos, text, font=font, fill=(255, 255, 255, 240))
    return img.resize((size, size), Image.LANCZOS)


# --------------------------------------------------------------------------- icon install

def install_icon_images(name: str, images: dict[int, Image.Image]) -> Path:
    """Write images into the user's hicolor theme and Glacier's own icon dir; returns the 256px PNG."""
    for s, im in images.items():
        d = paths.HICOLOR_DIR / f"{s}x{s}" / "apps"
        d.mkdir(parents=True, exist_ok=True)
        im.save(d / f"{name}.png")
    paths.ICONS_DIR.mkdir(parents=True, exist_ok=True)
    big = paths.ICONS_DIR / f"{name}.png"
    images[max(images)].save(big)
    return big


def install_icon_from_ico(name: str, ico_bytes: bytes) -> Path:
    return install_icon_images(name, ico_to_images(ico_bytes))


def install_placeholder_icon(name: str, initials: str, color: str) -> Path:
    base = placeholder(initials, color, 256)
    images = {s: base.resize((s, s), Image.LANCZOS) if s != 256 else base for s in ICON_SIZES}
    return install_icon_images(name, images)


def remove_icon(name: str) -> None:
    for s in ICON_SIZES:
        (paths.HICOLOR_DIR / f"{s}x{s}" / "apps" / f"{name}.png").unlink(missing_ok=True)
    (paths.ICONS_DIR / f"{name}.png").unlink(missing_ok=True)


def icon_png(name: str) -> Path | None:
    p = paths.ICONS_DIR / f"{name}.png"
    return p if p.exists() else None


# --------------------------------------------------------------------------- .desktop entries

def glacier_command() -> list[str]:
    """How desktop entries should invoke Glacier."""
    installed = paths.LOCAL_BIN / "glacier"
    if installed.exists():
        return [str(installed)]
    found = shutil.which("glacier")
    if found:
        return [found]
    return ["env", f"PYTHONPATH={paths.PACKAGE_DIR.parent}", sys.executable, "-m", "glacier"]


def _quote_exec(arg: str) -> str:
    """Quote one argument per the Desktop Entry spec."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def _escape_value(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def write_entry(file_id: str, name: str, args: list[str], icon: str, *, comment: str = "",
                wm_class: str = "", categories: str = "Game;", keywords: str = "",
                actions: dict[str, tuple[str, list[str]]] | None = None,
                extra_env: dict[str, str] | None = None) -> Path:
    cmd = glacier_command() + args
    exec_line = " ".join(_quote_exec(a).replace("%", "%%") for a in cmd)
    if extra_env:
        exec_line = "env " + " ".join(_quote_exec(f"{k}={v}") for k, v in extra_env.items()) + " " + exec_line
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        f"Name={_escape_value(name)}",
    ]
    if comment:
        lines.append(f"Comment={_escape_value(comment)}")
    lines += [
        f"Exec={exec_line}",
        f"Icon={icon}",
        "Terminal=false",
        f"Categories={categories}",
        "StartupNotify=true",
        "X-Glacier-Managed=true",
    ]
    if wm_class:
        lines.append(f"StartupWMClass={wm_class}")
    if keywords:
        lines.append(f"Keywords={keywords}")
    if actions:
        lines.append("Actions=" + ";".join(actions) + ";")
    for key, (label, a_args) in (actions or {}).items():
        a_exec = " ".join(_quote_exec(a).replace("%", "%%") for a in glacier_command() + a_args)
        lines += ["", f"[Desktop Action {key}]", f"Name={_escape_value(label)}", f"Exec={a_exec}"]
    paths.APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = paths.APPLICATIONS_DIR / file_id
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)
    return path


def remove_entry(file_id: str) -> None:
    (paths.APPLICATIONS_DIR / file_id).unlink(missing_ok=True)


def entry_exists(file_id: str) -> bool:
    return (paths.APPLICATIONS_DIR / file_id).exists()


def refresh() -> None:
    """Nudge menus and icon caches; silently skips tools that are missing."""
    cmds = [
        ["update-desktop-database", "-q", str(paths.APPLICATIONS_DIR)],
        ["gtk-update-icon-cache", "-q", "-t", "-f", str(paths.HICOLOR_DIR)],
        ["kbuildsycoca6", "--noincremental"] if shutil.which("kbuildsycoca6") else ["kbuildsycoca5"],
    ]
    for c in cmds:
        if shutil.which(c[0]):
            try:
                subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except OSError:
                pass


def app_entry_name() -> str:
    return APP_NAME


# --------------------------------------------------------------------------- autostart

AUTOSTART_FILE = paths.XDG_CONFIG / "autostart" / "glacier-battlenet.desktop"


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def set_autostart(enabled: bool) -> None:
    if not enabled:
        AUTOSTART_FILE.unlink(missing_ok=True)
        return
    exec_line = " ".join(_quote_exec(a) for a in glacier_command() + ["bnet", "--background"])
    AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART_FILE.write_text("\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Battle.net (Glacier)",
        "Comment=Start Battle.net minimised so games stay up to date",
        f"Exec={exec_line}", f"Icon={icon_name('battlenet')}", "Terminal=false",
        "X-GNOME-Autostart-Delay=20", "X-KDE-autostart-after=panel", "X-Glacier-Managed=true",
    ]) + "\n")


def install_app_entry() -> Path:
    """Glacier's own menu entry and icon."""
    for s in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512):
        src = paths.ASSETS_DIR / f"glacier-{s}.png"
        if src.exists():
            d = paths.HICOLOR_DIR / f"{s}x{s}" / "apps"
            d.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, d / "glacier.png")
    return write_entry("glacier.desktop", APP_NAME, [], "glacier",
                       comment="Battle.net launcher for Linux (Proton)", wm_class="glacier",
                       categories="Game;PackageManager;", keywords="blizzard;battle.net;proton;launcher;",
                       actions={"bnet": ("Open Battle.net", ["bnet"]),
                                "stop": ("Stop Battle.net and games", ["kill"])})
