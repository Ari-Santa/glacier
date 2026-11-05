"""Catalog of Battle.net games.

`uid` is Blizzard's NGDP product code: it is what product.db stores as `product_code`
and what the version servers (us.version.battle.net) use. `launch` is the code
Battle.net accepts in `--exec="launch <code>"`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GameDef:
    uid: str
    name: str
    launch: str
    folder: str                         # default install folder name
    exes: tuple[str, ...] = ()          # candidates relative to the install dir (globs ok); first = game process
    color: str = "#2b6cb0"
    short: str = ""
    aliases: tuple[str, ...] = ()       # other uids/codes seen in product.db
    subdir: str = ""                    # variant folder inside a shared install (WoW flavours)
    note: str = ""
    featured: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def initials(self) -> str:
        if self.short:
            return self.short
        words = [w for w in self.name.replace(":", " ").split() if w[0].isalnum()]
        return "".join(w[0] for w in words[:3]).upper()


BATTLENET_EXE = "Battle.net.exe"
BATTLENET_DIR = "Program Files (x86)/Battle.net"

_COD_NOTE = "Ricochet anti-cheat blocks Call of Duty multiplayer under Proton."

GAMES: tuple[GameDef, ...] = (
    GameDef("wow", "World of Warcraft", "WoW", "World of Warcraft",
            ("_retail_/Wow.exe", "_retail_/WowT.exe"), "#2c5aa0", "WoW", subdir="_retail_"),
    GameDef("wow_classic", "WoW Classic", "WoWC", "World of Warcraft",
            ("_classic_/WowClassic.exe",), "#8a6d2b", "WoWC", subdir="_classic_"),
    GameDef("wow_classic_era", "WoW Classic Era", "WoWC", "World of Warcraft",
            ("_classic_era_/WowClassic.exe",), "#7a5a24", "Era", subdir="_classic_era_"),
    GameDef("wow_anniversary", "WoW Classic Anniversary", "WoWC", "World of Warcraft",
            ("_anniversary_/WowClassic.exe",), "#6b4f1f", "Anni", subdir="_anniversary_"),
    GameDef("pro", "Overwatch 2", "Pro", "Overwatch",
            ("_retail_/Overwatch.exe", "Overwatch.exe", "Overwatch Launcher.exe"),
            "#f99e1a", "OW2", aliases=("prometheus",)),
    GameDef("fenris", "Diablo IV", "Fen", "Diablo IV",
            ("Diablo IV.exe", "Diablo IV Launcher.exe"), "#8b1a1a", "D4"),
    GameDef("osi", "Diablo II: Resurrected", "OSI", "Diablo II Resurrected",
            ("D2R.exe", "Diablo II Resurrected Launcher.exe"), "#6e1f1f", "D2R"),
    GameDef("d3", "Diablo III", "D3", "Diablo III",
            ("x64/Diablo III64.exe", "Diablo III Launcher.exe"), "#a83232", "D3",
            aliases=("diablo3",)),
    GameDef("anbs", "Diablo Immortal", "ANBS", "Diablo Immortal",
            ("DiabloImmortal.exe", "Diablo Immortal Launcher.exe"), "#7d2424", "DI"),
    GameDef("hsb", "Hearthstone", "WTCG", "Hearthstone",
            ("Hearthstone.exe", "Hearthstone Beta Launcher.exe"), "#c9822b", "HS",
            aliases=("hs_beta",)),
    GameDef("hero", "Heroes of the Storm", "Hero", "Heroes of the Storm",
            ("Versions/Base*/HeroesOfTheStorm_x64.exe", "Support64/HeroesSwitcher_x64.exe",
             "Heroes of the Storm.exe"), "#2d79c7", "HotS", aliases=("heroes",)),
    GameDef("s2", "StarCraft II", "S2", "StarCraft II",
            ("Versions/Base*/SC2_x64.exe", "Support64/SC2Switcher_x64.exe", "StarCraft II.exe"),
            "#1d5e8c", "SC2"),
    GameDef("s1", "StarCraft: Remastered", "S1", "StarCraft",
            ("x86_64/StarCraft.exe", "x86/StarCraft.exe", "StarCraft Launcher.exe"),
            "#2a4d6e", "SC"),
    GameDef("w3", "Warcraft III: Reforged", "W3", "Warcraft III",
            ("_retail_/x86_64/Warcraft III.exe", "Warcraft III Launcher.exe"), "#3d6b2e", "W3"),
    GameDef("w1r", "Warcraft I: Remastered", "W1R", "Warcraft I Remastered",
            (), "#5b7d2b", "W1R", featured=False),
    GameDef("w2r", "Warcraft II: Remastered", "W2R", "Warcraft II Remastered",
            (), "#4c6e26", "W2R", featured=False),
    GameDef("gryphon", "Warcraft Rumble", "GRY", "Warcraft Rumble",
            (), "#d2a32c", "WR"),
    GameDef("rtro", "Blizzard Arcade Collection", "RTRO", "Blizzard Arcade Collection",
            (), "#5c3d99", "BAC", featured=False),
    GameDef("auks", "Call of Duty", "AUKS", "Call of Duty",
            ("_retail_/cod.exe", "cod.exe", "Call of Duty Launcher.exe"), "#3a3a3a", "CoD",
            note=_COD_NOTE),
    GameDef("odin", "Call of Duty: Modern Warfare (2019)", "ODIN", "Call of Duty Modern Warfare",
            ("ModernWarfare.exe",), "#41463a", "MW", note=_COD_NOTE, featured=False),
    GameDef("zeus", "Call of Duty: Black Ops Cold War", "ZEUS", "Call of Duty Black Ops Cold War",
            ("BlackOpsColdWar.exe",), "#4a3b2f", "CW", note=_COD_NOTE, featured=False),
    GameDef("fore", "Call of Duty: Vanguard", "FORE", "Call of Duty Vanguard",
            ("Vanguard.exe",), "#4d3a2a", "VG", note=_COD_NOTE, featured=False),
    GameDef("viper", "Call of Duty: Black Ops 4", "VIPR", "Call of Duty Black Ops 4",
            ("BlackOps4.exe",), "#2f3b4a", "BO4", note=_COD_NOTE, featured=False),
    GameDef("lazr", "Call of Duty: MW2 Campaign Remastered", "LAZR",
            "Call of Duty Modern Warfare 2 Campaign Remastered", ("MW2CR.exe",), "#3b4a2f", "MW2R",
            aliases=("lazarus",), featured=False),
    GameDef("wlby", "Crash Bandicoot 4: It's About Time", "WLBY", "Crash Bandicoot 4",
            (), "#d9661f", "CB4", featured=False),
)

BY_UID: dict[str, GameDef] = {}
for _g in GAMES:
    BY_UID[_g.uid.lower()] = _g
    for _a in _g.aliases:
        BY_UID.setdefault(_a.lower(), _g)


def lookup(key: str) -> GameDef | None:
    """Find a game by uid, alias, launch code or (case-insensitive) name."""
    k = key.strip().lower()
    if k in BY_UID:
        return BY_UID[k]
    for g in GAMES:
        if g.launch.lower() == k or g.name.lower() == k:
            return g
    return None


# Executables that are never the game itself (used by the exe heuristic).
NOT_GAME_EXE = (
    "crash", "error", "report", "uninstall", "unins", "setup", "install", "agent", "battle.net",
    "blizzardbrowser", "vc_redist", "vcredist", "dxsetup", "directx", "ue4prereq", "easyanticheat",
    "helper", "bootstrap", "updater", "repair", "qtwebengine", "cef", "dumper", "telemetry",
    "browser", "bugreport", "launcher_helper",
)
