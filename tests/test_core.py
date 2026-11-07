"""Unit tests for Glacier's parsers and helpers (no Proton or network needed)."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/glacier-test/config")
os.environ.setdefault("XDG_DATA_HOME", "/tmp/glacier-test/data")
os.environ.setdefault("XDG_STATE_HOME", "/tmp/glacier-test/state")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/glacier-test/cache")

from glacier import blizzard, desktop, games, pe, vdf  # noqa: E402
from glacier.config import Config  # noqa: E402
from glacier.engine import Engine  # noqa: E402
from glacier.library import _split_windows_cmdline, find_main_exe  # noqa: E402


# ----------------------------------------------------------------------------- VDF

def test_vdf_nested_and_case_insensitive():
    text = '''
    "libraryfolders"
    {
        // comment
        "0" { "path" "/home/u/.steam/steam" "apps" { "228980" "1" } }
        "1" { "path" "/mnt/games/Steam Library" }
    }'''
    data = vdf.loads(text)
    lf = data.find("LibraryFolders")
    assert lf["0"]["path"] == "/home/u/.steam/steam"
    assert lf["1"]["path"] == "/mnt/games/Steam Library"
    assert lf["0"]["apps"]["228980"] == "1"


def test_vdf_escapes():
    data = vdf.loads(r'"a" { "b" "say \"hi\"\n" }')
    assert data["a"]["b"] == 'say "hi"\n'


# ----------------------------------------------------------------------------- BPSV

def test_bpsv_versions():
    text = ("Region!STRING:0|BuildConfig!HEX:16|CDNConfig!HEX:16|KeyRing!HEX:16|BuildId!DEC:4|"
            "VersionsName!String:0|ProductConfig!HEX:16\n## seqn = 1\n"
            "us|AB|cd||70783|12.1.5.70783|ef\neu|AB|cd||70783|12.1.5.70783|ef\n")
    rows = blizzard.parse_bpsv(text)
    assert len(rows) == 2
    assert rows[0]["VersionsName"] == "12.1.5.70783"
    vs = [blizzard.RemoteVersion(r["Region"], r["BuildConfig"].lower(), r["BuildId"], r["VersionsName"])
          for r in rows]
    assert blizzard.latest_for_region(vs, "eu").region == "eu"
    assert blizzard.latest_for_region(vs, "kr").region == "us"


def test_build_info(tmp_path: Path):
    header = ("Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|CDN Key!HEX:16|Install Key!HEX:16|IM Size!DEC:4|"
              "CDN Path!STRING:0|CDN Hosts!STRING:0|CDN Servers!STRING:0|Tags!STRING:0|Armadillo!STRING:0|"
              "Last Activated!STRING:0|Version!STRING:0|KeyRing!HEX:16|Product!STRING:0")
    cols = header.count("|") + 1
    row = ["eu", "1", "ABC", "", "", "", "tpr/wow", "", "", "", "", "", "12.1.0.1234", "", "wow"]
    assert len(row) == cols
    (tmp_path / ".build.info").write_text(header + "\n" + "|".join(row) + "\n")
    info = blizzard.read_build_info(tmp_path)
    assert info[0].version == "12.1.0.1234"
    assert info[0].product == "wow"
    assert info[0].build_key == "abc"
    assert info[0].active


# ----------------------------------------------------------------------------- protobuf / product.db

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(num: int, value) -> bytes:
    if isinstance(value, bool) or isinstance(value, int):
        return _varint(num << 3 | 0) + _varint(int(value))
    if isinstance(value, float):
        return _varint(num << 3 | 1) + struct.pack("<d", value)
    if isinstance(value, str):
        value = value.encode()
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def _product(uid, code, path, version, installed=True, playable=True, progress=None, complete=True):
    settings = _field(1, path) + _field(2, "eu")
    base = _field(1, installed) + _field(2, playable) + _field(3, complete) + _field(7, version)
    state = _field(1, base)
    if progress is not None:
        state += _field(4, _field(2, progress) + _field(4, 1000) + _field(5, 400))
    return _field(1, _field(1, uid) + _field(2, code) + _field(3, settings) + _field(4, state))


def test_product_db(tmp_path: Path):
    db = (_product("battle.net", "bna", "C:/Program Files (x86)/Battle.net", "2.53.8.17986")
          + _product("wow_classic", "wow_classic", "D:/World of Warcraft", "5.5.4.1", complete=False, progress=0.42)
          + _product("prometheus", "pro", "C:/Program Files (x86)/Overwatch", "2.25.1.0.1"))
    f = tmp_path / "product.db"
    f.write_bytes(db)
    items = {p.uid: p for p in blizzard.read_product_db(f)}
    assert items["battle.net"].product_code == "bna"
    assert items["battle.net"].progress is None
    wc = items["wow_classic"]
    assert wc.install_path == "D:/World of Warcraft"
    assert wc.play_region == "eu"
    assert wc.progress == pytest.approx(0.42)
    assert wc.total_bytes == 1000 and wc.remaining_bytes == 400
    assert items["prometheus"].version == "2.25.1.0.1"
    assert games.lookup(items["prometheus"].product_code).name == "Overwatch 2"
    assert games.lookup("prometheus").uid == "pro"


def test_protobuf_rejects_garbage():
    with pytest.raises(ValueError):
        blizzard.decode_message(b"\x0a\xff\x01abc")


# ----------------------------------------------------------------------------- PE icons

def _png(size: int, color) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGBA", (size, size), color).save(b, "PNG")
    return b.getvalue()


def _build_pe(icons: dict[int, bytes]) -> bytes:
    """Craft a tiny PE32+ file whose .rsrc holds one RT_GROUP_ICON and the given RT_ICONs."""
    rsrc_rva, file_align = 0x1000, 0x200
    ids = sorted(icons)
    grp = struct.pack("<HHH", 0, 1, len(ids))
    for i in ids:
        w = 0 if icons[i][1] >= 256 else icons[i][1]
        grp += struct.pack("<BBBBHHIH", w, w, 0, 0, 1, 32, len(icons[i][0]), i)
    blobs = [("grp", 1, grp)] + [("ico", i, icons[i][0]) for i in ids]

    def directory(entries):          # entries: list of (id, offset_or_flagged)
        d = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entries))
        for ident, target in entries:
            d += struct.pack("<II", ident, target)
        return d

    # Layout: root dir, type dirs, name dirs, lang dirs, data entries, data
    root_size = 16 + 8 * 2
    type_size = lambda n: 16 + 8 * n  # noqa: E731
    off = root_size
    t_icon_off, off = off, off + type_size(len(ids))
    t_grp_off, off = off, off + type_size(1)
    name_offs = []
    for _ in blobs:
        name_offs.append(off)
        off += type_size(1)
    entry_offs = []
    for _ in blobs:
        entry_offs.append(off)
        off += 16
    data_offs = []
    for _, _, data in blobs:
        data_offs.append(off)
        off += (len(data) + 7) & ~7
    total = off
    buf = bytearray(total)
    HI = 0x80000000
    buf[0:root_size] = directory([(3, HI | t_icon_off), (14, HI | t_grp_off)])
    icon_entries = [(i, HI | name_offs[k + 1]) for k, i in enumerate(ids)]
    buf[t_icon_off:t_icon_off + type_size(len(ids))] = directory(icon_entries)
    buf[t_grp_off:t_grp_off + type_size(1)] = directory([(1, HI | name_offs[0])])
    for k, (_, _, data) in enumerate(blobs):
        buf[name_offs[k]:name_offs[k] + type_size(1)] = directory([(1033, entry_offs[k])])
        buf[entry_offs[k]:entry_offs[k] + 16] = struct.pack("<IIII", rsrc_rva + data_offs[k], len(data), 0, 0)
        buf[data_offs[k]:data_offs[k] + len(data)] = data

    raw_size = (total + file_align - 1) & ~(file_align - 1)
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0x22)
    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20B)
    struct.pack_into("<I", opt, 108, 16)                              # NumberOfRvaAndSizes
    struct.pack_into("<II", opt, 112 + 16, rsrc_rva, total)           # resource data directory
    sect = struct.pack("<8sIIIIIIHHI", b".rsrc", total, rsrc_rva, raw_size, file_align, 0, 0, 0, 0, 0x40000040)
    headers = bytes(dos) + b"PE\0\0" + coff + bytes(opt) + sect
    headers += b"\0" * (file_align - len(headers))
    return headers + bytes(buf) + b"\0" * (raw_size - total)


def test_pe_icon_roundtrip(tmp_path: Path):
    from PIL import Image
    exe = tmp_path / "Game.exe"
    exe.write_bytes(_build_pe({1: (_png(32, "red"), 32), 2: (_png(256, "blue"), 256)}))
    with pe.PEFile(exe) as f:
        assert f.icon_groups() == [1]
        ico = f.group_icon()
    im = Image.open(io.BytesIO(ico))
    assert set(im.info["sizes"]) == {(32, 32), (256, 256)}
    images = desktop.ico_to_images(ico)
    assert images[256].getpixel((10, 10))[:3] == (0, 0, 255)
    assert images[16].size == (16, 16)


def test_pe_rejects_non_pe(tmp_path: Path):
    f = tmp_path / "x.exe"
    f.write_bytes(b"not a pe file at all" * 10)
    assert pe.extract_icon(f) is None
    assert pe.version_info(f) == {}


# ----------------------------------------------------------------------------- desktop entries

def test_exec_quoting():
    assert desktop._quote_exec("launch") == "launch"
    assert desktop._quote_exec("/a b/c") == '"/a b/c"'
    assert desktop._quote_exec('x"y$z') == '"x\\"y\\$z"'


def _cache_file(cache: Path, data: bytes) -> str:
    digest = hashlib.md5(data).hexdigest()
    f = cache / digest[:2] / digest[2:4] / digest
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return digest


def _battlenet_icon_cache(cache: Path) -> None:
    """A Battle.net client cache with the World of Warcraft manifest and two big icons."""
    import json
    wow = _cache_file(cache, _png(275, (31, 44, 71, 255)))
    wowc = _cache_file(cache, _png(275, (0, 0, 0, 0)))
    manifest = {
        "fragment_id": "world_of_warcraft",
        "products": [
            {"id": "WoW", "base": {"icon_medium": "world_of_warcraft#WOW_ICON_MEDIUM",
                                   "icon_svg": "world_of_warcraft#WOW_ICON_SVG"}},
            {"id": "WoWC", "base": {"icon_svg": "world_of_warcraft#WOW_CLASSIC_MOP_ICON_SVG"}},
            {"id": "WoWF", "base": {"icon_svg": "world_of_warcraft#WOW_FOREVER_ICON_SVG"}},
        ],
        "files": {"default": {
            "world_of_warcraft#WOW_ICON_MASSIVE": {"hash": wow, "name": "resources/wow/icon_game_wow_massive.png"},
            "world_of_warcraft#WOWC_ICON_MASSIVE": {"hash": wowc, "name": "resources/wow/icon_game_wowc_massive.png"},
            "world_of_warcraft#WOWF_ICON_MASSIVE": {"hash": "0" * 32, "name": "not cached"},
        }},
    }
    _cache_file(cache, json.dumps(manifest).encode())
    _cache_file(cache, b"\x89PNG not json")


def test_product_icons_from_battlenet_cache(tmp_path: Path):
    from PIL import Image

    from glacier.battlenet import product_icons
    _battlenet_icon_cache(tmp_path)
    icons = product_icons(tmp_path)
    assert set(icons) == {"wow", "wowc"}            # WoWF's file isn't in the cache
    assert Image.open(icons["wow"]).size == (275, 275)
    assert product_icons(tmp_path / "missing") == {}


def test_game_icon_prefers_bigger_battlenet_art(engine: Engine, tmp_path: Path, monkeypatch):
    from PIL import Image

    from glacier import paths
    from glacier.battlenet import BattleNet
    from glacier.library import GameState, Library
    monkeypatch.setattr(paths, "HICOLOR_DIR", tmp_path / "hicolor")
    monkeypatch.setattr(paths, "ICONS_DIR", tmp_path / "icons")
    bnet = BattleNet(engine)
    _battlenet_icon_cache(bnet.cache_dir)
    root = tmp_path / "library" / "World of Warcraft"
    exe = root / "_retail_" / "Wow.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(_build_pe({1: (_png(48, (255, 0, 0, 255)), 48)}))
    lib = Library(engine, bnet)
    st = GameState(games.lookup("wow"), installed=True, install_dir=root, exe=exe)

    png = lib.ensure_icon(st)               # 48 px in the exe, 275 px from Battle.net: Battle.net wins
    im = Image.open(png)
    assert im.size == (256, 256)
    assert im.getpixel((128, 128))[:3] == (31, 44, 71)
    assert im.getpixel((0, 0))[3] == 0      # the opaque tile got rounded corners
    assert (tmp_path / "icons" / "glacier-wow.src").read_text().endswith("|" + bnet.product_icon("WoW").name)
    stamp = png.stat().st_mtime_ns
    assert lib.ensure_icon(st).stat().st_mtime_ns == stamp      # unchanged inputs: not rebuilt

    exe.write_bytes(_build_pe({1: (_png(256, (255, 0, 0, 255)), 256)}))
    png = lib.ensure_icon(st, force=True)   # a 256 px exe icon is kept over the art
    assert Image.open(png).getpixel((128, 128))[:3] == (255, 0, 0)


def test_placeholder_icon():
    im = desktop.placeholder("WoW", "#2c5aa0", 64)
    assert im.size == (64, 64)
    assert im.getpixel((0, 0))[3] == 0          # rounded corner is transparent
    assert im.getpixel((32, 5))[3] == 255


# ----------------------------------------------------------------------------- engine paths

@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    cfg = Config(tmp_path / "cfg.json")
    cfg["prefix"] = str(tmp_path / "compat")
    pfx = tmp_path / "compat" / "pfx"
    (pfx / "drive_c" / "Program Files (x86)" / "World of Warcraft").mkdir(parents=True)
    dd = pfx / "dosdevices"
    dd.mkdir()
    os.symlink("../drive_c", dd / "c:")
    os.symlink("/", dd / "z:")
    lib = tmp_path / "library"
    (lib / "Diablo IV").mkdir(parents=True)
    os.symlink(lib, dd / "d:")
    cfg["library_dir"] = str(lib)
    return Engine(cfg)


def test_win_to_unix(engine: Engine, tmp_path: Path):
    p = engine.win_to_unix("C:/Program Files (x86)/world of warcraft")
    assert p == engine.drive_c / "Program Files (x86)" / "World of Warcraft"
    assert engine.win_to_unix(r"D:\Diablo IV") == (tmp_path / "library" / "Diablo IV").resolve()
    assert engine.win_to_unix("Z:/etc") == Path("/etc")
    assert engine.win_to_unix("relative/path") is None


def test_unix_to_win(engine: Engine, tmp_path: Path):
    assert engine.unix_to_win(engine.drive_c / "Program Files (x86)") == r"C:\Program Files (x86)"
    assert engine.unix_to_win(tmp_path / "library" / "Diablo IV") == r"D:\Diablo IV"
    assert engine.unix_to_win(Path("/opt/x")) == r"Z:\opt\x"


def test_option_env_and_fingerprint(engine: Engine):
    engine.cfg["mangohud"] = True
    engine.cfg.set_game("wow", env={"FOO": "1"}, mangohud=False)
    assert engine.option_env(None)["MANGOHUD"] == "1"
    env_wow = engine.option_env("wow")
    assert "MANGOHUD" not in env_wow and env_wow["FOO"] == "1"


# ----------------------------------------------------------------------------- library helpers

def test_find_main_exe(tmp_path: Path):
    g = games.lookup("s2")
    root = tmp_path / "StarCraft II"
    for base in ("Base90000", "Base100000", "Base9999"):
        d = root / "Versions" / base
        d.mkdir(parents=True)
        (d / "SC2_x64.exe").write_bytes(b"MZ")
    assert find_main_exe(root, g).parent.name == "Base100000"
    # Heuristic fallback picks the biggest plausible exe, skipping crash handlers
    other = tmp_path / "Mystery"
    other.mkdir()
    (other / "BlizzardError.exe").write_bytes(b"x" * 5000)
    (other / "Game.exe").write_bytes(b"x" * 3000)
    (other / "tool.exe").write_bytes(b"x" * 10)
    g2 = games.GameDef("zz", "Mystery", "ZZ", "Mystery")
    assert find_main_exe(other, g2).name == "Game.exe"


def test_split_windows_cmdline():
    assert _split_windows_cmdline('"C:\\Program Files\\x.exe" --uid=wow --lang=enUS') == \
        ["C:\\Program Files\\x.exe", "--uid=wow", "--lang=enUS"]


def test_catalog_unique():
    uids = [g.uid for g in games.GAMES]
    assert len(uids) == len(set(uids))
    assert games.lookup("WTCG").uid == "hsb"
    assert games.lookup("hs_beta").uid == "hsb"
    assert games.lookup("world of warcraft").uid == "wow"
    assert games.lookup("wowf").name == "WoW Forever"


def test_wow_flavours_sharing_an_exe(engine: Engine, tmp_path: Path):
    """Retail and Forever both run Wow.exe; the flavour folder says which one is up."""
    from glacier.battlenet import BattleNet
    from glacier.engine import WineProc
    from glacier.library import BNET_UID, GameState, Library
    root = tmp_path / "library" / "World of Warcraft"
    lib = Library(engine, BattleNet(engine))
    lib.states = {uid: GameState(games.lookup(uid), installed=True, install_dir=root)
                  for uid in ("wow", "wowf", "wow_classic")}
    procs = [WineProc(1, "wow.exe", ["\\\\?\\D:\\World of Warcraft\\_forever_\\Wow.exe"]),
             WineProc(2, "wow.exe", ["D:/World of Warcraft/_forever_/Wow.exe", "--type=gpu-process"]),
             WineProc(3, "battle.net.exe", ["C:\\Program Files (x86)\\Battle.net\\Battle.net.exe"]),
             WineProc(4, "explorer.exe", ["C:\\windows\\system32\\explorer.exe"])]
    assert lib.owners(procs) == {1: "wowf", 2: "wowf", 3: BNET_UID}
    assert lib.refresh_running(procs) == {"wowf", BNET_UID}
    assert lib.states["wowf"].running and not lib.states["wow"].running

    procs[0] = WineProc(1, "wow.exe", ["D:\\World of Warcraft\\_retail_\\Wow.exe"])
    assert lib.owners(procs)[1] == "wow"
    # No folder in the path: first claimant, as before.
    procs[0] = WineProc(1, "wow.exe", ["Wow.exe"])
    assert lib.owners(procs)[1] == "wow"


def test_config_roundtrip(tmp_path: Path):
    c = Config(tmp_path / "c.json")
    c.update(mangohud=True)
    c.set_game("wow", launch_mode="direct")
    c2 = Config(tmp_path / "c.json")
    assert c2["mangohud"] is True
    assert c2.game("wow")["launch_mode"] == "direct"
    c2.set_game("wow", launch_mode="bnet")       # back to default -> entry removed
    assert "wow" not in Config(tmp_path / "c.json").data["games"]


# ----------------------------------------------------------------------------- Battle.net config

def _bnet_config(engine: Engine, data: dict):
    import json

    from glacier.battlenet import BattleNet
    bn = BattleNet(engine)
    bn.config_file.parent.mkdir(parents=True, exist_ok=True)
    bn.config_file.write_text(json.dumps(data))
    return bn


def test_logged_in_hint_top_level_client(engine: Engine):
    # Battle.net 2.5x keeps the remembered account in the top-level Client section.
    bn = _bnet_config(engine, {"Client": {"SavedAccountNames": "someone@example.com"}})
    assert bn.logged_in_hint()
    bn = _bnet_config(engine, {"5a61123b37cafce1": {"Services": {"LastLoginRegion": "US"}}})
    assert bn.logged_in_hint()
    bn = _bnet_config(engine, {"Client": {"Version": {"FirstRun": "false"}}})
    assert not bn.logged_in_hint()


def test_apply_config_default_install_path(engine: Engine):
    import json
    # Legacy Glacier key is replaced by the one Battle.net actually reads.
    bn = _bnet_config(engine, {"Client": {"DefaultInstallPath": "D:/", "AutoLogin": "true"}})
    bn.apply_config()
    client = json.loads(bn.config_file.read_text())["Client"]
    assert "DefaultInstallPath" not in client
    assert client["Install"]["DefaultInstallPath"] == "D:/"
    assert client["AutoLogin"] == "true"
    # A folder the user picked in Battle.net's settings is left alone.
    bn = _bnet_config(engine, {"Client": {"Install": {"DefaultInstallPath": "C:/Games/"}}})
    bn.apply_config()
    assert json.loads(bn.config_file.read_text())["Client"]["Install"]["DefaultInstallPath"] == "C:/Games/"


def test_copied_in_game_needs_locate(engine: Engine, tmp_path: Path):
    from glacier.battlenet import BattleNet
    from glacier.library import NOT_REGISTERED, GameState, Library, NotRegisteredError
    wow = games.lookup("wow")
    st = GameState(wow, installed=True, install_dir=tmp_path / "library" / "World of Warcraft",
                   version="12.1.0.69933", latest="12.1.5.70783")
    assert st.needs_locate and st.status_text == NOT_REGISTERED
    lib = Library(engine, BattleNet(engine))
    lib.states = {"wow": st}
    with pytest.raises(NotRegisteredError, match=r"D:\\World of Warcraft"):
        lib.launch("wow")
    st.db_uid = "wow"
    assert not st.needs_locate and st.status_text == "Update available"


def test_detect_region_offline():
    from glacier.battlenet import detect_region
    assert detect_region(geoip=False, tz="Europe/Oslo", lang="nb_NO.UTF-8") == "eu"
    assert detect_region(geoip=False, tz="America/Sao_Paulo", lang="pt_BR.UTF-8") == "us"
    assert detect_region(geoip=False, tz="Australia/Sydney", lang="en_AU.UTF-8") == "us"
    assert detect_region(geoip=False, tz="Asia/Seoul", lang="ko_KR.UTF-8") == "kr"
    assert detect_region(geoip=False, tz="Asia/Taipei", lang="zh_TW.UTF-8") == "tw"
    assert detect_region(geoip=False, tz="Asia/Dubai", lang="ar_AE.UTF-8") == "eu"
    # No timezone: fall back to the locale's country, then to us.
    assert detect_region(geoip=False, tz="", lang="de_DE.UTF-8") == "eu"
    assert detect_region(geoip=False, tz="", lang="en_US.UTF-8") == "us"
    assert detect_region(geoip=False, tz="", lang="C") == "us"


def test_login_region_and_setregion(engine: Engine):
    bn = _bnet_config(engine, {"5a61123b37cafce1": {"Services": {"LastLoginRegion": "EU"}}})
    assert bn.login_region() == "eu"
    assert bn.region() == "eu"
    engine.cfg["region"] = "us"
    assert bn.region() == "us"


def test_stale_prefix_after_crash(engine: Engine, monkeypatch):
    import time

    from glacier.battlenet import BattleNet
    from glacier.engine import WineProc
    bn = BattleNet(engine)
    # What a Battle.net crash leaves behind: Wine services, Agent and a CEF renderer.
    procs = [WineProc(1, "explorer.exe", ["C:\\windows\\system32\\explorer.exe", "/desktop"]),
             WineProc(2, "agent.exe", ["C:/ProgramData/Battle.net/Agent/Agent.9852/Agent.exe"]),
             WineProc(3, "battle.net.exe", ["Battle.net.exe", "--type=renderer"])]
    session = {"started": time.time() - 3 * 3600}
    monkeypatch.setattr(engine, "processes", lambda: procs)
    monkeypatch.setattr(engine, "wineserver_running", lambda: True)
    monkeypatch.setattr(bn, "_read_session", lambda: session)
    assert bn.stale_prefix()
    # Not while Battle.net itself, a game or an installer is running.
    for extra in ("battle.net.exe", "wow.exe", "battle.net-setup.exe"):
        procs.append(WineProc(9, extra, [extra]))
        assert not bn.stale_prefix(), extra
        procs.pop()
    # Not while a start Glacier just made is still coming up.
    session["started"] = time.time() - 5
    assert not bn.stale_prefix()
    session["started"] = time.time() - 3 * 3600

    # launch() clears the leftovers and starts fresh instead of joining them with `run`.
    killed = []
    monkeypatch.setattr(engine, "kill_all", lambda: killed.append(True))
    monkeypatch.setattr(engine, "fingerprint", lambda uid: "fp")
    monkeypatch.setattr(engine, "build", lambda exe, args, **kw: kw["verb"])
    monkeypatch.setattr(engine, "spawn", lambda spec: spec)
    monkeypatch.setattr(bn, "installed", lambda: True)
    monkeypatch.setattr(bn, "apply_config", lambda: None)
    monkeypatch.setattr(bn, "_write_session", lambda fp: None)
    assert bn.launch() == "auto" and killed == [True]

    monkeypatch.setattr(engine, "wineserver_running", lambda: False)
    assert not bn.stale_prefix()


# ----------------------------------------------------------------------------- CurseForge

def _fake_appimage(payload: bytes = b"hsqs" + bytes(60), interp: bool = True, magic: bytes = b"AI\x02") -> bytes:
    """ELF64 header + one program header, then the payload where the section headers end."""
    phoff, phentsize, shoff, shentsize, shnum = 64, 56, 120, 64, 2
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + magic + bytes(8 - len(magic))
    head = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, phoff, shoff, 0, 64,
                               phentsize, 1, shentsize, shnum, 0)
    ph = struct.pack("<I", 3 if interp else 1).ljust(phentsize, b"\0")
    elf = (head + ph).ljust(shoff + shentsize * shnum, b"\0")
    return elf + payload


def test_inspect_appimage(tmp_path: Path):
    from glacier.curseforge import inspect_appimage
    f = tmp_path / "a.AppImage"
    f.write_bytes(_fake_appimage())
    info = inspect_appimage(f)
    assert info.offset == 120 + 64 * 2 and not info.static_runtime
    f.write_bytes(_fake_appimage(interp=False))
    assert inspect_appimage(f).static_runtime
    for bad in (b"MZ" + bytes(100), _fake_appimage(magic=b"AI\x01"), _fake_appimage(payload=b"nope" * 20)):
        f.write_bytes(bad)
        with pytest.raises(ValueError):
            inspect_appimage(f)


def test_curseforge_desktop_parsing_and_host_check():
    from glacier import curseforge
    from glacier.engine import EngineError
    entry = curseforge.parse_desktop("[Desktop Entry]\nName=CurseForge\nExec=AppRun --no-sandbox %U\n"
                                     "X-AppImage-Version=1.325.0-41594.41594\n\n[Desktop Action x]\nName=Other\n")
    assert entry["Name"] == "CurseForge" and entry["Exec"] == "AppRun --no-sandbox %U"
    assert curseforge.short_version(entry["X-AppImage-Version"]) == "1.325.0-41594"
    curseforge.check_host("https://curseforge.overwolf.com/downloads/curseforge-latest-linux.AppImage")
    for url in ("http://curseforge.overwolf.com/x.AppImage", "https://curseforge.overwolf.com.evil.net/x",
                "https://example.com/curseforge.AppImage"):
        with pytest.raises(EngineError):
            curseforge.check_host(url)


@pytest.fixture
def cf_env(tmp_path: Path, monkeypatch):
    """CurseForge with menu/icon dirs in tmp_path and the network and AppImage tools faked."""
    from glacier import curseforge, download, paths
    monkeypatch.setattr(paths, "APPLICATIONS_DIR", tmp_path / "applications")
    monkeypatch.setattr(paths, "HICOLOR_DIR", tmp_path / "hicolor")
    monkeypatch.setattr(paths, "ICONS_DIR", tmp_path / "icons")
    monkeypatch.setattr(desktop, "refresh", lambda: None)
    monkeypatch.setattr(curseforge, "can_mount", lambda info: True)
    monkeypatch.setattr(curseforge, "openssl_override", lambda: "")
    meta = curseforge.Metadata({"Name": "CurseForge", "StartupWMClass": "CurseForge",
                                "MimeType": "x-scheme-handler/curseforge;x-scheme-handler/cfauth;",
                                "X-AppImage-Version": "1.325.0-41594.41594"}, _png(64, (240, 100, 54, 255)))
    monkeypatch.setattr(curseforge, "read_metadata", lambda path, info=None, timeout=120: meta)
    state = {"body": _fake_appimage(b"hsqs" + b"build-1"), "corrupt": False, "downloads": 0}

    def fake_remote(self):
        body = state["body"]
        return curseforge.Remote(curseforge.DOWNLOAD_URL, len(body), hashlib.md5(body).hexdigest(), "")

    def fake_download(url, dest, progress=None, cancel=None):
        state["downloads"] += 1
        Path(dest).write_bytes(b"\0" * len(state["body"]) if state["corrupt"] else state["body"])
        return dest

    monkeypatch.setattr(curseforge.CurseForge, "remote", fake_remote)
    monkeypatch.setattr(download, "download", fake_download)
    return curseforge.CurseForge(tmp_path / "apps dir"), state


def test_curseforge_install_update_remove(cf_env, tmp_path: Path, monkeypatch):
    from glacier import curseforge, paths
    from glacier.engine import EngineError
    cf, state = cf_env
    res = cf.install()
    assert res.changed and res.version == "1.325.0-41594" and cf.version() == "1.325.0-41594"
    assert cf.appimage.read_bytes() == state["body"] and os.access(cf.appimage, os.X_OK)
    entry = (paths.APPLICATIONS_DIR / curseforge.DESKTOP_ID).read_text()
    assert f'Exec="{cf.appimage}" --no-sandbox %U\n' in entry
    assert "MimeType=x-scheme-handler/curseforge;x-scheme-handler/cfauth;\n" in entry
    assert "StartupWMClass=CurseForge\n" in entry and "Icon=glacier-curseforge\n" in entry
    assert desktop.entry_managed(curseforge.DESKTOP_ID)
    assert (paths.HICOLOR_DIR / "256x256" / "apps" / "glacier-curseforge.png").is_file()

    # Same build on the server: nothing downloaded.
    assert not cf.install().changed and state["downloads"] == 1
    assert not cf.update_available()

    # A new build: update_available, then install replaces it.
    state["body"] = _fake_appimage(b"hsqs" + b"build-2")
    assert cf.update_available()
    assert cf.install().changed and cf.appimage.read_bytes() == state["body"]

    # A damaged download is retried once, then rejected; the installed build stays.
    good = cf.appimage.read_bytes()
    state["body"], state["corrupt"], state["downloads"] = _fake_appimage(b"hsqs" + b"build-3"), True, 0
    with pytest.raises(EngineError):
        cf.install()
    assert state["downloads"] == 2 and cf.appimage.read_bytes() == good
    state["corrupt"] = False

    # Without FUSE the entry makes the AppImage extract itself instead of mounting, and with
    # only OpenSSL 4 it points the addon agent at it.
    monkeypatch.setattr(curseforge, "can_mount", lambda info: False)
    monkeypatch.setattr(curseforge, "openssl_override", lambda: "4")
    cf.integrate()
    assert (f'Exec=env APPIMAGE_EXTRACT_AND_RUN=1 DOTNET_OPENSSL_VERSION_OVERRIDE=4 "{cf.appimage}" '
            "--no-sandbox %U\n") in (paths.APPLICATIONS_DIR / curseforge.DESKTOP_ID).read_text()

    cf.remove()
    assert not cf.installed() and not cf.state_file.exists()
    assert not desktop.entry_exists(curseforge.DESKTOP_ID)
    assert not (paths.HICOLOR_DIR / "256x256" / "apps" / "glacier-curseforge.png").exists()


def test_curseforge_openssl_override(tmp_path: Path, monkeypatch):
    from glacier import curseforge
    monkeypatch.delenv("DOTNET_OPENSSL_VERSION_OVERRIDE", raising=False)
    monkeypatch.delenv("CLR_OPENSSL_VERSION_OVERRIDE", raising=False)
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(curseforge, "LIB_DIRS", (str(lib),))
    assert curseforge.openssl_override() == ""          # no OpenSSL at all: nothing to point at
    (lib / "libssl.so.4").touch()
    assert curseforge.openssl_override() == "4"         # Ubuntu 26.10: only OpenSSL 4
    (lib / "libssl.so.3").touch()
    assert curseforge.openssl_override() == ""          # .NET finds OpenSSL 3 by itself
    (lib / "libssl.so.3").unlink()
    monkeypatch.setenv("DOTNET_OPENSSL_VERSION_OVERRIDE", "4")
    assert curseforge.openssl_override() == ""          # the user's own setting wins


def test_curseforge_keeps_foreign_menu_entry(cf_env):
    from glacier import curseforge, paths
    from glacier.engine import EngineError
    cf, state = cf_env
    foreign = paths.APPLICATIONS_DIR / curseforge.DESKTOP_ID
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Desktop Entry]\nName=CurseForge\nExec=/opt/CurseForge/curseforge %U\n")
    with pytest.raises(EngineError):
        cf.install()
    assert state["downloads"] == 0 and "/opt/CurseForge" in foreign.read_text()
    cf.remove()
    assert foreign.exists()


@pytest.mark.skipif(not shutil.which("mksquashfs") or not shutil.which("unsquashfs"),
                    reason="needs squashfs-tools")
def test_curseforge_read_metadata_from_squashfs(tmp_path: Path):
    from glacier import curseforge
    root = tmp_path / "root"
    for size, color in ((32, (0, 0, 255, 255)), (256, (255, 0, 0, 255))):
        d = root / "usr" / "share" / "icons" / "hicolor" / f"{size}x{size}" / "apps"
        d.mkdir(parents=True)
        (d / "curseforge.png").write_bytes(_png(size, color))
    (root / "curseforge.desktop").write_text("[Desktop Entry]\nName=CurseForge\nIcon=curseforge\n"
                                             "X-AppImage-Version=1.325.0-41594.41594\n")
    (root / "resources").mkdir()
    (root / "resources" / "app.asar").write_bytes(b"x" * 4096)
    sqfs = tmp_path / "img.sqfs"
    subprocess.run(["mksquashfs", str(root), str(sqfs), "-quiet", "-noappend", "-all-root"], check=True,
                   stdout=subprocess.DEVNULL)
    app = tmp_path / "CurseForge.AppImage"
    app.write_bytes(_fake_appimage(sqfs.read_bytes()))
    meta = curseforge.read_metadata(app)
    assert meta.entry["Name"] == "CurseForge" and meta.version == "1.325.0-41594"
    from PIL import Image
    assert Image.open(io.BytesIO(meta.icon)).size == (256, 256)       # the largest icon wins


def test_curseforge_version_types():
    from glacier.curseforge import version_type
    assert version_type("wow", "12.1.5.70783") == 517
    assert version_type("wowt", "") == 517 and version_type("wowxptr", "") == 517
    assert version_type("wow_beta", "") == 517
    assert version_type("wowf", "1.60.1.70790") == 88568
    assert version_type("wow_classic_era", "1.15.9.1") == 67408
    assert version_type("wow_classic_titan", "3.80.2.1") == 81212
    assert version_type("wow_classic", "5.5.4.1") == 79434         # progression Classic: by expansion
    assert version_type("wow_anniversary", "2.5.6.1") == 73246
    assert version_type("wow_classic", "") is None                 # unknown version: don't guess
    assert version_type("wow_classic", "9.0.1.1") is None
    assert version_type("d4", "1.0") is None


def test_scan_wow_flavours(tmp_path: Path):
    from glacier.curseforge import scan_wow_flavours
    root = tmp_path / "World of Warcraft"
    for folder, code in (("_retail_", "wow"), ("_ptr_", "wowt"), ("_classic_", "wow_classic")):
        (root / folder).mkdir(parents=True)
        (root / folder / ".flavor.info").write_text(f"Product Flavor!STRING:0\n{code}\n")
    (root / "_nothing_").mkdir()                                    # no .flavor.info: ignored
    (root / "Data").mkdir()
    (root / ".build.info").write_text("Branch!STRING:0|Version!STRING:0|Product!STRING:0\n"
                                      "eu|12.1.5.70783|wow\neu|5.5.4.1|wow_classic\n")
    found = {f.path.name: f for f in scan_wow_flavours({root: {"wow", "wow_classic"}})}
    assert set(found) == {"_retail_", "_ptr_", "_classic_"}
    assert found["_retail_"].installed and found["_retail_"].version == "12.1.5.70783"
    assert found["_classic_"].code == "wow_classic" and found["_classic_"].version == "5.5.4.1"
    assert not found["_ptr_"].installed and found["_ptr_"].code == "wowt"


def _cf_instance(guid: str, path: str, name: str, vtype: int = 517, addons=(), game: int = 1) -> dict:
    return {"guid": guid, "gameTypeID": game, "installPath": path, "name": name, "isValid": True,
            "isEnabled": True, "gameVersionTypeId": vtype, "installedAddons": list(addons),
            "preferenceAutoInstallUpdates": True, "syncProfile": {"GameInstanceGuid": guid}}


def test_curseforge_sync_instances(tmp_path: Path):
    import json

    from glacier.curseforge import Flavour, sync_instances
    root = tmp_path / "World of Warcraft"
    for d in ("_retail_", "_forever_", "_ptr_", "_xptr_", "_classic_", "_anniversary_"):
        (root / d).mkdir(parents=True)
    cfg = tmp_path / "CurseForge"
    inst = cfg / "agent" / "GameInstances" / "AddonGameInstance.json"
    inst.parent.mkdir(parents=True)
    original = [
        _cf_instance("g-forever", f"{root}/_forever_/", "Forever", 88568),
        _cf_instance("g-xptr", f"{root}/_xptr_/", "Retail PTR X"),                    # leftover
        _cf_instance("g-ptr", f"{root}/_ptr_/", "Retail PTR", addons=[{"id": 1}]),     # leftover with addons
        _cf_instance("g-gone", f"{root}/_beta_/", "Retail Beta"),                      # folder deleted
        _cf_instance("g-own", "/home/u/Wow/_retail_/", "My copy"),                     # not Battle.net's
        _cf_instance("g-eso", f"{root}/_xptr_/", "Other game", game=455),               # another game
    ]
    inst.write_text(json.dumps(original))
    storage = cfg / "storage.json"
    storage.write_text(json.dumps({"session-counter": "1", "internal-last-game-instances":
                                   json.dumps({"lastGameInstances": {"1": "g-xptr"}, "startupGameId": 1})}))
    flavours = [Flavour("wow", root / "_retail_", "12.1.5.70783", True),
                Flavour("wowf", root / "_forever_", "1.60.1.70790", True),
                Flavour("wowt", root / "_ptr_", "", False),
                Flavour("wowxptr", root / "_xptr_", "", False),
                Flavour("wow_classic", root / "_classic_", "5.5.4.1", True),
                Flavour("wow_anniversary", root / "_anniversary_", "", True)]

    res = sync_instances(flavours, cfg)
    assert [a.split(" (")[0] for a in res.added] == ["Retail", "Classic"]
    assert sorted(r.split(" (")[0] for r in res.removed) == ["Retail Beta", "Retail PTR X"]
    assert res.unknown and "wow_anniversary" in res.unknown[0]
    data = json.loads(inst.read_text())
    by_name = {e["name"]: e for e in data}
    assert set(by_name) == {"Forever", "Retail PTR", "My copy", "Other game", "Retail", "Classic"}
    retail = by_name["Retail"]
    assert retail["installPath"] == f"{root}/_retail_/" and retail["gameVersionTypeId"] == 517
    assert retail["installedAddons"] == [] and retail["syncProfile"]["GameInstanceGuid"] == retail["guid"]
    assert retail["guid"] not in {e["guid"] for e in original}
    assert by_name["Classic"]["gameVersionTypeId"] == 79434
    assert by_name["Forever"] == original[0]                       # existing entries untouched
    # The last-used WoW instance pointed at a removed leftover: now Retail.
    last = json.loads(json.loads(storage.read_text())["internal-last-game-instances"])
    assert last["lastGameInstances"]["1"] == retail["guid"]
    assert json.loads(inst.with_name(inst.name + ".glacier-backup").read_text()) == original

    # Nothing left to do the second time.
    again = sync_instances(flavours, cfg)
    assert not again.changed and json.loads(inst.read_text()) == data

    # A file Glacier doesn't understand is left alone.
    inst.write_text('{"not": "a list"}')
    assert sync_instances(flavours, cfg).skipped and inst.read_text() == '{"not": "a list"}'


def test_curseforge_sync_first_start(tmp_path: Path):
    import json

    from glacier.curseforge import Flavour, sync_instances
    # Before CurseForge ever ran, Glacier writes the list so it doesn't scan drives for leftovers.
    (tmp_path / "wow" / "_retail_").mkdir(parents=True)
    res = sync_instances([Flavour("wow", tmp_path / "wow" / "_retail_", "12.1.5.1", True)], tmp_path / "cf")
    data = json.loads((tmp_path / "cf" / "agent" / "GameInstances" / "AddonGameInstance.json").read_text())
    assert res.added and [e["name"] for e in data] == ["Retail"] and data[0]["gameTypeID"] == 1


# ----------------------------------------------------------------------------- Glacier self-update

def test_selfupdate_versions():
    from glacier.selfupdate import is_newer, parse_version
    assert parse_version("v0.2.0") == (0, 2, 0) and parse_version("1.10") == (1, 10)
    assert parse_version("v1.2-beta") is None and parse_version("") is None
    assert is_newer("v0.10.0", "0.9.9") and is_newer("0.2.1", "0.2") and not is_newer("0.2.0", "0.2")
    assert not is_newer("0.1.9", "0.2.0") and not is_newer("garbage", "0.1.0")


def _gh_release(version: str, assets=("tar", "sums"), **extra) -> dict:
    files = {"tar": f"glacier-{version}.tar.gz", "sums": "SHA256SUMS"}
    return {"tag_name": f"v{version}", "html_url": f"https://github.com/x/glacier/releases/tag/v{version}",
            "published_at": "2026-11-05T22:00:00Z", "body": "## Highlights\nCurseForge support\n",
            "assets": [{"name": files[a], "browser_download_url": f"https://example.invalid/{files[a]}"}
                       for a in assets], **extra}


def test_selfupdate_release_json_and_check(tmp_path: Path, monkeypatch):
    from glacier import selfupdate
    rel = selfupdate.release_from_json(_gh_release("9.0.0"))
    assert rel.version == "9.0.0" and rel.asset_name == "glacier-9.0.0.tar.gz" and rel.published == "2026-11-05"
    assert rel.summary == "Highlights"
    assert selfupdate.release_from_json(_gh_release("9.0.0", assets=("tar",))) is None      # no checksums
    assert selfupdate.release_from_json(_gh_release("9.0.0", prerelease=True)) is None

    monkeypatch.setattr(selfupdate, "STATE_FILE", tmp_path / "selfupdate.json")
    calls = []
    answer = {"rel": rel}
    monkeypatch.setattr(selfupdate, "fetch_latest", lambda: calls.append(1) or answer["rel"])
    assert selfupdate.check().version == "9.0.0" and len(calls) == 1
    assert selfupdate.check().version == "9.0.0" and len(calls) == 1          # cached for 6 hours
    assert selfupdate.available().version == "9.0.0"
    answer["rel"] = selfupdate.release_from_json(_gh_release(selfupdate.__version__))
    assert selfupdate.check(force=True) is None and len(calls) == 2           # same version: nothing to do
    assert selfupdate.available() is None and selfupdate.cached_latest() is not None


def _release_files(tmp_path: Path, version: str, top: str | None = None, init_version: str | None = None):
    """A release tarball whose install.sh records where it was asked to install."""
    import tarfile
    src = tmp_path / "build" / (top or f"glacier-{version}")
    (src / "glacier").mkdir(parents=True)
    (src / "glacier" / "__init__.py").write_text(f'__version__ = "{init_version or version}"\n')
    (src / "install.sh").write_text('#!/usr/bin/env bash\nset -e\nmkdir -p "$PREFIX"\n'
                                    'echo installed > "$PREFIX/marker"\n')
    out = tmp_path / "release"
    out.mkdir(exist_ok=True)
    tar = out / f"glacier-{version}.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(src, arcname=src.name)
    (out / "SHA256SUMS").write_text(f"{hashlib.sha256(tar.read_bytes()).hexdigest()}  {tar.name}\n")
    return out


def test_selfupdate_install(tmp_path: Path, monkeypatch):
    from glacier import download, paths, selfupdate
    prefix = tmp_path / "prefix"
    monkeypatch.setattr(paths, "PACKAGE_DIR", prefix / "lib" / "glacier" / "glacier")
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(selfupdate, "STATE_FILE", tmp_path / "selfupdate.json")
    monkeypatch.setattr(selfupdate, "_gh", lambda: None)
    files = {"dir": _release_files(tmp_path, "9.0.0")}

    def fake_download(url, dest, progress=None, cancel=None):
        shutil.copy(files["dir"] / url.rsplit("/", 1)[1], dest)
        return Path(dest)

    monkeypatch.setattr(download, "download", fake_download)
    rel = selfupdate.release_from_json(_gh_release("9.0.0"))
    assert selfupdate.can_self_update()[0]
    assert selfupdate.install(rel) == "9.0.0"
    assert (prefix / "marker").read_text() == "installed\n"          # install.sh ran with PREFIX=prefix

    # A tarball that doesn't match SHA256SUMS is not installed.
    (prefix / "marker").unlink()
    sums = files["dir"] / "SHA256SUMS"
    sums.write_text("0" * 64 + "  glacier-9.0.0.tar.gz\n")
    with pytest.raises(selfupdate.UpdateError, match="Checksum"):
        selfupdate.install(rel)
    assert not (prefix / "marker").exists()

    # Nor one with an unexpected layout or the wrong version inside.
    for kw in ({"top": "evil"}, {"init_version": "6.6.6"}):
        shutil.rmtree(tmp_path / "build")
        files["dir"] = _release_files(tmp_path, "9.0.0", **kw)
        with pytest.raises(selfupdate.UpdateError):
            selfupdate.install(rel)
        assert not (prefix / "marker").exists()


def test_selfupdate_refuses_source_checkout(tmp_path: Path, monkeypatch):
    from glacier import paths, selfupdate
    checkout = tmp_path / "blizzard"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(paths, "PACKAGE_DIR", checkout / "glacier")
    ok, why = selfupdate.can_self_update()
    assert not ok and "git pull" in why
    with pytest.raises(selfupdate.UpdateError):
        selfupdate.install(selfupdate.release_from_json(_gh_release("9.0.0")))
