#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./shortcuts.sh uninstall || true
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/snapshot.svg"
gnome-extensions disable snapshot@hyunsik1-lee 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/snapshot@hyunsik1-lee"
rm -rf "$HOME/.local/share/snapshot" "$HOME/.local/bin/snapshot" "$HOME/.local/share/applications/snapshot.desktop"
echo "Snapshot removed."
