# Glacier

A Battle.net launcher for Linux. Glacier sets up Proton, installs the official Battle.net
client unattended, finds your Blizzard games, launches them, keeps them updated, and gives
every game its own app-menu entry, icon and taskbar identity.

![Glacier icon](glacier/assets/glacier-128.png)

## What it does

| | |
|---|---|
| **Proton** | Uses Steam's Proton builds (auto-detected in every Steam library) or downloads **GE-Proton** from GitHub (sha512-verified). Runs Proton inside the **Steam Linux Runtime** container the build asks for (`toolmanifest.vdf` → Runtime 3.0 *sniper* or 4.0), downloading the runtime itself when Steam isn't installed. |
| **Install** | `glacier setup` (or the first GUI start) creates a dedicated prefix, downloads `Battle.net-Setup.exe` from battle.net (redirect host and signer checked), and installs it with `--lang`/`--installpath` so **no clicks are needed**. The only manual step is logging in to your Blizzard account. |
| **Games** | Reads the Battle.net Agent's `product.db` (protobuf, decoded without dependencies) for installed games, paths, versions and live download/update progress. Falls back to scanning the usual folders. |
| **Updates** | Compares installed versions with Blizzard's public version service (`us.version.battle.net`) and flags games with updates; Battle.net applies them when it runs. Optional autostart keeps Battle.net in the tray so games stay current. GE-Proton can auto-update too. |
| **Launching** | `--exec="launch_uid <uid>"` through the running client (exact product, including WoW Classic flavours), or the game exe directly. Launches hand over to an already-running Battle.net in ~2 s. |
| **Icons** | Extracts each game's icon from its `.exe` (pure-Python PE resource reader), installs it into the hicolor theme and writes `.desktop` entries. A small watcher **tags each game window** so KDE/GNOME show the right icon and group windows per game (see below). |
| **Addons** | `glacier curseforge install` (or the *Addons* page in the GUI, which also lists your WoW flavours and opens their AddOns folders) downloads the official **CurseForge** AppImage from curseforge.com (host, size and MD5 checked), adds it to the app menu with its `cfauth://`/`curseforge://` link handlers. It runs natively, not in Proton, and updates itself; `glacier curseforge update` re-downloads when the published build changed. Glacier also tells CurseForge where WoW is: while CurseForge is closed it writes CurseForge's WoW list (`agent/GameInstances/AddonGameInstance.json`) from Battle.net's installed flavours, adding missing ones and dropping leftover `_ptr_`/`_beta_` folders that hold no addons (on install, before *Open CurseForge*, on library rescans, or `glacier curseforge sync`). |
| **Extras** | MangoHud, GameMode, Gamescope, DXVK HUD, Wayland driver/HDR, custom env/wrappers (global or per game), playtime tracking, tray menu, logs viewer, winecfg/regedit/taskmgr. |

## Install

Needs Python 3.11+, PyQt6, requests and Pillow (`sudo apt install python3-pyqt6 python3-requests python3-pil`).

Battle.net also needs your graphics driver's **32-bit Vulkan driver**. It is a 32-bit program, and
without one it closes right after it starts. Steam installs this driver, so you only need to add it
yourself on a system without Steam:

| System | 32-bit Vulkan driver |
|---|---|
| Debian, Ubuntu (AMD, Intel) | `sudo dpkg --add-architecture i386 && sudo apt update && sudo apt install mesa-vulkan-drivers:i386` |
| Debian, Ubuntu (NVIDIA) | the driver's 32-bit libraries: `nvidia-driver-libs:i386` (Debian), `libnvidia-gl-<version>:i386` (Ubuntu) |
| Arch | `lib32-vulkan-radeon`, `lib32-vulkan-intel` or `lib32-nvidia-utils` (from `[multilib]`) |
| Fedora | `mesa-vulkan-drivers.i686` |

`glacier status` says whether it found one, and the GUI shows a banner when it is missing.

```bash
./install.sh           # copies Glacier to ~/.local/lib/glacier, adds ~/.local/bin/glacier + menu entry
./install.sh --dev     # same, but links to this checkout
./install.sh --uninstall [--purge]
```

Or take a release: download `glacier-<version>.tar.gz` from
[Releases](https://github.com/Ari-Santa/glacier/releases), unpack it and run its `./install.sh`.

Then open **Glacier** from the app menu. On first start it sets up everything by itself;
when the Battle.net login window appears, log in and install games as usual. They show up in
Glacier and in your app menu automatically.

### Updates

Glacier checks its GitHub releases (at most every 6 hours; *Settings → Glacier updates*) and shows
a banner when a newer version is out. **Update Glacier** (or `glacier self-update`) downloads the
release tarball, checks it against the release's `SHA256SUMS`, runs its `install.sh` into the same
place and offers to restart Glacier; Battle.net and running games are left alone. A copy installed
with `--dev` runs from your checkout, so update that one with `git pull`. No GitHub account is
needed; when the GitHub CLI (`gh`) is installed and signed in, Glacier asks through it instead.

## Command line

```text
glacier                      open the launcher (same as `glacier gui`, `--hidden` = tray only)
glacier setup [--library D]  unattended setup: Proton runtime, prefix, Battle.net, menu entries
glacier status [-u] [--json] Proton/Battle.net/game status (-u checks Blizzard for updates)
glacier launch wow           play (ids: `glacier list`; launch codes and names work too)
glacier install pro          ask Battle.net to install a game
glacier open | repair | uninstall <game>
glacier bnet [--background]  start Battle.net (tray only with --background)
glacier kill                 stop Battle.net and every game in the prefix
glacier proton list|available|install [TAG|latest]|update|use ID|remove ID
glacier menu [--remove]      sync app-menu entries;  glacier icons [--force]
glacier run X.exe [args]     run any Windows program in the prefix; winecfg|regedit|taskmgr|explorer
glacier curseforge [install|update|launch|sync|remove|status [-u]]   CurseForge addon manager (AppImage)
glacier config [key [json]]  read/change settings;  glacier logs [-n 100]
glacier self-update [--check]  update Glacier itself from its GitHub releases
```

## How the per-game icons work

Proton names every X11 window `steam_app_<SteamGameId>` (or `steam_proton`), and a game started
by Battle.net inherits Battle.net's environment. As a result every game looks like the same app
to the desktop: wrong icon, all windows grouped together, pinned launchers never light up.

Glacier's watcher matches each window's `_NET_WM_PID` to the Wine process in Glacier's prefix,
maps the exe to a game, and tags the window with:

* `_KDE_NET_WM_DESKTOP_FILE` / `_GTK_APPLICATION_ID` = `glacier-<game>`
* `WM_CLASS` = `glacier-<game>` (matches `StartupWMClass` in the generated `.desktop` file)

KWin picks this up live, so each game gets its own taskbar entry and icon. The watcher runs
inside the GUI, or as a short-lived `glacier watch` process after menu launches; it exits once
the prefix is idle. It needs X11/XWayland windows, so it doesn't apply with the Wine Wayland driver.

## Files

| Path | Contents |
|---|---|
| `~/.config/glacier/config.json` | settings |
| `~/.local/share/glacier/prefix/` | `STEAM_COMPAT_DATA_PATH`; the Wine prefix is `pfx/` |
| `~/.local/share/glacier/runners/` | GE-Proton builds downloaded by Glacier |
| `~/.local/share/glacier/runtimes/` | Steam Linux Runtime (only when Steam's copy isn't found) |
| `~/.local/share/glacier/apps/` | `CurseForge.AppImage` (menu entry: `~/.local/share/applications/CurseForge.desktop`; its own settings live in `~/.config/CurseForge`) |
| `~/.local/state/glacier/logs/` | one log per launch; `stats.json` holds playtime |
| `~/.local/share/applications/glacier-*.desktop` | menu entries (`X-Glacier-Managed=true`) |

If you set a *game library folder*, Glacier maps it to a Windows drive (`D:` by default) and
makes it Battle.net's default install location, so big games can live on another disk.

## Limitations

* Logging in to Battle.net (and any 2FA) is up to you; Glacier never handles credentials.
* Games with kernel anti-cheat that rejects Proton (Call of Duty's Ricochet) won't run online.
* CurseForge's addon agent is a .NET program that only looks for OpenSSL 3/1.1. On systems
  with only OpenSSL 4 (Ubuntu 26.10) it would crash on start, so Glacier starts CurseForge with
  `DOTNET_OPENSSL_VERSION_OVERRIDE=4`. It runs with `--no-sandbox`, like its own menu entry,
  because Ubuntu's AppArmor blocks Electron's sandbox for AppImages.
* Settings such as MangoHud or env vars apply when Battle.net starts. Glacier restarts the
  client automatically when they change, but only if no game is running.

## Development

```bash
podman build -t glacier-test -f tools/Containerfile.test tools
podman run --rm -v "$PWD":/src:Z,ro glacier-test sh -c 'python -m pytest -q -p no:cacheprovider && ruff check --no-cache glacier tests tools'
python3 tools/make_icon.py   # re-render the app icon
tools/release.sh 0.3.0 [notes.md]   # tag, build glacier-0.3.0.tar.gz + SHA256SUMS, publish the GitHub release
```

To release: bump `__version__` in `glacier/__init__.py` and `version` in `pyproject.toml`, commit and
push `master`, then run `tools/release.sh`. It refuses to run on a dirty or unpushed tree or when
the two version numbers disagree.

Code map: `engine.py` (Proton commands, prefix, processes), `proton.py` (runner/runtime
discovery and downloads), `battlenet.py` (client install/launch), `library.py` (games, icons,
menus), `blizzard.py` (product.db, .build.info, version service), `pe.py` (icon extraction),
`x11.py` + `watcher.py` (window tagging, playtime), `curseforge.py` (CurseForge AppImage),
`selfupdate.py` (Glacier's own updates),
`gui/` (PyQt6 interface), `cli.py`.
