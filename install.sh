#!/bin/sh
# javsubs.lua installer for mpv (macOS / Linux)

set -e

SRC=$(cd "$(dirname "$0")" && pwd)
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/mpv"

echo
echo "  mpv-javsubs installer"
echo "  ---------------------"
echo "  target: $DEST"
echo

if [ ! -f "$SRC/scripts/javsubs.lua" ]; then
    echo "[ERROR] scripts/javsubs.lua not found next to this installer." >&2
    exit 1
fi

mkdir -p "$DEST/scripts" "$DEST/script-opts"

cp "$SRC/scripts/javsubs.lua" "$DEST/scripts/javsubs.lua"
echo "[OK] $DEST/scripts/javsubs.lua"

if [ -f "$DEST/script-opts/javsubs.conf" ]; then
    echo "[SKIP] javsubs.conf already exists, keeping your settings."
    echo "       new default config: $SRC/script-opts/javsubs.conf"
else
    cp "$SRC/script-opts/javsubs.conf" "$DEST/script-opts/javsubs.conf"
    echo "[OK] $DEST/script-opts/javsubs.conf"
fi

if ! command -v curl >/dev/null 2>&1; then
    echo
    echo "[WARN] curl not found in PATH. Install it first (apt/dnf/brew install curl)."
fi

echo
echo "  Done. Restart mpv, open a video, press Ctrl+Shift+s."
echo
