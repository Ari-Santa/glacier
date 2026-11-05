"""Unit tests for Glacier's parsers and helpers (no Proton or network needed)."""

from __future__ import annotations

import io
import os
import struct
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
