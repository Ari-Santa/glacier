#!/usr/bin/env bash
# Install Glacier for the current user (no root needed).
#
#   ./install.sh            copy into ~/.local and add menu entries
#   ./install.sh --dev      link to this checkout instead of copying (for hacking on it)
#   ./install.sh --uninstall [--purge]   remove Glacier (--purge also deletes the prefix and data)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
LIB="$PREFIX/lib/glacier"
BIN="$PREFIX/bin"
MODE="copy"
PURGE=0

for arg in "$@"; do
  case "$arg" in
    --dev) MODE="dev" ;;
    --uninstall) MODE="uninstall" ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$MODE" == "uninstall" ]]; then
  if [[ -x "$BIN/glacier" ]]; then "$BIN/glacier" integrate --remove || true; fi
  rm -rf "$LIB" "$BIN/glacier"
  if [[ "$PURGE" == 1 ]]; then
    "${BIN}/glacier" kill 2>/dev/null || true
    rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/glacier" "${XDG_CONFIG_HOME:-$HOME/.config}/glacier" \
           "${XDG_CACHE_HOME:-$HOME/.cache}/glacier" "${XDG_STATE_HOME:-$HOME/.local/state}/glacier"
    echo "Removed Glacier, its Wine prefix and all data."
  else
    echo "Removed Glacier. Your prefix and settings are kept (use --purge to delete them)."
  fi
  exit 0
fi

missing=()
python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=(python3-pyqt6)
python3 -c 'import requests' 2>/dev/null || missing+=(python3-requests)
python3 -c 'import PIL' 2>/dev/null || missing+=(python3-pil)
if (( ${#missing[@]} )); then
  echo "Missing Python packages: ${missing[*]}" >&2
  echo "Install them with:  sudo apt install ${missing[*]}" >&2
  exit 1
fi

mkdir -p "$BIN" "$(dirname "$LIB")"
rm -rf "$LIB"
if [[ "$MODE" == "dev" ]]; then
  mkdir -p "$LIB"
  ln -s "$SRC/glacier" "$LIB/glacier"
else
  mkdir -p "$LIB"
  cp -r "$SRC/glacier" "$LIB/"
  find "$LIB" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

cat > "$BIN/glacier" <<WRAP
#!/bin/sh
# Glacier launcher wrapper (installed by install.sh)
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m glacier "\$@"
WRAP
chmod +x "$BIN/glacier"

"$BIN/glacier" integrate
echo "Installed Glacier to $LIB"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "Note: $BIN is not on your PATH; the app-menu entry works regardless." ;;
esac
echo "Run 'glacier' (or open Glacier from the app menu). First start installs Battle.net automatically."
